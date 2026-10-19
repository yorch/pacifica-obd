"""A deterministic, offline provider used by tests and by offline demos.

No network, no key. It can replay a scripted event list (unit tests), echo the
request it received (so redaction and prompt-injection tests can assert what the
model would have seen), or emit a tool call when the prompt asks for one.
"""

from __future__ import annotations

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
)

__all__ = ["FakeProvider", "FakeScript"]


class FakeScript:
    """A tiny scripted event list for unit tests."""

    def __init__(self, events: list[Event], *, error: ProviderError | None = None):
        self.events = list(events)
        self.error = error


class FakeProvider:
    id = "fake"
    label = "Deterministic offline provider (testing)"
    requires_key = False
    local = True
    supports_tools = True
    supports_model_discovery = True

    def __init__(self, model: str = "fake-1", *, script: list[Event] | None = None,
                 error: ProviderError | None = None, huge_usage: bool = False,
                 max_rounds: int = 2):
        self.model = model
        self.script = list(script or [])
        self.error = error
        self.huge_usage = huge_usage
        self.max_rounds = max_rounds
        self.requests: list[ChatRequest] = []
        self.closed = False

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(self.model, "Offline deterministic model", context=32000,
                          supports_tools=True, local=True)]

    async def aclose(self) -> None:
        self.closed = True

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        if self.script:
            for event in self.script:
                yield event
            return

        prompt = req.messages[-1].content if req.messages else ""
        # A scripted tool call: "USE_TOOL:<name> <json args>".
        if req.tools and "USE_TOOL:" in prompt:
            marker = prompt.split("USE_TOOL:", 1)[1].strip()
            name = marker.split()[0] if marker.split() else "get_adapter_status"
            import json

            args: dict = {}
            tail = marker[len(name):].strip()
            if tail:
                try:
                    args = json.loads(tail)
                except ValueError:
                    args = {}
            call = ToolCall(id="c1", name=name, args=args)
            yield call
            yield Usage(input_tokens=estimate_tokens(req.system + prompt),
                        output_tokens=8)
            yield Done(reason="tool_calls", tool_calls=(call,))
            return

        # Default: a deterministic echo, word by word, so SSE is exercised.
        answer = "Offline answer: " + " ".join(prompt.split()[:40])
        for word in answer.split(" "):
            yield Token(word + " ")
        if self.huge_usage:
            yield Usage(input_tokens=10_000_000, output_tokens=10_000_000, usd_estimate=1.0)
        else:
            yield Usage(input_tokens=estimate_tokens(req.system + prompt),
                        output_tokens=estimate_tokens(answer))
        yield Done(reason="stop")

    # Convenience for the wire-level test: a scripted answer in one call.
    @classmethod
    def deterministic(cls, text: str, **kwargs) -> "FakeProvider":
        return cls(script=[Token(piece + " ") for piece in text.split(" ")]
                   + [Usage(input_tokens=max(1, len(text) // 4), output_tokens=len(text) // 4),
                      Done(reason="stop")], **kwargs)
