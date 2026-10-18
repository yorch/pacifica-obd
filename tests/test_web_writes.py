"""Guarded-write route checks: the confirm token, its binding, the typed
acknowledgement, the precondition re-check, the audit trail and ``--read-only``.

    uv run --extra web python tests/test_web_writes.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx  # noqa: F401
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    print("the [web] extra is required: uv run --extra web python tests/test_web_writes.py")
    raise SystemExit(2)

from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

BASE = "http://127.0.0.1:8765"
HEADERS = {"X-Pacifica-Request": "1"}
WRITE = {"module": "744", "bus": "can-c", "did": "F190", "data": "01"}


def make_app(**overrides):
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    return create_app(Settings(demo=True, read_only=False, **overrides))


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


def test_write_did_preview_then_confirm_executes_exactly_once():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/writes/write-did", json=WRITE,
                                     headers=HEADERS)).json()
        assert preview["request"] == "2EF19001"
        assert preview["confirm_token"] and preview["sent"] is False
        assert preview["acknowledgement"] == "744"
        assert adapter(app).writes == []  # preview sent nothing

        result = (await client.post("/api/writes/write-did/confirm",
                                    json={"confirm_token": preview["confirm_token"],
                                          "acknowledgement": "744"}, headers=HEADERS)).json()
        assert result["sent"] is True and result["request"] == "2EF19001"
        assert len(adapter(app).writes) == 1
        assert adapter(app).writes[0]["did"] == "F190"

        reuse = await client.post("/api/writes/write-did/confirm",
                                  json={"confirm_token": preview["confirm_token"],
                                        "acknowledgement": "744"}, headers=HEADERS)
        assert reuse.status_code == 409 and reuse.json()["error"]["status"] == "consumed"
        assert len(adapter(app).writes) == 1  # still exactly one write

    run_async(app, body)


def test_confirm_only_accepts_the_token_and_the_typed_module_id():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/writes/write-did", json=WRITE,
                                     headers=HEADERS)).json()
        wrong = await client.post("/api/writes/write-did/confirm",
                                  json={"confirm_token": preview["confirm_token"],
                                        "acknowledgement": "745"}, headers=HEADERS)
        assert wrong.status_code == 409
        assert wrong.json()["error"]["status"] == "acknowledgement"
        assert adapter(app).writes == []
        # A tampered payload field is simply ignored: the token names the bytes.
        tampered = (await client.post("/api/writes/write-did/confirm",
                                      json={"confirm_token": preview["confirm_token"],
                                            "acknowledgement": "744",
                                            "data": "DEADBEEF", "did": "F1FF"},
                                      headers=HEADERS)).json()
        assert tampered["request"] == "2EF19001"
        assert adapter(app).writes[0]["data"] == "01"

    run_async(app, body)


def test_a_token_is_bound_to_its_operation():
    app = make_app()

    async def body(client):
        routine = (await client.post("/api/writes/routine",
                                     json={"module": "744", "rid": "0203", "sub": 1,
                                           "data": ""}, headers=HEADERS)).json()
        crossed = await client.post("/api/writes/write-did/confirm",
                                    json={"confirm_token": routine["confirm_token"],
                                          "acknowledgement": "744"}, headers=HEADERS)
        assert crossed.status_code == 409
        assert crossed.json()["error"]["status"] == "mismatch"
        assert adapter(app).writes == [] and adapter(app).routines == []

    run_async(app, body)


def test_a_module_without_a_known_response_address_is_refused():
    app = make_app()

    async def body(client):
        response = await client.post("/api/writes/write-did",
                                     json={"module": "7DF", "did": "F190", "data": "01"},
                                     headers=HEADERS)
        assert response.status_code == 403
        error = response.json()["error"]
        assert error["kind"] == "refused"
        assert "response address" in error["message"]
        assert adapter(app).writes == []

    run_async(app, body)


def test_the_precondition_is_rechecked_inside_the_write_job():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/writes/write-did", json=WRITE,
                                     headers=HEADERS)).json()
        assert preview["current"]  # the current value was captured
        adapter(app).vin = "2C4RC1BG7MR999999"  # the ECU's value changed
        response = await client.post("/api/writes/write-did/confirm",
                                     json={"confirm_token": preview["confirm_token"],
                                           "acknowledgement": "744"}, headers=HEADERS)
        assert response.status_code == 409
        assert response.json()["error"]["status"] == "precondition_changed"
        assert adapter(app).writes == [], "a write happened after the value changed"

    run_async(app, body)


def test_routine_preview_and_confirm():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/writes/routine",
                                     json={"module": "744", "rid": "0203", "sub": 3,
                                           "data": "0102"}, headers=HEADERS)).json()
        assert preview["request"] == "310302030102"
        result = (await client.post("/api/writes/routine/confirm",
                                    json={"confirm_token": preview["confirm_token"],
                                          "acknowledgement": "744"}, headers=HEADERS)).json()
        assert result["sent"] is True
        assert adapter(app).routines == [{
            "module": "744", "sub": "03", "routine": "0203", "data": "0102",
            "request": "310302030102", "bus": "can-c"}]

    run_async(app, body)


def test_every_preview_and_send_is_audited():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/writes/write-did", json=WRITE,
                                     headers=HEADERS)).json()
        await client.post("/api/writes/write-did/confirm",
                          json={"confirm_token": preview["confirm_token"],
                                "acknowledgement": "744"}, headers=HEADERS)
        rows = (await client.get("/api/db/events")).json()["rows"]
        kinds = [row["kind"] for row in rows]
        assert "write-preview" in kinds and "write-send" in kinds
        send = next(row for row in rows if row["kind"] == "write-send")
        detail = json.loads(send["detail"])["detail"]
        assert detail["module"] == "744" and detail["request"] == "2EF19001"
        assert detail["sent"] is True and detail["outcome"] == "ok"

    run_async(app, body)


def test_writes_require_the_custom_header():
    app = make_app()

    async def body(client):
        response = await client.post("/api/writes/write-did", json=WRITE)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "missing_request_header"

    run_async(app, body)


def test_a_write_that_never_transmitted_is_not_audited_as_sent():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/writes/write-did", json=WRITE,
                                     headers=HEADERS)).json()
        adapter(app).refuse.add("744")  # extended session now fails
        response = await client.post("/api/writes/write-did/confirm",
                                     json={"confirm_token": preview["confirm_token"],
                                           "acknowledgement": "744"}, headers=HEADERS)
        assert response.status_code in (403, 503)
        rows = (await client.get("/api/db/events")).json()["rows"]
        sends = [row for row in rows if row["kind"] == "write-send"]
        assert sends, "the failed send was not audited"
        for row in sends:
            detail = json.loads(row["detail"])["detail"]
            assert detail["sent"] is not True, detail

    run_async(app, body)


def test_a_token_is_invalidated_by_an_adapter_generation_change():
    app = make_app()

    async def body(client):
        preview = (await client.post("/api/writes/write-did", json=WRITE,
                                     headers=HEADERS)).json()
        # Simulate a disconnect/reconnect (BusWorker generation is per connection).
        app.state.worker.backend.generation += 1
        response = await client.post("/api/writes/write-did/confirm",
                                     json={"confirm_token": preview["confirm_token"],
                                           "acknowledgement": "744"}, headers=HEADERS)
        assert response.status_code == 409
        assert response.json()["error"]["status"] == "generation_mismatch"
        assert adapter(app).writes == []

    run_async(app, body)


def test_a_did_whose_current_value_is_unreadable_is_not_written_blind():
    app = make_app()

    async def body(client):
        # F1FF is answered with a negative response by the fake ECU.
        response = await client.post("/api/writes/write-did",
                                     json={"module": "744", "did": "F1FF", "data": "01"},
                                     headers=HEADERS)
        assert response.status_code == 403
        assert response.json()["error"]["kind"] == "refused"
        assert "blind write" in response.json()["error"]["message"]
        assert adapter(app).writes == []

    run_async(app, body)


def test_a_cancelled_job_does_not_transmit():
    import threading

    from pacifica_obd.web import service

    app = make_app()
    adapter_ = adapter(app)
    cancel = threading.Event()
    cancel.set()
    try:
        service.execute_routine(adapter_, bus="can-c", module="744", rid=0x0203, sub=1,
                                data="", timeout_ms=10, cancel=cancel)
    except service.OperationCancelled:
        pass
    else:
        raise AssertionError("a cancelled job transmitted a routine")
    assert adapter_.routines == []
    app.state.worker.stop()


def test_read_only_removes_the_write_surface():
    app = create_app(Settings(demo=True))
    try:
        paths = set(app.openapi()["paths"])
        assert "/api/writes/write-did" not in paths
        assert "/api/writes/routine/confirm" not in paths

        async def body(client):
            for path in ("/api/writes/write-did", "/api/writes/write-did/confirm",
                         "/api/writes/routine", "/api/writes/routine/confirm"):
                response = await client.post(path, json=WRITE, headers=HEADERS)
                assert response.status_code == 403
                assert response.json()["error"]["kind"] == "readonly"
            assert adapter(app).writes == []

        run_async(app, body)
    finally:
        app.state.worker.stop()


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all guarded-write checks passed")
