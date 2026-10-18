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

__all__ = [
    "ModelInfo", "Message", "ToolSpec", "ChatRequest",
    "Token", "ToolCall", "ToolResult", "Usage", "Done", "ProviderError", "Event",
    "Provider", "PROVIDERS", "build_provider", "map_http_error", "estimate_tokens",
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


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
#: provider id -> factory. Labels are intentionally vendor-neutral: the shipped
#: UI never names a vendor (design §9 / task constraint).
PROVIDERS: dict[str, str] = {
    "openai_compat": "Compatible chat API (hosted or local)",
    "anthropic": "Native messages API",
    "fake": "Deterministic offline provider (testing)",
}

DEFAULT_BASE_URLS = {
    "openai_compat": "https://api.example.invalid/v1",
    "anthropic": "https://api.example.invalid",
}


def build_provider(config, key: str | None = None, *, transport=None):
    """Construct the configured provider, or raise :class:`ProviderError`.

    ``transport`` is an optional ``httpx.AsyncBaseTransport`` (tests use it to
    point an adapter at a local mock without a socket).
    """
    provider_id = (getattr(config, "provider", None) or "").strip()
    if not provider_id:
        raise ProviderError("bad_request", "no provider is configured")
    if provider_id not in PROVIDERS:
        raise ProviderError("bad_request", f"unknown provider: {provider_id!r}")
    if provider_id == "fake":
        from pacifica_obd.web.ai.fake import FakeProvider

        return FakeProvider(model=getattr(config, "model", "") or "fake-1")
    if provider_id == "openai_compat":
        from pacifica_obd.web.ai.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(config, key=key, transport=transport)
    if provider_id == "anthropic":
        from pacifica_obd.web.ai.anthropic import AnthropicProvider

        return AnthropicProvider(config, key=key, transport=transport)
    raise ProviderError("bad_request", f"unknown provider: {provider_id!r}")


# Kept so a caller can ask whether an async factory exists without importing the
# adapter modules (both are optional to the base install).
def provider_is_async(provider) -> bool:
    return inspect.isasyncgenfunction(getattr(provider, "stream", None)) or hasattr(
        provider, "stream"
    )
