"""Database-browser checks: filtering, pagination, CSV, the detail endpoints, the
downsampled series endpoint, and the path/SQL-injection guards.

    uv run --extra web python tests/test_web_db.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx  # noqa: F401
    from httpx import ASGITransport, AsyncClient
except ImportError:  # pragma: no cover
    print("the [web] extra is required: uv run --extra web python tests/test_web_db.py")
    raise SystemExit(2)

from pacifica_obd.store import Store  # noqa: E402
from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.downsample import downsample, lttb, min_max_avg  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

BASE = "http://127.0.0.1:8765"
VIN = "2C4RC1BG7MR00000X"  # obviously fake (test VIN)


def seed() -> str:
    """A temp home with a small, deterministic database. Returns the home dir."""
    home = tempfile.mkdtemp()
    os.environ["PACIFICA_OBD_HOME"] = home
    store = Store()
    store.upsert_vehicle(VIN, make="Chrysler", model="Pacifica", year=2021,
                         powertrain="phev", nickname="Test van")
    store.upsert_vehicle("2C4RC1BG0MR00000Y", make="Chrysler", model="Pacifica", year=2019)
    session = store.start_session("dtc-all", {"bus": "can-c"}, "/dev/cu.test")
    store.finish_session(session, "ok", VIN)
    store.add_module(session, VIN, "can-c", {"request_id": "7E0", "response_id": "7E8", "kind": "positive"})
    store.add_dtcs(session, VIN, "can-c", "7E0",
                   [{"code": "P0300", "failure_type": "00", "status": "08", "flags": ["confirmed"],
                     "description": "Misfire"}, {"code": "U0100", "failure_type": "00", "status": "04",
                                                 "flags": ["pending"], "description": "Lost comm"}],
                   "5902FF")
    store.add_reading(session, VIN, "can-c", "7E0", "F190", "VIN", VIN.encode().hex(), VIN, "ok",
                      None, category="module-info")
    store.add_reading(session, VIN, "can-c", "7E0", "F187", "Part", "414243", "ABC", "ok", None,
                      category="module-info")
    store.add_reading(session, VIN, "can-c", "7E7", "F40D", "pack", "0168", None, "no_response", None,
                      category="did")
    # 1000 samples at 1 s with a spike, so the downsampler has something to chew on.
    start = datetime.now(timezone.utc) - timedelta(seconds=1000)
    for index in range(1000):
        ts = (start + timedelta(seconds=index)).isoformat(timespec="milliseconds")
        value = 100.0 + index * 0.1
        if index == 500:
            value = 5000.0  # the extreme the downsampler must not lose
        store.add_sample(session, VIN, "can-c", "7E0", "F40D", "pack", value, str(value), "V", "ok",
                         None, ts=ts)
    store.close()
    return home


def run_async(app, fn):
    async def wrapper():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE) as client:
            try:
                return await fn(client)
            finally:
                app.state.worker.stop()
    return asyncio.run(wrapper())


def test_downsample_algorithms():
    points = [(float(i), float(i * i % 97)) for i in range(1000)]
    points[500] = (500.0, 100000.0)  # a spike
    sampled = lttb(points, 100)
    assert len(sampled) == 100
    assert sampled[0] == points[0] and sampled[-1] == points[-1]
    assert any(value > 90000 for _x, value in sampled), "lttb dropped the spike"
    assert lttb(points, 5000) == points
    envelope = min_max_avg(points, 50)
    assert len(envelope) <= 150 + 3
    assert max(value for _x, value in envelope) == 100000.0
    result, method = downsample(points, 100)
    assert method == "lttb" and len(result) == 100
    same, method = downsample(points, 5000)
    assert method is None and same == points


def test_schema_and_table_views():
    seed()
    app = create_app(Settings())

    async def body(client):
        schema = (await client.get("/api/db/schema")).json()
        assert schema["available"] is True
        names = [table["name"] for table in schema["tables"]]
        assert names == ["vehicles", "sessions", "events", "modules", "dtcs", "readings", "samples"]
        counts = {table["name"]: table["rows"] for table in schema["tables"]}
        assert counts["vehicles"] == 2 and counts["dtcs"] == 2 and counts["samples"] == 1000

        vehicles = (await client.get("/api/db/vehicles")).json()
        assert vehicles["total"] == 2
        assert vehicles["rows"][0]["vin"] in (VIN, "2C4RC1BG0MR00000Y")

        filtered = (await client.get(f"/api/db/dtcs?vin={VIN}&code=P0300")).json()
        assert filtered["total"] == 1 and filtered["rows"][0]["code"] == "P0300"

        searched = (await client.get("/api/db/readings?q=Part")).json()
        assert searched["total"] == 1

        paged = (await client.get("/api/db/samples?limit=5&offset=10&order=ts&dir=asc")).json()
        assert len(paged["rows"]) == 5 and paged["total"] == 1000
        stamps = [row["ts"] for row in paged["rows"]]
        assert stamps == sorted(stamps)

        empty = (await client.get(f"/api/db/dtcs?code=NOTHING")).json()
        assert empty["total"] == 0 and empty["rows"] == []
        assert empty["columns"]  # columns survive an empty result

    run_async(app, body)


def test_detail_endpoints():
    seed()
    app = create_app(Settings())

    async def body(client):
        sessions = (await client.get("/api/db/sessions")).json()
        session_id = sessions["rows"][0]["id"]
        detail = (await client.get(f"/api/db/sessions/{session_id}")).json()
        assert detail["session"]["id"] == session_id
        assert detail["children"]["dtcs"] and detail["children"]["modules"]
        assert detail["children"]["samples"]

        summary = (await client.get(f"/api/db/vehicles/{VIN}/summary")).json()
        assert summary["vin"] == VIN and summary["vehicle"]["nickname"] == "Test van"
        assert summary["counts"]["dtcs"] == 2 and summary["counts"]["samples"] == 1000

        missing = await client.get("/api/db/sessions/999999")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "session_not_found"

    run_async(app, body)


def test_series_is_downsampled_and_filterable():
    seed()
    app = create_app(Settings())

    async def body(client):
        payload = (await client.get("/api/db/samples/series?max_points=200")).json()
        assert payload["available"] is True and payload["rows"] == 1000
        series = payload["series"]
        assert len(series) == 1
        assert series[0]["id"] == "7E0:F40D" and series[0]["raw_count"] == 1000
        assert len(series[0]["points"]) <= 200
        assert series[0]["downsampled"] == "lttb"
        assert any(value > 4000 for _t, value in series[0]["points"]), "the spike was lost"
        assert series[0]["statuses"] == {"ok": 1000}

        narrow = (await client.get("/api/db/samples/series?max_points=50&module=7E2")).json()
        assert narrow["series"] == []

        by_did = (await client.get("/api/db/samples/series?max_points=20&did=F40D")).json()
        assert len(by_did["series"]) == 1 and len(by_did["series"][0]["points"]) <= 20

    run_async(app, body)


def test_csv_export():
    seed()
    app = create_app(Settings())

    async def body(client):
        response = await client.get("/api/db/csv?table=dtcs")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        lines = response.text.strip().splitlines()
        assert lines[0].startswith("id,session_id,ts,vin,bus,module,code")
        assert len(lines) == 3  # header + two DTCs
        assert "P0300" in response.text

        filtered = await client.get("/api/db/csv?table=dtcs&code=U0100")
        assert "U0100" in filtered.text and "P0300" not in filtered.text

    run_async(app, body)


def test_unknown_table_and_injection_attempts_are_refused():
    seed()
    app = create_app(Settings())

    async def body(client):
        for path in ("/api/db/sqlite_master", "/api/db/../../etc/passwd",
                     "/api/db/vehicles;DROP TABLE vehicles", "/api/db/schema%20"):
            response = await client.get(path)
            assert response.status_code == 404, (path, response.status_code)
            assert response.json()["error"]["kind"] == "not_found"

        # A bogus sort column falls back to the table default instead of failing
        # (or worse, being interpolated).
        response = await client.get("/api/db/dtcs?order=id;DROP%20TABLE%20dtcs")
        assert response.status_code == 200 and response.json()["total"] == 2
        still_there = (await client.get("/api/db/schema")).json()
        assert any(table["name"] == "dtcs" for table in still_there["tables"])

    run_async(app, body)


def test_missing_database_degrades_instead_of_crashing():
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()  # empty: no pacifica.db
    app = create_app(Settings())

    async def body(client):
        schema = (await client.get("/api/db/schema")).json()
        assert schema["available"] is False and schema["tables"] == []
        table = (await client.get("/api/db/dtcs")).json()
        assert table["available"] is False and table["rows"] == []
        series = (await client.get("/api/db/samples/series")).json()
        assert series["available"] is False
        csv = await client.get("/api/db/csv?table=dtcs")
        assert csv.status_code == 503
        assert csv.json()["error"]["code"] == "db_unavailable"
        # The rest of the API keeps working with no database at all.
        assert (await client.get("/api/health")).status_code == 200

    run_async(app, body)


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all web DB checks passed")
