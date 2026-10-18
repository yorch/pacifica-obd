"""Security-route checks (0x27): explain/seed/preview/unlock, the lease and the
held-seed policy, the typed acknowledgement and the audit trail.

    uv run --extra web python tests/test_web_security.py

No hardware: the demo ``FakeAdapter`` answers ``27``/``2E``/``31`` and records
every seed request and key so the tests can prove *no second seed request*.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx  # noqa: F401
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    print("the [web] extra is required: uv run --extra web python tests/test_web_security.py")
    raise SystemExit(2)

from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Pacifica-Request": "1"}
RESOLVED = {"module": "744", "level": 1, "bus": "can-c", "variant_id": 64}


def make_app(**overrides):
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    settings = Settings(demo=True, read_only=False, **overrides)
    return create_app(settings)


def run_async(app, fn):
    async def wrapper():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE) as client:
            try:
                return await fn(client)
            finally:
                app.state.worker.stop()

    return asyncio.run(wrapper())


def adapter(app):
    return app.state.worker.backend._adapter


def test_explain_is_offline_and_ambiguity_refuses():
    app = make_app()

    async def body(client):
        resolved = (await client.post("/api/security/explain", json=RESOLVED,
                                      headers=HEADERS)).json()
        assert resolved["status"] == "resolved"
        assert resolved["algorithm"].startswith("CalculateKeyS1_ORC_PN")
        ambiguous = (await client.post("/api/security/explain",
                                       json={"module": "747", "level": 1},
                                       headers=HEADERS)).json()
        assert ambiguous["status"] == "ambiguous"
        assert len(ambiguous["candidates"]) > 1  # the refusal is actionable
        unmapped = (await client.post("/api/security/explain",
                                      json={"module": "7E0", "level": 1},
                                      headers=HEADERS)).json()
        assert unmapped["status"] == "unmapped"
        # Nothing touched the bus for an explanation.
        assert adapter(app).commands == []

    run_async(app, body)


def test_preview_never_sends_and_a_refusal_carries_no_token():
    app = make_app()

    async def body(client):
        refused = (await client.post("/api/security/preview", json={"module": "747", "level": 1},
                                      headers=HEADERS)).json()
        assert refused["status"] == "ambiguous"
        assert refused["confirm_token"] is None and refused["sent"] is False
        assert refused["request"] is None and refused["key"] is None
        # A key is never computed for an unresolved choice, so no seed was read.
        assert adapter(app).seed_requests == []

        preview = (await client.post("/api/security/preview", json=RESOLVED,
                                     headers=HEADERS)).json()
        assert preview["status"] == "preview"
        assert preview["request"] == "27%02X%s" % (RESOLVED["level"] + 1, preview["key"])
        assert preview["confirm_token"]
        assert preview["sent"] is False
        assert len(adapter(app).keys_sent) == 0  # preview sent nothing

    run_async(app, body)


def test_unlock_sends_the_reviewed_key_once_with_no_second_seed_request():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/security/preview", json=RESOLVED,
                                     headers=HEADERS)).json()
        token = preview["confirm_token"]
        assert adapter(app).seed_requests == [("744", "01")]  # exactly one seed

        result = (await client.post("/api/security/unlock",
                                    json={"confirm_token": token, "acknowledgement": "744"},
                                    headers=HEADERS)).json()
        assert result["status"] == "unlocked" and result["unlocked"] is True
        assert result["request"] == preview["request"]
        keys = adapter(app).keys_sent
        assert len(keys) == 1 and keys[0]["key"] == preview["key"]
        # The held-seed policy: the unlock did not ask for a second seed.
        assert adapter(app).seed_requests == [("744", "01")]
        assert result["strict"] is False and result["re_read"] is False

        # And the lease cannot be replayed.
        reuse = await client.post("/api/security/unlock",
                                  json={"confirm_token": token, "acknowledgement": "744"},
                                  headers=HEADERS)
        assert reuse.status_code == 409
        assert reuse.json()["error"]["status"] == "consumed"
        assert len(adapter(app).keys_sent) == 1

    run_async(app, body)


def test_a_wrong_typed_acknowledgement_sends_nothing():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/security/preview", json=RESOLVED,
                                     headers=HEADERS)).json()
        response = await client.post("/api/security/unlock",
                                     json={"confirm_token": preview["confirm_token"],
                                           "acknowledgement": "745"},
                                     headers=HEADERS)
        assert response.status_code == 409
        assert response.json()["error"]["status"] == "acknowledgement"
        assert adapter(app).keys_sent == []
        # The still-valid lease can be used with the right acknowledgement.
        ok = (await client.post("/api/security/unlock",
                                json={"confirm_token": preview["confirm_token"],
                                      "acknowledgement": "744"}, headers=HEADERS)).json()
        assert ok["unlocked"] is True

    run_async(app, body)


def test_strict_mode_rereads_and_aborts_when_the_seed_changed():
    app = make_app(strict_seed_reread=True)

    async def body(client):
        preview = (await client.post("/api/security/preview", json=RESOLVED,
                                     headers=HEADERS)).json()
        # The ECU now presents a different seed (a real module may do this).
        adapter(app).seeds["744"] = "DEADBEEF"
        response = await client.post("/api/security/unlock",
                                     json={"confirm_token": preview["confirm_token"],
                                           "acknowledgement": "744"}, headers=HEADERS)
        assert response.status_code == 409
        assert response.json()["error"]["status"] == "seed_changed"
        assert adapter(app).keys_sent == [], "a key was sent after a seed change"
        # Strict mode re-read the seed: one at preview, one at confirm.
        assert adapter(app).seed_requests == [("744", "01"), ("744", "01")]

    run_async(app, body)


def test_strict_mode_sends_when_the_seed_is_unchanged():
    app = make_app(strict_seed_reread=True)

    async def body(client):
        preview = (await client.post("/api/security/preview", json=RESOLVED,
                                     headers=HEADERS)).json()
        result = (await client.post("/api/security/unlock",
                                    json={"confirm_token": preview["confirm_token"],
                                          "acknowledgement": "744"}, headers=HEADERS)).json()
        assert result["unlocked"] is True and result["re_read"] is True
        assert adapter(app).seed_requests == [("744", "01"), ("744", "01")]

    run_async(app, body)


def test_audit_events_record_the_preview_and_the_send():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/security/preview", json=RESOLVED,
                                     headers=HEADERS)).json()
        await client.post("/api/security/unlock",
                          json={"confirm_token": preview["confirm_token"],
                                "acknowledgement": "744"}, headers=HEADERS)
        events = (await client.get("/api/db/events")).json()["rows"]
        by_kind = {}
        for row in events:
            by_kind.setdefault(row["kind"], []).append(row)
        assert "security-preview" in by_kind and "security-send" in by_kind
        send = by_kind["security-send"][0]
        assert "744" in (send["name"] or "")
        import json

        detail = json.loads(send["detail"])["detail"]
        assert detail["module"] == "744" and detail["sent"] is True
        assert detail.get("key_bytes") == 4  # the key itself is never recorded
        assert "E0E2DC58" not in send["detail"]  # never the key material

    run_async(app, body)


def test_a_second_preview_supersedes_the_first_token():
    app = make_app()

    async def body(client):
        first = (await client.post("/api/security/preview", json=RESOLVED,
                                   headers=HEADERS)).json()
        second = (await client.post("/api/security/preview", json=RESOLVED,
                                    headers=HEADERS)).json()
        assert first["confirm_token"] != second["confirm_token"]
        stale = await client.post("/api/security/unlock",
                                  json={"confirm_token": first["confirm_token"],
                                        "acknowledgement": "744"}, headers=HEADERS)
        assert stale.status_code == 409
        assert stale.json()["error"]["status"] == "superseded"
        assert adapter(app).keys_sent == []
        ok = (await client.post("/api/security/unlock",
                                json={"confirm_token": second["confirm_token"],
                                      "acknowledgement": "744"}, headers=HEADERS)).json()
        assert ok["unlocked"] is True

    run_async(app, body)


def test_unlock_requires_the_custom_header_and_is_disabled_in_read_only():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/security/preview", json=RESOLVED,
                                     headers=HEADERS)).json()
        no_header = await client.post("/api/security/unlock",
                                      json={"confirm_token": preview["confirm_token"],
                                            "acknowledgement": "744"})
        assert no_header.status_code == 403
        assert no_header.json()["error"]["code"] == "missing_request_header"

    run_async(app, body)

    read_only = create_app(Settings(demo=True))
    try:
        async def body2(client):
            response = await client.post("/api/security/unlock", json={}, headers=HEADERS)
            assert response.status_code == 403
            assert response.json()["error"]["kind"] == "readonly"
            # Explain still works without the write surface.
            assert (await client.post("/api/security/explain", json=RESOLVED,
                                      headers=HEADERS)).status_code == 200

        run_async(read_only, body2)
    finally:
        read_only.state.worker.stop()


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all security-route checks passed")
