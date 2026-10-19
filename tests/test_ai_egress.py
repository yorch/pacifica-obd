"""Egress: structured values are scrubbed before serialisation, and the history
that is replayed to the provider is scrubbed too.

Adversarial review findings covered here:
* JSON escaping defeated literal-key redaction (`"` -> `\\"`), so scrubbing only the
  serialised tool payload missed a key containing a quote or a backslash;
* the assistant's own text and tool-call metadata were sent back to the provider
  unscrubbed on the next round;
* switching provider on the settings form carried the previous endpoint over, so
  the new provider's stored key could be sent to a URL approved for the old one;
* `/api/raw/confirm` was not structurally excluded in read-only mode.

    uv run --extra web python tests/test_ai_egress.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from pacifica_obd.web.ai import chat as chat_mod  # noqa: E402
from pacifica_obd.web.ai.config import AIConfig  # noqa: E402
from pacifica_obd.web.ai.providers import Done, Message, Token, ToolCall  # noqa: E402
from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

HEADERS = {"X-Pacifica-Request": "1"}
#: A key with a quote and a backslash: JSON escaping rewrites both.
ODD_KEY = 'proxy"token\\secret-0123456789'


def _app(**overrides):
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    return create_app(Settings(demo=True, **overrides))


def _run(app, fn):
    async def wrapper():
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://127.0.0.1:8765") as client:
            return await fn(client)

    return asyncio.run(wrapper())


# --------------------------------------------------------------------------- #
# 1. escaping must not defeat redaction
# --------------------------------------------------------------------------- #
def test_a_fenced_payload_cannot_carry_an_escaped_literal():
    engine = chat_mod.ChatEngine(None, config=AIConfig(provider="fake", model="fake-1"))
    engine._keys = (ODD_KEY,)
    fenced = engine._fenced("read_did", {
        "data": {"value": ODD_KEY, "nested": [{"k": ODD_KEY}]},
        "summary": f"read back {ODD_KEY}", "source": "local-db"})
    assert ODD_KEY not in fenced, fenced
    # …and not in the escaped form JSON serialisation would produce
    assert 'proxy\\"token' not in fenced, fenced
    assert "proxy" not in fenced or "redacted" in fenced, fenced


def test_the_evidence_pack_cannot_carry_an_escaped_literal():
    from pacifica_obd.web.ai import prompts

    def scrubber(value):
        from pacifica_obd.web.ai import redact

        return redact.scrub_obj(value, keys=(ODD_KEY,))

    text, _ids = prompts.render_evidence(
        [{"id": "E1", "label": "did", "tool": "read_did", "source": "local-db",
          "data": {"value": ODD_KEY}}], scrubber=scrubber)
    assert ODD_KEY not in text, text
    assert 'proxy\\"' not in text, text


# --------------------------------------------------------------------------- #
# 3. the history replayed to the provider is scrubbed
# --------------------------------------------------------------------------- #
def test_assistant_text_and_tool_metadata_are_scrubbed_before_replay():
    seen: list[list[Message]] = []

    class LeakyProvider:
        id = "fake"
        label = "fake"

        async def stream(self, request):
            seen.append(list(request.messages))
            if len(seen) == 1:
                yield Token(text=f"the key is {ODD_KEY} ")
                yield ToolCall(id=f"call-{ODD_KEY}", name="read_did",
                               args={"did": f"F190-{ODD_KEY}"})
                yield Done(reason="tool_calls", tool_calls=(
                    ToolCall(id=f"call-{ODD_KEY}", name="read_did",
                             args={"did": f"F190-{ODD_KEY}"}),))
            else:
                yield Done(reason="stop")

    class Backend:
        async def dispatch(self, name, args):
            return {"data": {"ok": True}, "summary": "ok", "source": "local-db"}

    engine = chat_mod.ChatEngine(LeakyProvider(),
                                 config=AIConfig(provider="fake", model="fake-1"),
                                 backend=Backend(), tools_enabled=True)
    engine._keys = (ODD_KEY,)

    async def drain():
        return [event async for event in engine.stream(messages=[
            Message(role="user", content="go")])]

    events = asyncio.run(drain())
    assert len(seen) == 2, "the tool round should have produced a second request"
    replay = repr(seen[1])
    assert ODD_KEY not in replay, replay[:400]
    # the tool result still matches the (scrubbed) call id, so the provider does
    # not see an unanswered tool_use
    assert "call-" in replay


# --------------------------------------------------------------------------- #
# 2. a provider switch must not inherit the previous endpoint
# --------------------------------------------------------------------------- #
def test_switching_provider_resets_the_endpoint_and_model():
    app = _app()

    async def body(client):
        saved = await client.put("/api/ai/config", headers=HEADERS, json={
            "provider": "openai_compat", "model": "custom-model",
            "base_url": "https://proxy.example/v1"})
        assert saved.status_code == 200, saved.text
        # switch without supplying a URL or model
        switched = await client.put("/api/ai/config", headers=HEADERS,
                                    json={"provider": "openai"})
        assert switched.status_code == 200, switched.text
        payload = switched.json()
        assert payload["provider"] == "openai"
        assert payload["base_url"] != "https://proxy.example/v1", payload["base_url"]
        assert payload["model"] != "custom-model", payload["model"]

    _run(app, body)


# --------------------------------------------------------------------------- #
# 4. read-only cannot reach the raw confirm tier
# --------------------------------------------------------------------------- #
def test_read_only_cannot_reach_raw_confirm_even_with_a_token():
    app = _app(read_only=True)

    async def body(client):
        # mint a token the way a write-mode preview would; the route must still be
        # unreachable
        entry = app.state.confirm.issue(kind="raw", module="7E0", bus="can-c",
                                        request="ATSTFF", ack="ATSTFF",
                                        params={"command": "ATSTFF",
                                                "generation": "0"})
        response = await client.post("/api/raw/confirm", headers=HEADERS,
                                     json={"confirm_token": entry.token,
                                           "acknowledgement": "ATSTFF"})
        assert response.status_code == 403, response.text
        assert response.json()["error"]["kind"] == "readonly", response.json()
        assert getattr(app.state.worker.backend, "commands", []) == []

    _run(app, body)


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all AI egress / switch checks passed")
