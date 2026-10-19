"""OpenAI-compatible chat adapter (design §5.3).

One translation covers a hosted ``/v1/chat/completions`` endpoint *and* every
local server that speaks the same shape. ``base_url`` is therefore a first-class
setting: point it at a loopback server and no vehicle data leaves the machine.

Errors are normalised with :func:`map_http_error`; the response body is capped and
redacted by the caller's error handler, never echoed raw by this module. A stream
that is not SSE at all (a corporate proxy sign-in page, a JSON error) is reported
as a failure rather than being silently treated as a successful empty answer.
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
    MAX_MODELS,
    config_base_url,
    estimate_tokens,
    iter_sse_data,
    map_http_error,
    read_capped,
    retry_backoff,
    RETRYABLE_CODES,
)

__all__ = ["OpenAICompatProvider"]

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_BASE_URL = "https://api.example.invalid/v1"
MAX_MODELS_BODY_BYTES = 4 * 1024 * 1024
#: Retried only when nothing has been emitted yet, so a retry can never duplicate
#: a token the client already saw.
_MAX_ATTEMPTS = 2


class OpenAICompatProvider:
    id = "openai_compat"
    label = "OpenAI-compatible chat API"
    requires_key = True
    #: ``GET /models`` is part of the compatible shape, so the probe can discover.
    supports_model_discovery = True

    def __init__(self, config, *, key: str | None = None, transport=None):
        # The preset id (e.g. "openai", "ollama") is what the UI and the rate
        # limiter think in; the wire format is an implementation detail.
        preset_id = getattr(config, "provider", None)
        self.id = str(preset_id or type(self).id)
        self.label = "OpenAI-compatible chat API"
        self.model = getattr(config, "effective_model", None)
        self.model = self.model() if callable(self.model) else (
            getattr(config, "model", "") or "")
        self.base_url = config_base_url(config, DEFAULT_BASE_URL)
        self.temperature = float(getattr(config, "temperature", 0.2) or 0.2)
        self.key = key
        self.local = bool(getattr(config, "is_local", lambda: False)())
        self._transport = transport
        self._client = None
        self._include_usage = "openai.com" in self.base_url

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
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        return headers

    async def list_models(self) -> list[ModelInfo]:
        # Bounded: the response is streamed and capped before it is parsed, so a
        # hostile endpoint cannot force an unbounded allocation just by being probed.
        client = await self._client_()
        try:
            async with client.stream("GET", f"{self.base_url}/models",
                                     headers=self._headers()) as response:
                if response.status_code >= 400:
                    body = (await read_capped(response)).decode("utf-8", "replace")
                    raise map_http_error(response.status_code, body)
                raw = await read_capped(response, limit=MAX_MODELS_BODY_BYTES)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - httpx errors → typed provider error
            raise _transport_error(exc) from exc
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ProviderError("bad_request", "the provider returned no model list") from exc
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("bad_request", "the provider returned no model list")
        models = []
        for row in rows[:MAX_MODELS]:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            try:
                context = int(row.get("context_length") or 0)
            except (TypeError, ValueError, OverflowError):
                context = 0
            models.append(ModelInfo(str(row["id"]), str(row.get("id")),
                                    context=context, supports_tools=True, local=self.local))
        return models

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        payload = {
            "model": req.model or self.model,
            "messages": self._messages(req),
            "temperature": req.temperature,
            "stream": True,
        }
        if req.max_output_tokens:
            payload["max_tokens"] = req.max_output_tokens
        if req.tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": tool.name, "description": tool.description,
                              "parameters": tool.parameters}}
                for tool in req.tools
            ]
        if self._include_usage:
            payload["stream_options"] = {"include_usage": True}

        client = await self._client_()
        calls: dict[int, dict] = {}
        finish = None
        usage: Usage | None = None
        produced = False
        terminal = False
        # `emitted` is true only once a token has reached the caller; a buffered
        # tool fragment must not disable a retry (nothing has been delivered yet).
        emitted = False

        attempt = 0
        while True:
            attempt += 1
            try:
                async with client.stream("POST", f"{self.base_url}/chat/completions",
                                         json=payload, headers=self._headers()) as response:
                    if response.status_code >= 400:
                        body = (await read_capped(response)).decode("utf-8", "replace")
                        raise map_http_error(response.status_code, body,
                                             retry_after=_retry_after(response))
                    async for data in iter_sse_data(response):
                        if data.strip() == "[DONE]":
                            terminal = True
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if chunk.get("error"):
                            raise _stream_error(chunk)
                        if chunk.get("usage"):
                            raw = chunk["usage"]
                            usage = Usage(
                                input_tokens=int(raw.get("prompt_tokens") or 0),
                                output_tokens=int(raw.get("completion_tokens") or 0),
                            )
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                produced = True
                                emitted = True
                                yield Token(str(delta["content"]))
                            for tool in delta.get("tool_calls") or []:
                                produced = True
                                index = int(tool.get("index") or 0)
                                slot = calls.setdefault(
                                    index, {"id": None, "name": "", "args": ""})
                                if tool.get("id"):
                                    slot["id"] = str(tool["id"])
                                function = tool.get("function") or {}
                                if function.get("name"):
                                    slot["name"] += str(function["name"])
                                if function.get("arguments"):
                                    slot["args"] += str(function["arguments"])
                            if choice.get("finish_reason"):
                                finish = str(choice["finish_reason"])
                                terminal = True
                if not terminal and not produced:
                    # A 200 that carried no SSE events and no answer is not a
                    # successful empty completion; it is a broken endpoint.
                    raise ProviderError(
                        "bad_request",
                        "the provider returned a response that is not a chat stream")
                break
            except ProviderError as exc:
                if attempt < _MAX_ATTEMPTS and not emitted and exc.code in RETRYABLE_CODES:
                    await asyncio.sleep(retry_backoff(exc, attempt))
                    calls, finish, usage = {}, None, None
                    produced = terminal = False
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                error = _transport_error(exc)
                if attempt < _MAX_ATTEMPTS and not emitted:
                    await asyncio.sleep(retry_backoff(error, attempt))
                    calls, finish, usage = {}, None, None
                    produced = terminal = False
                    continue
                raise error from exc

        tool_calls = _finalise_calls(calls, terminal=terminal)
        if usage is None:
            usage = Usage(input_tokens=estimate_tokens(req.system + _joined(req)),
                          output_tokens=0)
        yield usage
        if tool_calls:
            yield Done(reason="tool_calls", tool_calls=tuple(tool_calls))
        else:
            yield Done(reason=finish or "stop")

    def _messages(self, req: ChatRequest) -> list[dict]:
        messages: list[dict] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        for message in req.messages:
            if message.role == "tool":
                messages.append({"role": "tool", "tool_call_id": message.tool_call_id,
                                 "name": message.name, "content": message.content})
            elif message.role == "assistant" and message.tool_calls:
                messages.append({
                    "role": "assistant", "content": message.content or None,
                    "tool_calls": [
                        {"id": call.get("id") or call.get("name"), "type": "function",
                         "function": {"name": call.get("name"),
                                      "arguments": json.dumps(call.get("args") or {})}}
                        for call in message.tool_calls
                    ],
                })
            else:
                messages.append({"role": message.role, "content": message.content})
        return messages


def _stream_error(chunk: dict) -> ProviderError:
    """Map an in-band ``{"error": {...}}`` chunk without echoing its text."""
    err = chunk.get("error")
    if isinstance(err, dict):
        code = str(err.get("code") or err.get("type") or "").lower()
    else:
        code = str(err or "").lower()
    if "context" in code and ("length" in code or "token" in code):
        return ProviderError("context_length", "the request exceeded the model's context window")
    if "rate" in code or "quota" in code:
        return ProviderError("rate_limit", "the provider is rate limiting requests")
    if "auth" in code or "key" in code:
        return ProviderError("auth", "the provider rejected the key or permissions")
    return ProviderError("unavailable", "the provider reported an error")


def _joined(req: ChatRequest) -> str:
    return "\n".join(message.content or "" for message in req.messages)


def _finalise_calls(calls: dict[int, dict], *, terminal: bool = True) -> list[ToolCall]:
    if not terminal:
        # The stream ended without a terminal event (no [DONE] and no
        # finish_reason): a call assembled from a truncated stream is not trusted.
        return []
    out = []
    for index in sorted(calls):
        slot = calls[index]
        if not slot.get("name"):
            continue
        raw = slot.get("args") or ""
        try:
            args = json.loads(raw or "{}")
        except ValueError:
            args = {"_raw": raw}
        if not isinstance(args, dict):
            args = {"value": args}
        out.append(ToolCall(id=slot.get("id") or f"call_{index}", name=slot["name"], args=args))
    return out


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
