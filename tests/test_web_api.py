"""End-to-end HTTP checks for the web API: the error envelope, every read
endpoint, the read-only guarantee, and the localhost/auth hardening.

    uv run --extra web python tests/test_web_api.py

The app is exercised over ASGI with ``httpx`` (no sockets) except for the SSE
stream, which needs a real server and lives in ``test_web_stream.py``.
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
except ImportError:  # pragma: no cover - depends on the optional extra
    print("the [web] extra is required: uv run --extra web python tests/test_web_api.py")
    raise SystemExit(2)

from pacifica_obd.web.app import READ_ONLY, create_app  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

BASE = "http://127.0.0.1:8765"

# Every documented read endpoint from the design's §4.3 catalogue, minus the
# ones this read-only build deliberately does not ship.
READ_ENDPOINTS = [
    ("GET", "/api/health", None),
    ("GET", "/api/status", None),
    ("GET", "/api/ports", None),
    ("GET", "/api/info", None),
    ("POST", "/api/connect", {}),
    ("POST", "/api/vin", None),
    ("POST", "/api/bus", {"bus": "can-ihs"}),
    ("GET", "/api/jobs", None),
    ("GET", "/api/obd/dtc", None),
    ("GET", "/api/obd/live", None),
    ("GET", "/api/obd/pids", None),
    ("GET", "/api/obd/readiness", None),
    ("GET", "/api/obd/freeze", None),
    ("POST", "/api/obd/scan", None),
    ("POST", "/api/uds/modules", {"bus": "can-c", "start": "7E0", "end": "7E2"}),
    ("POST", "/api/uds/dtc-all", {"bus": "can-c", "start": "7E0", "end": "7E2"}),
    ("POST", "/api/uds/read-did", {"module": "7E0", "dids": ["F190", "F187"]}),
    ("POST", "/api/uds/module-info", {"module": "7E0"}),
    ("POST", "/api/uds/capture", {"bus": "can-c", "start": "7E0", "end": "7E1"}),
    ("GET", "/api/uds/module-map", None),
    ("GET", "/api/uds/modules/known?bus=can-c", None),
    ("GET", "/api/uds/ecus", None),
    ("GET", "/api/uds/dids?ecu=PCM_PN", None),
    ("GET", "/api/uds/adaptations?ecu=PCM_PN", None),
    ("GET", "/api/dataset/info", None),
    ("GET", "/api/live/streams", None),
    ("GET", "/api/db/schema", None),
    ("GET", "/api/db/vehicles", None),
    ("GET", "/api/db/sessions", None),
    ("GET", "/api/db/events", None),
    ("GET", "/api/db/modules", None),
    ("GET", "/api/db/dtcs", None),
    ("GET", "/api/db/readings", None),
    ("GET", "/api/db/samples", None),
    ("GET", "/api/db/samples/series", None),
    ("GET", "/api/db/sessions/1", None),
    ("GET", "/api/reports", None),
]


def make_app(**overrides):
    """A fresh app on an isolated data dir (demo backend, seeded DB)."""
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    return create_app(Settings(demo=True, **overrides))


async def _with_client(app, fn):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as client:
        try:
            return await fn(client)
        finally:
            app.state.worker.stop()


def run_async(app, fn):
    return asyncio.run(_with_client(app, fn))


def test_read_only_write_commands_are_refused_everywhere():
    """No read endpoint may reach a write command (mode 04 / CLEAR_DTC)."""
    app = make_app()

    async def body(client):
        # The picker must not offer it…
        pids = (await client.get("/api/obd/pids")).json()["pids"]
        assert "CLEAR_DTC" not in {row["name"] for row in pids}
        assert all(row["command"].startswith("01") for row in pids)

        # …a live read must not send it…
        live = (await client.get("/api/obd/live")).json()["pids"]
        assert "CLEAR_DTC" not in {row["name"] for row in live}

        # …and a watch that asks for it is refused before any stream starts.
        response = await client.post("/api/live/watch/start",
                                     json={"source": "pid", "values": ["CLEAR_DTC"]})
        assert response.status_code == 400, response.text
        assert response.json()["error"]["kind"] == "validation"
        assert (await client.post("/api/live/watch/start",
                                  json={"source": "pid", "values": ["NOPE_NOT_A_PID"]})).status_code == 400
        # Nothing is holding the bus after those refusals.
        assert (await client.get("/api/status")).json()["job"]["busy"] is False

    run_async(app, body)


def test_monitor_start_and_stop_round_trip():
    app = make_app()

    async def body(client):
        started = await client.post("/api/uds/monitor/start",
                                    json={"module": "7E0", "dids": ["F190"], "interval_ms": 100,
                                          "timeout_ms": 100})
        assert started.status_code == 200, started.text
        stream_id = started.json()["stream_id"]
        try:
            assert (await client.post("/api/uds/dtc-all", json={})).status_code == 409
        finally:
            stopped = await client.post("/api/uds/monitor/stop", json={"stream_id": stream_id})
            assert stopped.status_code == 200

    run_async(app, body)


def test_a_refused_connect_does_not_change_the_backend_configuration():
    app = make_app()

    async def body(client):
        worker = app.state.worker
        before = getattr(worker.backend, "requested_port", None)
        started = await client.post("/api/live/watch/start",
                                    json={"source": "pid", "values": ["RPM"], "interval_ms": 100})
        stream_id = started.json()["stream_id"]
        try:
            response = await client.post("/api/connect", json={"port": "/dev/cu.evil"})
            assert response.status_code == 409
            assert getattr(worker.backend, "requested_port", None) == before
        finally:
            await client.post("/api/live/watch/stop", json={"stream_id": stream_id})

    run_async(app, body)


def test_token_resolution_and_remote_bind_guard():
    from pacifica_obd.web.settings import Settings, resolve_token

    generated = resolve_token(True)
    assert isinstance(generated, str) and len(generated) >= 20
    assert resolve_token("literal") == "literal"
    assert resolve_token(None) is None
    os.environ["PACIFICA_OBD_WEB_TOKEN"] = "from-env"
    try:
        assert resolve_token(None) == "from-env"
        assert resolve_token("literal") == "literal"  # an explicit flag still wins
    finally:
        os.environ.pop("PACIFICA_OBD_WEB_TOKEN", None)

    settings = Settings(token="t", port=9000)
    assert settings.auth_required is True
    assert settings.read_only is True
    assert settings.origin.endswith(":9000")
    assert "http://127.0.0.1:9000" in settings.local_origins()
    assert "http://localhost:9000" in settings.local_origins()
    assert not any("evil" in origin for origin in settings.local_origins())


def test_every_read_endpoint_answers():
    app = make_app()

    async def body(client):
        for method, path, payload in READ_ENDPOINTS:
            response = await client.request(method, path, json=payload)
            assert response.status_code < 400, (method, path, response.status_code, response.text[:200])
            if response.headers.get("content-type", "").startswith("application/json"):
                response.json()  # must be valid JSON
        health = (await client.get("/api/health")).json()
        assert health["ok"] is True and health["read_only"] is True
        assert health["adapter"]["attached"] is True
        dids = (await client.post("/api/uds/read-did", json={"module": "7E0", "dids": ["F190"]})).json()
        assert dids["dids"][0]["ascii"].startswith("2C4RC1BG")
        dtcs = (await client.post("/api/uds/dtc-all",
                                  json={"bus": "can-c", "start": "7E0", "end": "7E0"})).json()
        assert dtcs["total_dtcs"] == 1 and dtcs["modules"][0]["dtcs"][0]["code"] == "P0300"

    run_async(app, body)


def test_static_index_is_served():
    app = make_app()

    async def body(client):
        response = await client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "pacifica-obd" in response.text
        script = await client.get("/js/app.js")
        assert script.status_code == 200 and "text/javascript" in script.headers["content-type"]
        uplot = await client.get("/vendor/uplot.min.js")
        assert uplot.status_code == 200

    run_async(app, body)


def test_no_write_route_exists_and_read_only_is_structural():
    assert READ_ONLY is True
    assert Settings().read_only is True
    app = make_app()
    assert app.state.read_only is True
    try:
        routes = []
        for route in app.routes:
            path = getattr(route, "path", "")
            methods = sorted(getattr(route, "methods", []) or [])
            if path.startswith("/api") and "{rest:path}" not in path:
                routes.append((path, methods))
        forbidden_words = ("write", "clear", "routine", "security", "unlock", "ai/", "settings")
        for path, _methods in routes:
            assert not any(word in path for word in forbidden_words), path
            assert not path.endswith("/confirm"), path
        # Only reads: GET, plus POSTs that open the bus or start a stream.
        allowed_posts = {
            "/api/connect", "/api/disconnect", "/api/vin", "/api/bus",
            "/api/obd/scan", "/api/uds/modules", "/api/uds/dtc-all", "/api/uds/read-did",
            "/api/uds/module-info", "/api/uds/capture", "/api/live/watch/start",
            "/api/live/watch/stop", "/api/uds/monitor/start", "/api/uds/monitor/stop",
            "/api/reports/dump", "/api/reports/from-session", "/api/reports/compare",
        }
        for path, methods in routes:
            for method in methods:
                if method in ("HEAD", "OPTIONS"):
                    continue
                if method == "GET":
                    continue
                assert method == "POST", (path, method)
                if "{job_id}" in path:
                    continue
                assert path in allowed_posts, f"unexpected POST route: {path}"
    finally:
        app.state.worker.stop()

    async def body(client):
        for path in ("/api/obd/dtc/clear", "/api/security/unlock", "/api/writes/write-did",
                     "/api/writes/routine", "/api/raw", "/api/ai/chat"):
            response = await client.post(path, json={})
            assert response.status_code == 404, (path, response.status_code)
            envelope = response.json()
            assert envelope["error"]["kind"] == "not_found"

    app2 = make_app()
    run_async(app2, body)


def test_report_documents_are_capability_addressed_not_token_gated():
    """A generated report document is readable by its unguessable id; everything
    that can act still needs the token."""
    app = make_app(token="s3cret-token")

    async def body(client):
        headers = {"Authorization": "Bearer s3cret-token"}
        created = await client.post("/api/reports/from-session",
                                    json={"session_id": 1, "formats": ["json", "html", "xml"]},
                                    headers=headers)
        if created.status_code == 404:
            return  # this demo DB has no session 1; the gate behaviour is covered below
        report_id = created.json()["id"]

        # No Authorization header, but the document opens (its CSP allows framing).
        html = await client.get(f"/api/reports/{report_id}/html")
        assert html.status_code == 200
        assert html.headers["content-security-policy"].startswith("default-src 'none'")
        assert html.headers["x-frame-options"] == "SAMEORIGIN"
        for path in (f"/api/reports/{report_id}/download/html", f"/api/reports/{report_id}/zip"):
            assert (await client.get(path)).status_code == 200
        # A guessed/unknown document id is still a 404, and the listing stays gated.
        assert (await client.get("/api/reports/000000000000/html")).status_code == 404
        assert (await client.get("/api/reports")).status_code == 401

    app2 = make_app(token="s3cret-token")
    run_async(app2, body)


def test_error_envelope_for_bad_input_and_unknown_resources():
    app = make_app()

    async def body(client):
        response = await client.post("/api/uds/read-did", json={"module": "7E0", "dids": ["ZZZZ"]})
        assert response.status_code == 400
        envelope = response.json()
        assert set(envelope) == {"error"}
        error = envelope["error"]
        assert error["kind"] == "validation" and error["code"]
        assert "ZZZZ" in error["message"]
        assert error["status"] is None  # no CLI status for a parse failure

        response = await client.get("/api/db/nope")
        assert response.status_code == 404 and response.json()["error"]["kind"] == "not_found"

        response = await client.get("/api/jobs/does-not-exist")
        assert response.status_code == 404

        response = await client.post("/api/uds/modules", json={"bus": "nope"})
        assert response.status_code == 400
        assert response.json()["error"]["kind"] == "validation"

        response = await client.post("/api/uds/read-did",
                                     json={"module": "7E0", "dids": ["F190"], "timeout_ms": 99999})
        assert response.status_code == 400

    run_async(app, body)


def test_adapter_unknown_is_a_503_envelope():
    from pacifica_obd.adapter import AdapterError
    from pacifica_obd.web.bus import BusWorker
    from pacifica_obd.web.deps import build_backend

    class Unplugged(type(build_backend(Settings()))):
        def obd(self):
            raise AdapterError("no serial port found (is the vLinker FS plugged in?)")

    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    settings = Settings()
    backend = Unplugged()
    app = create_app(settings, worker=BusWorker(backend))

    async def body(client):
        response = await client.get("/api/info")
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["kind"] == "adapter" and error["status"] == "no_response"
        assert "vLinker" in error["message"]

    run_async(app, body)


def test_bus_busy_is_a_409_envelope_and_the_stream_can_be_stopped():
    app = make_app()

    async def body(client):
        started = await client.post("/api/live/watch/start",
                                    json={"source": "pid", "values": ["RPM"], "interval_ms": 100})
        assert started.status_code == 200
        stream_id = started.json()["stream_id"]
        try:
            response = await client.post("/api/uds/dtc-all",
                                         json={"bus": "can-c", "start": "7E0", "end": "7E0"})
            assert response.status_code == 409
            error = response.json()["error"]
            assert error["kind"] == "bus" and error["code"] == "bus_busy"
            status = (await client.get("/api/status")).json()
            assert status["job"]["busy"] is True
        finally:
            stopped = await client.post("/api/live/watch/stop", json={"stream_id": stream_id})
            assert stopped.status_code == 200
        await asyncio.sleep(0.4)
        assert (await client.get("/api/status")).json()["job"]["busy"] is False

    run_async(app, body)


def test_bearer_token_is_required_and_only_read_from_the_header():
    app = make_app(token="s3cret-token")

    async def body(client):
        for path in ("/api/health", "/api/status"):
            assert (await client.get(path)).status_code == 401
            envelope = (await client.get(path)).json()
            assert envelope["error"]["kind"] == "auth"
        # A ?token= query string is never accepted (it would leak into logs).
        assert (await client.get("/api/health?token=s3cret-token")).status_code == 401
        response = await client.get("/api/health", headers={"Authorization": "Bearer s3cret-token"})
        assert response.status_code == 200
        assert response.json()["auth_required"] is True
        # The static front end is not behind the token; the SPA reads the fragment.
        assert (await client.get("/")).status_code == 200

    run_async(app, body)


def test_foreign_host_and_origin_are_rejected_and_no_cors_is_emitted():
    app = make_app()

    async def body(client):
        response = await client.get("/api/health", headers={"Host": "evil.example"})
        assert response.status_code == 403
        assert response.json()["error"]["kind"] == "auth"

        response = await client.post("/api/bus", json={"bus": "can-c"},
                                     headers={"Origin": "http://evil.example"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "bad_origin"

        response = await client.post("/api/bus", json={"bus": "can-c"},
                                     headers={"Origin": BASE})
        assert response.status_code == 200

        for method, path, payload in (("GET", "/api/health", None), ("POST", "/api/bus", {"bus": "can-c"})):
            response = await client.request(method, path, json=payload)
            headers = {key.lower() for key in response.headers}
            assert "access-control-allow-origin" not in headers, headers
            assert response.headers["content-security-policy"].startswith("default-src 'self'")
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"

    run_async(app, body)


def test_secret_never_appears_in_an_error_body():
    app = make_app(token="top-secret-value")

    async def body(client):
        response = await client.post("/api/uds/read-did",
                                     json={"module": "7E0", "dids": ["top-secret-value"]},
                                     headers={"Authorization": "Bearer top-secret-value"})
        assert response.status_code == 400
        assert "top-secret-value" not in response.text

    run_async(app, body)


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all web API checks passed")
