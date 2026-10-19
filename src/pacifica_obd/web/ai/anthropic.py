"""Native Messages-API adapter (design §5.3).

A second wire shape lives here so the rest of the app stays vendor-neutral: tools
map to ``input_schema``, tool results map to ``tool_result`` content blocks, and
the static system prompt carries a cache hint (a real cost win when the evidence
block is large). Unknown HTTP failures are normalised exactly like the compatible
adapter's, an in-band ``error`` event raises rather than ending "successfully",
and a stream that never reached a terminal event is reported as a failure.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from pacifica_obd.web.ai.providers import (
    ChatRequest,
    Done,
    Event,
    ModelInfo,
    ProviderError,
    Token,
    ToolCall,
    Usage,
    config_base_url,
    estimate_tokens,
    iter_sse_data,
    map_http_error,
    read_capped,
    retry_backoff,
    RETRYABLE_CODES,
)

__all__ = ["AnthropicProvider"]

API_VERSION = "2023-06-01"
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_BASE_URL = "https://api.example.invalid"
_MAX_ATTEMPTS = 2


class AnthropicProvider:
    id = "anthropic"
    label = "Anthropic Messages API"
    requires_key = True
    local = False
    #: The Messages API has no model-discovery endpoint; the probe must ping.
    supports_model_discovery = False

    def __init__(self, config, *, key: str | None = None, transport=None):
        preset_id = getattr(config, "provider", None)
        self.id = str(preset_id or type(self).id)
        self.model = getattr(config, "effective_model", None)
        self.model = self.model() if callable(self.model) else (
            getattr(config, "model", "") or "")
        self.base_url = config_base_url(config, DEFAULT_BASE_URL)
        self.temperature = float(getattr(config, "temperature", 0.2) or 0.2)
        self.key = key
        self.local = bool(getattr(config, "is_local", lambda: False)())
        self._transport = transport
        self._client = None

    async def _client_(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=10.0),
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _headers(self) -> dict:
        headers = {"content-type": "application/json", "accept": "text/event-stream",
                   "anthropic-version": API_VERSION}
        if self.key:
            headers["x-api-key"] = self.key
        return headers

    async def list_models(self) -> list[ModelInfo]:
        # The Messages API has no public model list; the model id is explicit by
        # design (there is no silent fallback), so this returns just the one.
        return [ModelInfo(self.model, self.model, context=200000, supports_tools=True,
                          local=self.local)] if self.model else []

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        payload = {
            "model": req.model or self.model,
            "system": [{"type": "text", "text": req.system,
                        "cache_control": {"type": "ephemeral"}}] if req.system else None,
            "messages": self._messages(req),
            "max_tokens": req.max_output_tokens or 1024,
            "temperature": req.temperature,
            "stream": True,
        }
        if req.tools:
            payload["tools"] = [
                {"name": tool.name, "description": tool.description,
                 "input_schema": tool.parameters}
                for tool in req.tools
            ]
        payload = {key: value for key, value in payload.items() if value is not None}

        client = await self._client_()
        blocks: dict[int, dict] = {}
        usage = Usage()
        stop_reason = None
        answer_parts: list[str] = []
        produced = False
        terminal = False
        emitted = False

        attempt = 0
        while True:
            attempt += 1
            try:
                async with client.stream("POST", f"{self.base_url}/v1/messages",
                                         json=payload, headers=self._headers()) as response:
                    if response.status_code >= 400:
                        body = (await read_capped(response)).decode("utf-8", "replace")
                        raise map_http_error(response.status_code, body,
                                             retry_after=_retry_after(response))
                    async for data in iter_sse_data(response):
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        kind = chunk.get("type")
                        if kind == "message_start":
                            raw = (chunk.get("message") or {}).get("usage") or {}
                            usage = Usage(
                                input_tokens=int(raw.get("input_tokens") or 0),
                                cache_read_tokens=int(
                                    raw.get("cache_read_input_tokens") or 0))
                        elif kind == "content_block_start":
                            block = chunk.get("content_block") or {}
                            index = int(chunk.get("index") or 0)
                            if block.get("type") == "tool_use":
                                blocks[index] = {"id": block.get("id"),
                                                 "name": block.get("name"), "args": ""}
                        elif kind == "content_block_delta":
                            delta = chunk.get("delta") or {}
                            index = int(chunk.get("index") or 0)
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                produced = True
                                emitted = True
                                answer_parts.append(str(delta["text"]))
                                yield Token(str(delta["text"]))
                            elif delta.get("type") == "input_json_delta":
                                produced = True
                                blocks.setdefault(index, {"id": None, "name": "", "args": ""})
                                blocks[index]["args"] += str(delta.get("partial_json") or "")
                        elif kind == "message_delta":
                            raw = chunk.get("usage") or {}
                            # Preserve the cached-input count from message_start:
                            # the delta's usage block usually omits it.
                            cache_read = int(raw.get("cache_read_input_tokens")
                                             or usage.cache_read_tokens or 0)
                            usage = Usage(input_tokens=usage.input_tokens,
                                          output_tokens=int(raw.get("output_tokens") or 0),
                                          cache_read_tokens=cache_read)
                            stop_reason = ((chunk.get("delta") or {}).get("stop_reason")
                                           or stop_reason)
                        elif kind == "error":
                            error = chunk.get("error") or {}
                            code = str(error.get("type") or error.get("code") or "").lower()
                            if "overloaded" in code or "rate" in code:
                                raise ProviderError("rate_limit",
                                                    "the provider is rate limiting requests")
                            if "auth" in code or "permission" in code:
                                raise ProviderError("auth",
                                                    "the provider rejected the key or permissions")
                            raise ProviderError("unavailable",
                                                "the provider reported an error")
                        elif kind == "message_stop":
                            terminal = True
                            break
                if not terminal and not produced:
                    raise ProviderError(
                        "bad_request",
                        "the provider returned a response that is not a messages stream")
                break
            except ProviderError as exc:
                if attempt < _MAX_ATTEMPTS and not emitted and exc.code in RETRYABLE_CODES:
                    await asyncio.sleep(retry_backoff(exc, attempt))
                    blocks, usage, stop_reason, answer_parts = {}, Usage(), None, []
                    produced = terminal = False
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                error = _transport_error(exc)
                if attempt < _MAX_ATTEMPTS and not emitted:
                    await asyncio.sleep(retry_backoff(error, attempt))
                    blocks, usage, stop_reason, answer_parts = {}, Usage(), None, []
                    produced = terminal = False
                    continue
                raise error from exc

        calls = []
        for index in sorted(blocks):
            if not terminal:
                # No message_stop: a tool_use assembled from a truncated stream is
                # not trusted.
                break
            block = blocks[index]
            if not block.get("name"):
                continue
            raw = block.get("args") or ""
            try:
                args = json.loads(raw or "{}")
            except ValueError:
                args = {"_raw": raw}
            calls.append(ToolCall(id=block.get("id") or f"call_{index}", name=block["name"],
                                  args=args if isinstance(args, dict) else {"value": args}))
        if usage.output_tokens == 0:
            # Estimate from the text the model actually generated, not the prompt.
            generated = "".join(answer_parts)
            if not generated:
                generated = _joined(req)
            usage = Usage(input_tokens=usage.input_tokens,
                          output_tokens=estimate_tokens(generated),
                          cache_read_tokens=usage.cache_read_tokens)
        yield usage
        if calls:
            yield Done(reason="tool_calls", tool_calls=tuple(calls))
        else:
            yield Done(reason=stop_reason or "stop")

    def _messages(self, req: ChatRequest) -> list[dict]:
        messages: list[dict] = []
        for message in req.messages:
            if message.role == "tool":
                messages.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }]})
            elif message.role == "assistant" and message.tool_calls:
                content = []
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    content.append({"type": "tool_use", "id": call.get("id") or call.get("name"),
                                    "name": call.get("name"), "input": call.get("args") or {}})
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append({"role": message.role, "content": message.content})
        return messages


def _joined(req: ChatRequest) -> str:
    return "\n".join(message.content or "" for message in req.messages)


def _retry_after(response) -> int | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _transport_error(exc: Exception) -> ProviderError:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", "the provider timed out")
    return ProviderError("network", "could not reach the provider")
