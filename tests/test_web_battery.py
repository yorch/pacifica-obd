"""HTTP checks for the HV battery screen (design §3.6).

    uv run --extra web python tests/test_web_battery.py

No vehicle and no serial port: the app runs on a :class:`FakeAdapter` behind the
real :class:`BusWorker`, with synthetic 0x22 frames for the pack DIDs. Beyond the
happy path these tests pin the properties the screen's safety claims depend on:
per-field failure tolerance, strict preset validation, and that a battery read
**never sends a write service**.
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
    print("the [web] extra is required: uv run --extra web python tests/test_web_battery.py")
    raise SystemExit(2)

from pacifica_obd.web.app import create_app  # noqa: E402
from pacifica_obd.web.bus import BusWorker, FakeAdapter, FakeBackend  # noqa: E402
from pacifica_obd.web.settings import Settings  # noqa: E402

BASE = "http://127.0.0.1:8765"

# A cells DID with a known shape: 6 big-endian 16-bit words (mV). Cell 3 is
# weak, so max−min is a clear, checkable imbalance and cell 3 sits >50 mV below
# the pack mean (the colour band).
CELLS = [3800, 3802, 3735, 3798, 3801, 3799]
CELLS_HEX = "62F40C" + "".join("%04X" % value for value in CELLS)


def fake_adapter(*, dids=None, modules=None):
    return FakeAdapter(
        modules=modules or {"7E0": 0x7E8, "7E2": 0x7EA, "7E7": 0x7EF, "7E3": 0x7EB},
        dids=dids if dids is not None else {"7E7": {0xF40C: CELLS_HEX}},
    )


def make_app(adapter, **overrides):
    """An isolated app whose worker owns the given fake adapter."""
    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    worker = BusWorker(FakeBackend(adapter=adapter), bus="can-c")
    return create_app(Settings(demo=False, **overrides), worker=worker), adapter


def run(fn):
    return asyncio.run(fn)


async def with_client(app, coro):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as client:
        try:
            return await coro(client)
        finally:
            app.state.worker.stop()


PRESET = {"cells_did": "F40C", "first_cell_index": 1, "count": 6, "scale": 1.0,
          "pack_voltage_did": "F40D", "pack_voltage_scale": 0.1,
          "soc_did": "F40E", "soc_scale": 0.5, "module": "7E7", "soc_module": "7E2"}


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
def test_schema_reports_an_unverified_did_map_with_a_source():
    app, _ = make_app(fake_adapter())

    async def body(client):
        response = await client.get("/api/battery/schema")
        assert response.status_code == 200, response.text
        payload = response.json()
        # Every pack DID is unverified and carries a source; none invented.
        for field_, entry in payload["dids"].items():
            assert entry["verified"] is False, field_
            assert entry["did"] is None, field_
            assert entry["source"], field_
        assert payload["dids"]["cells"]["unit"] == "mV"
        assert payload["module"] == {"bpcm": "7E7", "hcp": "7E2", "bus": "can-c"}
        # The identification block is the only verified DID set.
        verified = [row["did"] for row in payload["identification_dids"] if row["verified"]]
        assert "F190" in verified and "F100" in verified
        # The saved-preset shape is present and complete.
        for key in ("cells_did", "first_cell_index", "count", "scale",
                    "pack_voltage_did", "soc_did", "temp_did", "module", "soc_module"):
            assert key in payload["preset"], key
        assert payload["notes"], "the schema must explain the missing DIDs"
        return payload

    payload = run(with_client(app, body))
    print("    schema: unverified map, verified identification block, preset shape")


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_decodes_cells_imbalance_and_verdict():
    app, adapter = make_app(fake_adapter())

    async def body(client):
        response = await client.post("/api/battery/read", json={"preset": PRESET})
        assert response.status_code == 200, response.text
        payload = response.json()
        cells = payload["cells"]
        assert cells["status"] == "ok", cells
        assert cells["values_mv"] == [float(v) for v in CELLS], cells["values_mv"]
        assert cells["mean_mv"] == round(sum(CELLS) / len(CELLS), 3)
        assert cells["min_mv"] == 3735.0 and cells["max_mv"] == 3802.0
        assert cells["min_index"] == 3 and cells["max_index"] == 2
        assert payload["imbalance_mv"] == 67.0
        # 3735 is 54 mV below the mean → the >50 mV colour band.
        assert cells["away"] == [3], cells["away"]
        assert payload["verdict"]["band"] == "imbalanced"
        assert cells["plausible"] is True
        return payload

    run(with_client(app, body))
    print("    snapshot: 6 cells, mean, min/max, imbalance 67 mV, verdict 'imbalanced'")


def test_snapshot_bands_are_ordered_and_plain_english():
    """The verdict bands must not certify health; each is a distinct word."""
    from pacifica_obd.web.routes import battery as battery_route

    bands = [battery_route._verdict(value)["band"] for value in (10, 45, 90, 200, None)]
    assert bands == ["balanced", "watch", "imbalanced", "severe", "unknown"], bands
    for value in (10, 45, 90, 200, None):
        text = battery_route._verdict(value)["text"]
        assert text and text[0].isupper()
    print("    verdict bands: balanced < watch < imbalanced < severe, ordered")


def test_soc_can_be_read_from_the_pack_module_itself():
    """When soc_module == BPCM, the SoC DID is folded into the pack read."""
    adapter = fake_adapter(dids={"7E7": {0xF40C: CELLS_HEX, 0xF40E: "62F40E00B0"}})
    app, _ = make_app(adapter)

    async def body(client):
        preset = {"cells_did": "F40C", "count": 6, "scale": 1.0,
                  "soc_did": "F40E", "soc_scale": 0.5, "module": "7E7", "soc_module": "7E7"}
        payload = (await client.post("/api/battery/read", json={"preset": preset})).json()
        assert payload["soc"]["value"] == 88.0, payload["soc"]
        assert payload["module"]["hcp"] == "7E7"

    run(with_client(app, body))
    print("    SoC: readable from the BPCM when soc_module is the pack module")


def test_a_partial_cell_read_withholds_the_verdict_and_the_pack_sum():
    """A short reply must not read as a full, healthy pack or a pack voltage."""
    partial = "62F40C" + "".join("%04X" % value for value in CELLS[:3])
    adapter = fake_adapter(dids={"7E7": {0xF40C: partial}})
    app, _ = make_app(adapter)

    async def body(client):
        preset = {"cells_did": "F40C", "first_cell_index": 1, "count": 6, "scale": 1.0}
        payload = (await client.post("/api/battery/read", json={"preset": preset})).json()
        cells = payload["cells"]
        assert cells["status"] == "partial", cells
        assert cells["complete"] is False
        assert len(cells["values_mv"]) == 3
        assert payload["verdict"]["band"] == "incomplete", payload["verdict"]
        # No pack voltage inferred from a partial sum, and a warning is shown.
        assert payload["pack_voltage"]["value"] is None, payload["pack_voltage"]
        assert payload["pack_voltage"]["source"] == "cells_sum_incomplete"
        assert any("Only 3" in warning for warning in payload["warnings"])

    run(with_client(app, body))
    print("    coverage: a partial column withholds the verdict and the pack sum")


def test_an_implausible_scale_withholds_the_verdict():
    """A mis-scaled column must not be classified as healthy or imbalanced."""
    app, _ = make_app(fake_adapter())

    async def body(client):
        preset = {"cells_did": "F40C", "count": 6, "scale": 0.001}  # → 3.8 mV cells
        payload = (await client.post("/api/battery/read", json={"preset": preset})).json()
        assert payload["cells"]["plausible"] is False
        assert payload["verdict"]["band"] == "unknown", payload["verdict"]
        assert payload["verdict"]["text"].startswith("A cell value is outside")
        # The cells-sum pack voltage is not offered from implausible values.
        assert payload["pack_voltage"]["value"] is None

    run(with_client(app, body))
    print("    plausibility: an out-of-range scale withholds the verdict")


def test_pack_voltage_soc_and_temperatures_decode():
    adapter = fake_adapter(dids={
        "7E7": {0xF40C: CELLS_HEX, 0xF40D: "62F40D0DDF", 0xF40F: "62F40F161B"},
        "7E2": {0xF40E: "62F40E00B0"},
    })
    app, _ = make_app(adapter)

    async def body(client):
        preset = dict(PRESET, temp_did="F40F")
        response = await client.post("/api/battery/read", json={"preset": preset})
        payload = response.json()
        assert payload["pack_voltage"]["value"] == 355.1, payload["pack_voltage"]
        assert payload["soc"]["value"] == 88.0, payload["soc"]
        assert payload["soc"]["did"] == "F40E"
        assert payload["temperature"]["min_c"] == 22.0
        assert payload["temperature"]["max_c"] == 27.0

    run(with_client(app, body))
    print("    scalars: pack 355.1 V, SoC 88.0 %, temps 22–27 °C")


def test_pack_voltage_falls_back_to_the_cell_sum():
    """No pack DID, but cells read → pack voltage is the cell sum, marked as such."""
    app, _ = make_app(fake_adapter())

    async def body(client):
        preset = dict(PRESET)
        preset.pop("pack_voltage_did")
        payload = (await client.post("/api/battery/read", json={"preset": preset})).json()
        expected = round(sum(CELLS) / 1000.0, 3)
        assert payload["pack_voltage"]["value"] == expected, payload["pack_voltage"]
        assert payload["pack_voltage"]["source"] == "cells_sum"

    run(with_client(app, body))
    print("    fallback: pack voltage = cell sum when no pack DID")


# --------------------------------------------------------------------------- #
# per-field failure tolerance
# --------------------------------------------------------------------------- #
def test_a_missing_cells_did_does_not_fail_the_scalar_reads():
    adapter = fake_adapter(dids={"7E7": {0xF40D: "62F40D0DDF"}, "7E2": {0xF40E: "62F40E00B0"}})
    app, _ = make_app(adapter)

    async def body(client):
        preset = {"cells_did": "F40C", "count": 6, "scale": 1.0,   # 7E7 answers NO DATA
                  "pack_voltage_did": "F40D", "pack_voltage_scale": 0.1,
                  "soc_did": "F40E", "soc_scale": 0.5}
        response = await client.post("/api/battery/read", json={"preset": preset})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["cells"]["status"] in ("no_response", "unexpected"), payload["cells"]
        assert payload["cells"]["values_mv"] == []
        assert payload["imbalance_mv"] is None
        assert payload["verdict"]["band"] == "unknown"
        # The other fields still resolved.
        assert payload["pack_voltage"]["value"] == 355.1
        assert payload["soc"]["value"] == 88.0

    run(with_client(app, body))
    print("    tolerance: a dead cells DID degrades to a per-field status")


def test_an_unaddressed_module_does_not_fail_the_snapshot():
    """If the pack module never answers, every field reports and the call is 200."""
    app, _ = make_app(fake_adapter(dids={}))  # no custom DIDs registered

    async def body(client):
        response = await client.post("/api/battery/read", json={"preset": PRESET})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["cells"]["status"] in ("no_response", "unexpected")
        assert payload["pack_voltage"]["value"] is None
        assert payload["soc"]["value"] is None
        assert payload["imbalance_mv"] is None

    run(with_client(app, body))
    print("    tolerance: an unanswered module still returns a 200 snapshot")


# --------------------------------------------------------------------------- #
# preset validation
# --------------------------------------------------------------------------- #
def test_preset_validation_refusals():
    app, _ = make_app(fake_adapter())
    bad_bodies = [
        {"cells_did": "ZZZZ", "count": 4},                 # not hex
        {"cells_did": "F40", "count": 4},                  # too short
        {"cells_did": "F40C", "count": 0},                 # 1..96 required with a DID
        {"cells_did": "F40C", "count": 97},                # above the cap
        {"cells_did": "F40C", "count": 4, "scale": 0},     # scale must be positive
        {"cells_did": "F40C", "count": 4, "scale": -1},
        {"cells_did": "F40C", "count": 4, "scale": 1e7},   # implausibly large
        {"cells_did": "F40C", "count": 4, "first_cell_index": 96},   # 0..95
    ]

    async def body(client):
        for request in bad_bodies:
            response = await client.post("/api/battery/preset", json=request)
            assert response.status_code == 400, (request, response.status_code, response.text)
            assert response.json()["error"]["kind"] == "validation", request
        # ... and a good one is accepted and echoed back normalised.
        good = {"cells_did": "f40c", "count": 6, "scale": 1}
        response = await client.post("/api/battery/preset", json=good)
        assert response.status_code == 200, response.text
        assert response.json()["preset"]["cells_did"] == "F40C"
        assert response.json()["verified"] is False

    run(with_client(app, body))
    # Non-finite values cannot travel as JSON, so pin the domain rule directly.
    from pacifica_obd.web.routes import battery as battery_route

    for value in (float("inf"), float("-inf"), float("nan")):
        try:
            battery_route._validate_preset({"cells_did": "F40C", "count": 4, "scale": value})
        except ValueError:
            pass
        else:  # pragma: no cover - a regression would reach here
            raise AssertionError(f"non-finite scale accepted: {value}")
    print("    validation: hex, count 1..96, finite positive scale all refused/enforced")


# --------------------------------------------------------------------------- #
# the read path is read-only
# --------------------------------------------------------------------------- #
def test_a_battery_read_never_sends_a_write_service():
    app, adapter = make_app(fake_adapter())

    async def body(client):
        await client.post("/api/battery/read", json={"preset": PRESET})

    run(with_client(app, body))

    commands = [command.upper() for command in adapter.commands]
    functional = [c for c in commands if not c.startswith(("AT", "ST"))]
    # No 0x2E write, no 0x31 routine, no 0x27 security access, no clear.
    for command in functional:
        assert command[:2] not in ("2E", "31", "27", "14", "04"), f"a write service was sent: {command}"
        assert command.startswith("22") or command.startswith("10"), command
    assert any(c.startswith("22") for c in functional), "no 0x22 read was sent"
    # No manual flow control on the read path either.
    for command in commands:
        assert not command.startswith("ATFCSM"), command
        assert not command.startswith("ATFCSH"), command
        assert command != "ATCFC0", command
    print("    read-only: no 2E/31/27/14 sent, no manual flow control")


def test_recording_writes_samples_but_never_a_bus_write():
    adapter = fake_adapter(dids={"7E7": {0xF40C: CELLS_HEX, 0xF40D: "62F40D0DDF"},
                                 "7E2": {0xF40E: "62F40E00B0"}})
    app, adapter = make_app(adapter)
    # A writable store under the isolated home.
    from pacifica_obd.store import Store

    store = Store()
    app.state.worker.store_factory = lambda: store
    app.state.worker._store = store
    app.state.worker._store_ready = True

    async def body(client):
        response = await client.post("/api/battery/read", json={"preset": PRESET, "store": True})
        assert response.status_code == 200, response.text
        # cells + imbalance + pack voltage + SoC.
        assert response.json()["recorded"]["rows"] == len(CELLS) + 3

    run(with_client(app, body))
    functional = [c.upper() for c in adapter.commands if not c.upper().startswith(("AT", "ST"))]
    assert not [c for c in functional if c[:2] in ("2E", "31", "27")], functional
    print("    recording: rows persisted to the local DB, still no bus write")


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all web battery checks passed")
