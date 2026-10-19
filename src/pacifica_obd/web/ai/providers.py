"""Provider protocol, neutral event types and the registry (design §5.3).

Two native adapters cover every provider the brief names:

* :mod:`pacifica_obd.web.ai.openai_compat` — any endpoint that speaks the
  ``/v1/chat/completions`` shape. That includes the big hosted APIs, the
  OpenAI-compatible endpoint of a certain search-and-cloud vendor, and every
  local server (runtime on ``127.0.0.1:11434/v1``, desktop runners, vLLM, …).
  ``base_url`` is a first-class setting.
* :mod:`pacifica_obd.web.ai.anthropic` — a native Messages-API adapter.

:mod:`pacifica_obd.web.ai.fake` replays deterministic output for tests and for
offline demos. There is **no silent fallback**: the model id is explicit.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from pacifica_obd.web.ai import presets as _presets

__all__ = [
    "ModelInfo", "Message", "ToolSpec", "ChatRequest",
    "Token", "ToolCall", "ToolResult", "Usage", "Done", "ProviderError", "Event",
    "Provider", "PROVIDERS", "build_provider", "map_http_error", "estimate_tokens",
    "effective_model", "config_base_url", "iter_sse_data", "retry_backoff",
    "read_capped", "MAX_MODELS",
]


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    context: int = 0
    supports_tools: bool = False
    local: bool = False


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: tuple = ()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict

    def as_json(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


@dataclass
class ChatRequest:
    model: str
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float = 0.2
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class Token:
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class ToolResult:
    id: str
    summary: str
    untrusted: bool = True
    data: dict | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    usd_estimate: float = 0.0
    cache_read_tokens: int = 0


@dataclass(frozen=True)
class Done:
    reason: str = "stop"
    tool_calls: tuple = ()


class ProviderError(Exception):
    """A normalised provider failure (never contains a key; see :mod:`keys`).

    ``code`` is a stable machine string: ``auth``, ``rate_limit``, ``timeout``,
    ``network``, ``bad_request``, ``context_length``, ``unavailable``,
    ``cancelled``, ``budget`` or ``internal``.
    """

    def __init__(self, code: str, message: str, *, retry_after_s: int | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s
        self.status = status

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "retry_after_s": self.retry_after_s}


Event = Token | ToolCall | ToolResult | Usage | Done | ProviderError


@runtime_checkable
class Provider(Protocol):
    id: str
    label: str
    requires_key: bool
    local: bool

    async def list_models(self) -> list[ModelInfo]: ...

    def stream(self, req: ChatRequest) -> AsyncIterator[Event]: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- #
# error mapping (shared by both adapters and unit-tested directly)
# --------------------------------------------------------------------------- #
_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def map_http_error(status: int, body: str, *, retry_after: int | None = None) -> ProviderError:
    """Map an HTTP status onto a typed :class:`ProviderError`.

    The provider's response body is **never** used as the public message: it is
    attacker-influenced in the sense that it is arbitrary text the user's key was
    sent alongside, and it routinely echoes the credential or the request (a 500
    can quote the whole payload, VIN included). A short readable class of error
    is enough for the user; the body stays out of the event stream, the logs and
    the transcript.
    """
    if status in (401, 403):
        return ProviderError("auth", "the provider rejected the key or permissions",
                             status=status)
    if status == 429:
        return ProviderError("rate_limit", "the provider is rate limiting requests",
                             retry_after_s=retry_after, status=status)
    if status == 404:
        return ProviderError("bad_request", "the provider has no such model or path",
                             status=status)
    if status in (400, 422):
        lowered = (body or "").lower()
        if "context" in lowered and ("length" in lowered or "token" in lowered):
            return ProviderError("context_length",
                                 "the request exceeded the model's context window",
                                 status=status)
        return ProviderError("bad_request", "the provider rejected the request",
                             status=status)
    if status in _RETRYABLE or status >= 500:
        return ProviderError("unavailable", f"the provider failed (HTTP {status})",
                             retry_after_s=retry_after, status=status)
    return ProviderError("internal", f"provider error {status}", status=status)


def estimate_tokens(text: str) -> int:
    """A cheap, deterministic fallback when a provider reports no usage (~4 char/token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


#: Codes a transport may retry when nothing has been emitted yet.
RETRYABLE_CODES = frozenset({"timeout", "network", "unavailable", "rate_limit"})
MAX_BACKOFF_S = 2.0

#: Hard caps so a hostile or broken endpoint cannot exhaust memory. A chat turn
#: that legitimately exceeds these is pathological; failing closed is safer.
MAX_SSE_BYTES = 16 * 1024 * 1024
MAX_SSE_LINE_BYTES = 2 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_MODELS = 2000


async def iter_sse_data(response, *, max_bytes: int = MAX_SSE_BYTES,
                        max_line: int = MAX_SSE_LINE_BYTES):
    """Yield the concatenated ``data:`` payload of each SSE event on ``response``.

    Parsed from raw **bytes** so the byte budget is enforced *before* a line is
    buffered: ``response.aiter_lines()`` would itself accumulate an unterminated
    line without limit, so this splits lines from bounded chunks instead. Per the
    SSE spec several ``data:`` lines in one event join with a newline; blank lines
    delimit events; ``:`` comment lines are ignored.
    """
    data_lines: list[str] = []
    buffer = b""
    total = 0

    def feed(line: bytes):
        text = line.decode("utf-8", "replace").rstrip("\r")
        if text == "":
            return "\n".join(data_lines) if data_lines else None
        if text.startswith(":"):
            return None
        field, _, value = text.partition(":")
        if field == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
        return None

    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ProviderError("unavailable", "the provider sent an oversized stream")
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline == -1:
                break
            line, buffer = buffer[:newline], buffer[newline + 1:]
            if len(line) > max_line:
                raise ProviderError("unavailable",
                                    "the provider sent an oversized stream")
            event = feed(line)
            if event is not None:
                data_lines = []
                yield event
        if len(buffer) > max_line:
            raise ProviderError("unavailable", "the provider sent an oversized stream")
    if buffer:
        event = feed(buffer)
        if event is not None:
            yield event
    if data_lines:
        yield "\n".join(data_lines)


async def read_capped(response, limit: int = MAX_ERROR_BODY_BYTES) -> bytes:
    """Read at most ``limit`` bytes of a response (never buffer an unbounded body).

    Used for HTTP error bodies: the body is only inspected to classify the error
    and is never returned to a client, so truncating it is safe.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        remaining = limit - total
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        total += len(chunk)
        if total >= limit:
            break
    return b"".join(chunks)


def retry_backoff(error: "ProviderError", attempt: int) -> float:
    """Seconds to wait before retry number ``attempt`` (1-based), capped."""
    if getattr(error, "retry_after_s", None):
        return float(min(max(int(error.retry_after_s), 0), MAX_BACKOFF_S))
    return min(0.5 * (2 ** (max(1, attempt) - 1)), MAX_BACKOFF_S)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
#: preset id -> label. The set of provider ids the API accepts is exactly the
#: preset registry (see :mod:`pacifica_obd.web.ai.presets`).
PROVIDERS: dict[str, str] = {pid: p.label for pid, p in _presets.PRESETS.items()}

#: Kept for backwards compatibility; the live defaults live in the preset rows.
DEFAULT_BASE_URLS = {pid: p.default_base_url for pid, p in _presets.PRESETS.items()}


def effective_model(config) -> str:
    """The model the adapters should send for ``config`` (config wins, then preset)."""
    getter = getattr(config, "effective_model", None)
    if callable(getter):
        return getter()
    model = (getattr(config, "model", "") or "").strip()
    if model:
        return model
    found = _presets.preset(getattr(config, "provider", None))
    return found.default_model if found else ""


def config_base_url(config, fallback: str = "") -> str:
    """The base URL a config points at, or the preset default, or ``fallback``.

    Prefers an explicit ``config.url()`` (which knows the preset) but tolerates a
    plain object that only carries ``base_url`` — the adapters are constructed
    with the latter in a couple of unit tests.
    """
    url = getattr(config, "base_url", None)
    if not url:
        getter = getattr(config, "url", None)
        if callable(getter):
            try:
                url = getter()
            except Exception:  # noqa: BLE001 - never let a bad config crash a build
                url = None
    if not url:
        url = _presets.default_base_url(getattr(config, "provider", None)) or fallback
    return str(url or "").rstrip("/")


def build_provider(config, key: str | None = None, *, transport=None):
    """Construct the configured provider, or raise :class:`ProviderError`.

    ``transport`` is an optional ``httpx.AsyncBaseTransport`` (tests use it to
    point an adapter at a local mock without a socket).
    """
    provider_id = (getattr(config, "provider", None) or "").strip()
    if not provider_id:
        raise ProviderError("bad_request", "no provider is configured")
    found = _presets.preset(provider_id)
    if found is None:
        raise ProviderError("bad_request", f"unknown provider: {provider_id!r}")
    if found.format == _presets.FAKE_FORMAT:
        from pacifica_obd.web.ai.fake import FakeProvider

        return FakeProvider(model=effective_model(config) or "fake-1")
    if found.format == _presets.OPENAI_COMPAT_FORMAT:
        from pacifica_obd.web.ai.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(config, key=key, transport=transport)
    if found.format == _presets.ANTHROPIC_FORMAT:
        from pacifica_obd.web.ai.anthropic import AnthropicProvider

        return AnthropicProvider(config, key=key, transport=transport)
    raise ProviderError("bad_request", f"unknown provider: {provider_id!r}")


# Kept so a caller can ask whether an async factory exists without importing the
# adapter modules (both are optional to the base install).
def provider_is_async(provider) -> bool:
    return inspect.isasyncgenfunction(getattr(provider, "stream", None)) or hasattr(
        provider, "stream"
    )
