"""SSE lifecycle over a real HTTP server (ASGI test transports buffer the whole
response, so a stream needs a socket).

    uv run --extra web python tests/test_web_stream.py

Covers: the stream starts, samples arrive as complete SSE frames with ids, the
buffering headers are set, Stop ends the job, a later subscriber sees the terminal
event, and a reconnect can resume from ``Last-Event-ID``.
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx
    import uvicorn
except ImportError:  # pragma: no cover
    print("the [web] extra is required: uv run --extra web python tests/test_web_stream.py")
    raise SystemExit(2)

from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(app):
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                            access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        if not thread.is_alive():
            raise RuntimeError("the test server exited early")
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("the test server did not start")
    return server, thread, f"http://127.0.0.1:{port}"


def stop_server(server, thread, app):
    server.should_exit = True
    thread.join(timeout=10)
    app.state.worker.stop()


def test_sse_watch_lifecycle():
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    app = create_app(Settings(demo=True))
    server, thread, base = start_server(app)
    try:
        with httpx.Client(timeout=15) as client:
            started = client.post(f"{base}/api/live/watch/start",
                                  json={"source": "pid", "values": ["RPM", "SPEED"], "interval_ms": 100})
            assert started.status_code == 200, started.text
            stream_id = started.json()["stream_id"]

            events: list[str] = []
            last_event_id = None
            samples: list[dict] = []
            with client.stream("GET", f"{base}/api/live/watch/stream/{stream_id}") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["cache-control"] == "no-cache"
                assert response.headers["x-accel-buffering"] == "no"
                assert "content-encoding" not in response.headers
                current = None
                for line in response.iter_lines():
                    if line.startswith("id:"):
                        last_event_id = line.split(":", 1)[1].strip()
                    elif line.startswith("event:"):
                        current = line.split(":", 1)[1].strip()
                        events.append(current)
                    elif line.startswith("data:") and current == "sample":
                        import json

                        samples.append(json.loads(line.split(":", 1)[1].strip()))
                    if events.count("sample") >= 3:
                        break

            assert "status" in events and "sample" in events, events
            assert last_event_id is not None
            first = samples[0]
            assert first["source"] == "pid" and first["seq"] >= 1
            assert "RPM" in first["values"] and "SPEED" in first["values"]
            assert first["values"]["RPM"]["unit"] == "rpm"

            # A resume from the last id is accepted, and the response *ends* after
            # the terminal event rather than heartbeating forever.
            stopped = client.post(f"{base}/api/live/watch/stop", json={"stream_id": stream_id})
            assert stopped.status_code == 200

            terminal: list[str] = []
            with client.stream("GET", f"{base}/api/live/watch/stream/{stream_id}",
                               headers={"Last-Event-ID": last_event_id}) as response:
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        terminal.append(line.split(":", 1)[1].strip())
            assert terminal and terminal[-1] == "done", terminal  # the stream completed
            # Re-subscribing to a finished stream must also terminate immediately.
            again = []
            with client.stream("GET", f"{base}/api/live/watch/stream/{stream_id}") as response:
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        again.append(line.split(":", 1)[1].strip())
            assert again and again[-1] == "done", again

            # The job wound down and the bus is free.
            deadline = time.time() + 5
            while time.time() < deadline:
                status = client.get(f"{base}/api/status").json()
                if status["job"]["busy"] is False:
                    break
                time.sleep(0.1)
            assert client.get(f"{base}/api/status").json()["job"]["busy"] is False

            # And a fresh read works again.
            assert client.get(f"{base}/api/obd/dtc").status_code == 200
    finally:
        stop_server(server, thread, app)


def test_sse_unknown_stream_is_a_404_envelope():
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    app = create_app(Settings(demo=True))
    server, thread, base = start_server(app)
    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(f"{base}/api/live/watch/stream/deadbeef")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "stream_not_found"
    finally:
        stop_server(server, thread, app)


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all web stream checks passed")
