"""OpenAI-compatible chat adapter (design §5.3).

One translation covers a hosted ``/v1/chat/completions`` endpoint *and* every
local server that speaks the same shape. ``base_url`` is therefore a first-class
setting: point it at a loopback server and no vehicle data leaves the machine.

Errors are normalised with :func:`map_http_error`; the response body is capped and
redacted by the caller's error handler, never echoed raw by this module.
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

__all__ = ["OpenAICompatProvider"]

DEFAULT_TIMEOUT_S = 120.0


def _base_url(config) -> str:
    from pacifica_obd.web.ai.config import DEFAULT_BASE_URLS

    url = (getattr(config, "base_url", None) or DEFAULT_BASE_URLS["openai_compat"]).rstrip("/")
    return url


class OpenAICompatProvider:
    id = "openai_compat"
    label = "Compatible chat API (hosted or local)"
    requires_key = True

    def __init__(self, config, *, key: str | None = None, transport=None):
        self.model = getattr(config, "model", "") or ""
        self.base_url = _base_url(config)
        self.temperature = float(getattr(config, "temperature", 0.2))
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
        client = await self._client_()
        try:
            response = await client.get(f"{self.base_url}/models", headers=self._headers())
        except Exception as exc:  # noqa: BLE001 - httpx errors → typed provider error
            raise _transport_error(exc) from exc
        if response.status_code >= 400:
            raise map_http_error(response.status_code, response.text)
        try:
            rows = response.json().get("data") or []
        except ValueError as exc:
            raise ProviderError("bad_request", "the provider returned no model list") from exc
        models = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            models.append(ModelInfo(str(row["id"]), str(row.get("id")),
                                    context=int(row.get("context_length") or 0),
                                    supports_tools=True, local=self.local))
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
        try:
            async with client.stream("POST", f"{self.base_url}/chat/completions",
                                     json=payload, headers=self._headers()) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    retry = _retry_after(response)
                    raise map_http_error(response.status_code, body, retry_after=retry)
                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        raw = chunk["usage"]
                        usage = Usage(
                            input_tokens=int(raw.get("prompt_tokens") or 0),
                            output_tokens=int(raw.get("completion_tokens") or 0),
                        )
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            yield Token(str(delta["content"]))
                        for tool in delta.get("tool_calls") or []:
                            index = int(tool.get("index") or 0)
                            slot = calls.setdefault(index, {"id": None, "name": "", "args": ""})
                            if tool.get("id"):
                                slot["id"] = str(tool["id"])
                            function = tool.get("function") or {}
                            if function.get("name"):
                                slot["name"] += str(function["name"])
                            if function.get("arguments"):
                                slot["args"] += str(function["arguments"])
                        if choice.get("finish_reason"):
                            finish = str(choice["finish_reason"])
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _transport_error(exc) from exc

        tool_calls = _finalise_calls(calls)
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


def _joined(req: ChatRequest) -> str:
    return "\n".join(message.content or "" for message in req.messages)


def _finalise_calls(calls: dict[int, dict]) -> list[ToolCall]:
    out = []
    for index in sorted(calls):
        slot = calls[index]
        if not slot.get("name"):
            continue
        try:
            args = json.loads(slot.get("args") or "{}")
        except ValueError:
            args = {"_raw": slot.get("args")}
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
