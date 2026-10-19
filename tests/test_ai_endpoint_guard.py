"""A stored key may only ever be sent to the saved endpoint.

The connection test accepts a *candidate* configuration so a user can try a
provider before saving it. That is convenient and dangerous in exactly one case:
the server attaches the stored key for the provider, so a caller-supplied base URL
would turn "test my config" into "send my key wherever I say". The test refuses to
probe an endpoint that differs from the saved one **while a key is stored**; a
keyless endpoint (a local model) can still be probed before saving.

    uv run --extra web python tests/test_ai_endpoint_guard.py
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

from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

HEADERS = {"X-Pacifica-Request": "1"}
SAVED = "https://api.anthropic.com"
EVIL = "https://attacker.example.com/v1"


def make_app():
    """A clean home, with the ambient AI env cleared so the test owns the config."""
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    for key in ("PACIFICA_OBD_AI_PROVIDER", "PACIFICA_OBD_AI_MODEL",
                "PACIFICA_OBD_AI_BASE_URL", "PACIFICA_OBD_AI_OFFLINE",
                "ANTHROPIC_API_KEY", "PACIFICA_OBD_AI_KEY_ANTHROPIC"):
        os.environ.pop(key, None)
    return create_app(Settings(demo=True))


def run_async(app, fn):
    async def wrapper():
        # the app refuses a foreign Host header (bind is loopback by default), so
        # the client must look like a local one, exactly as a browser would
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://127.0.0.1:8765") as client:
            return await fn(client)

    return asyncio.run(wrapper())


async def _configure_anthropic(client) -> None:
    """Save Anthropic as the provider and give it a key — the owner's setup."""
    saved = await client.put("/api/ai/config", headers=HEADERS, json={
        "provider": "anthropic", "model": "claude-3-5-sonnet-latest",
        "base_url": SAVED})
    assert saved.status_code == 200, saved.text
    key = await client.post("/api/ai/keys", headers=HEADERS, json={
        "provider": "anthropic", "key": "sk-ant-test-0123456789abcdef"})
    assert key.status_code == 200, key.text


def test_an_unsaved_endpoint_is_refused_while_a_key_is_stored():
    app = make_app()

    async def body(client):
        await _configure_anthropic(client)
        config = (await client.get("/api/ai/config")).json()
        assert config["provider"] == "anthropic", config
        assert config["effective_base_url"] == SAVED, config["effective_base_url"]

        response = await client.post("/api/ai/test", headers=HEADERS, json={
            "provider": "anthropic", "base_url": EVIL, "probe": True})
        payload = response.json()
        assert payload["ok"] is False, payload
        assert payload["error"]["code"] == "endpoint_not_saved", payload
        assert any("save" in c["detail"].lower() for c in payload["checks"]), payload["checks"]

    run_async(app, body)


def test_the_saved_endpoint_still_passes_the_guard():
    app = make_app()

    async def body(client):
        await _configure_anthropic(client)
        response = await client.post("/api/ai/test", headers=HEADERS, json={
            "provider": "anthropic", "base_url": SAVED, "probe": True})
        payload = response.json()
        # the probe itself may fail for network reasons; the guard must not fire
        assert (payload.get("error") or {}).get("code") != "endpoint_not_saved", payload

    run_async(app, body)


def test_a_keyless_endpoint_can_still_be_probed_before_saving():
    """A local model has no credential to leak, so trying one first stays allowed."""
    app = make_app()

    async def body(client):
        response = await client.post("/api/ai/test", headers=HEADERS, json={
            "provider": "ollama", "base_url": "http://127.0.0.1:1/v1", "probe": True})
        payload = response.json()
        assert (payload.get("error") or {}).get("code") != "endpoint_not_saved", payload

    run_async(app, body)


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all AI endpoint-guard checks passed")
