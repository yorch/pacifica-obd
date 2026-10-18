"""AI HTTP checks: config/keys privacy, the SSE chat path with the fake provider,
the remote-context refusal, VIN redaction, and one real end-to-end HTTP call
through a running server (no network, no key).

    uv run --extra web python tests/test_web_ai_api.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx  # noqa: F401
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    print("the [web] extra is required: uv run --extra web python tests/test_web_ai_api.py")
    raise SystemExit(2)

from pacifica_obd.store import Store  # noqa: E402
from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.ai.config import AIConfig  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Pacifica-Request": "1"}
FAKE_ENV = {"PACIFICA_OBD_AI_PROVIDER": "fake", "PACIFICA_OBD_AI_MODEL": "fake-1"}


def _set_env(extra=None):
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    for key, value in FAKE_ENV.items():
        os.environ[key] = value
    for key, value in (extra or {}).items():
        os.environ[key] = value


def _clear_ai_env():
    for key in ("PACIFICA_OBD_AI_PROVIDER", "PACIFICA_OBD_AI_MODEL",
                "PACIFICA_OBD_AI_BASE_URL", "PACIFICA_OBD_AI_OFFLINE"):
        os.environ.pop(key, None)


def make_app(**overrides):
    _set_env()
    return create_app(Settings(demo=True, **overrides))


def run_async(app, fn):
    async def wrapper():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE) as client:
            try:
                return await fn(client)
            finally:
                app.state.worker.stop()

    return asyncio.run(wrapper())


async def _sse(client, path, payload):
    events = []
    async with client.stream("POST", path, json=payload, headers=HEADERS) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers.get("cache-control") == "no-cache"
        kind = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and kind:
                import json

                events.append((kind, json.loads(line[5:].strip())))
                kind = None
    return events


def test_config_defaults_are_privacy_first_and_keys_are_write_only():
    app = make_app()

    async def body(client):
        config = (await client.get("/api/ai/config")).json()
        assert config["redact_vin"] is True
        assert config["send_bus_data_to_remote"] is False
        assert config["persist_conversations"] is False
        assert config["allow_live_bus"] is False
        assert config["cost_table_date"]

        secret = "sk-this-must-never-come-back-9876"
        set_response = await client.post("/api/ai/keys",
                                         json={"provider": "openai_compat", "key": secret},
                                         headers=HEADERS)
        assert set_response.status_code == 200
        assert secret not in set_response.text
        assert set_response.json()["key"]["hint"].endswith("9876")

        config = (await client.get("/api/ai/config")).json()
        assert config["keys"]["openai_compat"]["configured"] is True
        assert secret not in (await client.get("/api/ai/config")).text
        providers = (await client.get("/api/ai/providers")).json()["providers"]
        assert any(row["id"] == "openai_compat" for row in providers)

        patched = await client.put("/api/ai/config",
                                   json={"temperature": 5.0}, headers=HEADERS)
        assert patched.status_code == 400  # invalid values are reported, not stored
        ok = await client.put("/api/ai/config",
                              json={"provider": "fake", "model": "fake-1", "redact_vin": False},
                              headers=HEADERS)
        assert ok.status_code == 200 and ok.json()["redact_vin"] is False
        removed = await client.delete("/api/ai/keys/openai_compat", headers=HEADERS)
        assert removed.status_code == 200 and removed.json()["removed"] is True
        # The chat/key/config routes also require the custom header (CSRF).
        assert (await client.post("/api/ai/chat", json={"message": "hi"})).status_code == 403

    run_async(app, body)
    _set_env()


def test_chat_streams_tokens_usage_and_done_from_the_fake_provider():
    app = make_app()

    async def body(client):
        events = await _sse(client, "/api/ai/chat", {"message": "What does P0300 mean?"})
        kinds = [kind for kind, _data in events]
        assert kinds[0] == "meta" and kinds[-1] == "done"
        assert "token" in kinds and "usage" in kinds
        meta = events[0][1]
        assert meta["provider"] == "fake" and meta["cost_estimate"] is True
        text = "".join(data["text"] for kind, data in events if kind == "token")
        assert "Offline answer:" in text
        done = events[-1][1]
        assert done["conversation_id"]
        usage = next(data for kind, data in events if kind == "usage")
        assert usage["total_tokens"] >= 0 and usage["usd_estimate"] == 0.0  # local/unknown

    run_async(app, body)
    _set_env()


def test_tool_loop_through_the_api_is_read_only_and_fenced():
    app = make_app()

    async def body(client):
        events = await _sse(client, "/api/ai/chat",
                            {"message": "USE_TOOL: read_generic_dtcs {}",
                             "tools": True})
        kinds = [kind for kind, _data in events]
        assert "tool_call" in kinds and "tool_result" in kinds
        call = next(data for kind, data in events if kind == "tool_call")
        assert call["name"] == "read_generic_dtcs"
        result = next(data for kind, data in events if kind == "tool_result")
        assert result["untrusted"] is True and result["source"] == "vehicle-bus"
        # No write tool can be reached through the API.
        events = await _sse(client, "/api/ai/chat",
                            {"message": "USE_TOOL: write_did {}", "tools": True})
        result = next(data for kind, data in events if kind == "tool_result")
        assert "unknown tool" in result["summary"]

    run_async(app, body)
    _set_env()


def test_remote_provider_without_consent_refuses_vehicle_tasks():
    _set_env()
    _clear_ai_env()
    AIConfig(provider="openai_compat", model="gpt-example",
             base_url="https://api.example.invalid/v1",
             send_bus_data_to_remote=False, redact_vin=True).save()
    app = create_app(Settings(demo=True))

    async def body(client):
        response = await client.post("/api/ai/tasks/diagnose", json={}, headers=HEADERS)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "remote_context_not_allowed"
        # The tool loop must be refused too: a cache-first tool still returns the
        # vehicle's stored data.
        response = await client.post("/api/ai/chat", json={"message": "hi", "tools": True},
                                     headers=HEADERS)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "remote_context_not_allowed"
        # Plain chat with no vehicle data can still run (once a key is configured).
        key = await client.post("/api/ai/keys",
                                json={"provider": "openai_compat", "key": "sk-x-1234567890"},
                                headers=HEADERS)
        assert key.status_code == 200
        async with client.stream("POST", "/api/ai/chat", json={"message": "hello"},
                                 headers=HEADERS) as response:
            assert response.status_code == 200
            raw = (await response.aread()).decode()
        assert '"context": "withheld"' in raw
        # Force-local-only mode refuses any remote provider outright.
        AIConfig(provider="openai_compat", model="gpt-example",
                 base_url="https://api.example.invalid/v1", force_local_only=True).save()
        response = await client.post("/api/ai/chat", json={"message": "hi"}, headers=HEADERS)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "local_only"

    run_async(app, body)
    _set_env()


def test_vin_is_redacted_in_the_egress_preview():
    app = make_app()

    async def body(client):
        config = AIConfig(provider="fake", model="fake-1", redact_vin=True)
        config.save()
        vin = "2C4RC1BG7MR000001"
        preview = (await client.get("/api/ai/what-will-be-sent",
                                    params={"message": f"my vin is {vin}"})).json()
        assert vin not in preview["user"] and "redacted" in preview["user"]
        assert vin not in preview["system"]
        assert preview["local"] is True

    run_async(app, body)
    _set_env()


def test_the_provider_error_message_never_leaks_a_key():
    _set_env()
    AIConfig(provider="openai_compat", model="gpt-x",
             base_url="https://api.example.invalid/v1").save()
    os.environ["PACIFICA_OBD_AI_BASE_URL"] = "https://api.example.invalid/v1"
    from pacifica_obd.web import errors

    from pacifica_obd.web.app import _ai_redactor

    store = __import__("pacifica_obd.web.ai.keys", fromlist=["KeyStore"]).KeyStore()
    # Ensure a literal key is redacted by the app's error path.
    store.set("openai_compat", "sk-literal-secret-1234567890")
    redact = _ai_redactor()
    assert "secret" not in redact("boom sk-literal-secret-1234567890 boom")
    assert errors.from_exception(ValueError("sk-literal-secret-1234567890")).message


def test_real_http_end_to_end_with_the_fake_provider():
    """One call over a real socket through uvicorn, not just the ASGI transport."""
    import uvicorn

    _set_env()
    app = create_app(Settings(demo=True))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error",
                            access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            with client.stream("POST", "/api/ai/chat", json={"message": "hello over http"},
                               headers=HEADERS) as response:
                assert response.status_code == 200
                body = "".join(response.iter_text())
        assert "event: meta" in body
        assert "event: token" in body and '"text": "Offline "' in body
        assert "event: done" in body
        # The usage was recorded against the local store (no content, no keys).
        store = Store()
        try:
            rows = store._query("SELECT provider, model, input_tokens, output_tokens"
                                " FROM ai_usage ORDER BY id DESC LIMIT 1")
        finally:
            store.close()
        assert rows and rows[0]["provider"] == "fake"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    _set_env()


def test_persistence_is_off_by_default_and_opt_in():
    app = make_app()

    async def body(client):
        await _sse(client, "/api/ai/chat", {"message": "remember nothing"})
        listing = (await client.get("/api/ai/conversations")).json()
        assert listing["available"] is True and listing["conversations"] == []

        AIConfig(provider="fake", model="fake-1", persist_conversations=True).save()
        events = await _sse(client, "/api/ai/chat", {"message": "remember this one"})
        conversation_id = events[-1][1]["conversation_id"]
        listing = (await client.get("/api/ai/conversations")).json()
        assert any(row["id"] == conversation_id for row in listing["conversations"])
        detail = (await client.get(f"/api/ai/conversations/{conversation_id}")).json()
        assert detail["messages"][0]["role"] == "user"

    run_async(app, body)
    _set_env()


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all AI HTTP checks passed")
