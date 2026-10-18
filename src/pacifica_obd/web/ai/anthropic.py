"""Native Messages-API adapter (design §5.3).

A second wire shape lives here so the rest of the app stays vendor-neutral: tools
map to ``input_schema``, tool results map to ``tool_result`` content blocks, and
the static system prompt carries a cache hint (a real cost win when the evidence
block is large). Unknown HTTP failures are normalised exactly like the compatible
adapter's.
"""

from __future__ import annotations

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
    estimate_tokens,
    map_http_error,
)

__all__ = ["AnthropicProvider"]

API_VERSION = "2023-06-01"
DEFAULT_TIMEOUT_S = 120.0


def _base_url(config) -> str:
    from pacifica_obd.web.ai.config import DEFAULT_BASE_URLS

    url = (getattr(config, "base_url", None) or DEFAULT_BASE_URLS["anthropic"]).rstrip("/")
    return url


class AnthropicProvider:
    id = "anthropic"
    label = "Native messages API"
    requires_key = True
    local = False

    def __init__(self, config, *, key: str | None = None, transport=None):
        self.model = getattr(config, "model", "") or ""
        self.base_url = _base_url(config)
        self.temperature = float(getattr(config, "temperature", 0.2))
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
        try:
            async with client.stream("POST", f"{self.base_url}/v1/messages", json=payload,
                                     headers=self._headers()) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise map_http_error(response.status_code, body,
                                         retry_after=_retry_after(response))
                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    kind = chunk.get("type")
                    if kind == "message_start":
                        raw = (chunk.get("message") or {}).get("usage") or {}
                        usage = Usage(input_tokens=int(raw.get("input_tokens") or 0))
                    elif kind == "content_block_start":
                        block = chunk.get("content_block") or {}
                        index = int(chunk.get("index") or 0)
                        if block.get("type") == "tool_use":
                            blocks[index] = {"id": block.get("id"), "name": block.get("name"),
                                             "args": ""}
                    elif kind == "content_block_delta":
                        delta = chunk.get("delta") or {}
                        index = int(chunk.get("index") or 0)
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield Token(str(delta["text"]))
                        elif delta.get("type") == "input_json_delta":
                            blocks.setdefault(index, {"id": None, "name": "", "args": ""})
                            blocks[index]["args"] += str(delta.get("partial_json") or "")
                    elif kind == "message_delta":
                        raw = chunk.get("usage") or {}
                        usage = Usage(input_tokens=usage.input_tokens,
                                      output_tokens=int(raw.get("output_tokens") or 0),
                                      cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0))
                        stop_reason = (chunk.get("delta") or {}).get("stop_reason") or stop_reason
                    elif kind == "error":
                        error = chunk.get("error") or {}
                        raise ProviderError("internal", str(error.get("message") or "provider error"))
                    elif kind == "message_stop":
                        break
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _transport_error(exc) from exc

        calls = []
        for index in sorted(blocks):
            block = blocks[index]
            if not block.get("name"):
                continue
            try:
                args = json.loads(block.get("args") or "{}")
            except ValueError:
                args = {"_raw": block.get("args")}
            calls.append(ToolCall(id=block.get("id") or f"call_{index}", name=block["name"],
                                  args=args if isinstance(args, dict) else {"value": args}))
        if usage.output_tokens == 0:
            usage = Usage(input_tokens=usage.input_tokens,
                          output_tokens=estimate_tokens(_joined(req)),
                          cache_read_tokens=usage.cache_read_tokens)
        yield usage
        if calls and (stop_reason == "tool_use" or True):
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
