"""DID discovery sweep: the planner, the classifier, and the read-only paths.

    uv run python tests/test_did_sweep.py

The classifier is checked against synthetic payloads for every shape *and* the
ambiguous/unknown cases (including a payload that could be either a cell block
or a pack voltage). The planner is checked at its bounds. The CLI is checked to
refuse a non-read service, the route to validate its input and to never send a
write service, and a sweep to report progress and stop when cancelled.

The HTTP checks need the ``[web]`` extra; without it they are skipped, so this
file still runs offline like the rest of ``tests/``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pacifica_obd import didsweep  # noqa: E402
from pacifica_obd.web import service  # noqa: E402
from pacifica_obd.web.bus import BusWorker, FakeAdapter, FakeBackend  # noqa: E402

try:
    from httpx import ASGITransport, AsyncClient

    HAVE_WEB = True
except ImportError:  # pragma: no cover - depends on the optional extra
    HAVE_WEB = False


def _hex_words(values) -> str:
    return "".join("%04X" % value for value in values)


CELLS = [3800, 3801, 3799, 3802, 3800, 3798, 3801, 3745]
CELLS_HEX = _hex_words(CELLS)
#: The wire frame for the demo cells DID (``62 F20C <words>``).
CELLS_FRAME = "62F20C" + CELLS_HEX
PACK_FRAME = "62F20D0DDF"


# --------------------------------------------------------------------------- #
# classifier: one test per shape
# --------------------------------------------------------------------------- #
def test_cells_shape_is_confident_and_counted():
    verdict = didsweep.classify(CELLS_HEX)
    assert verdict.kind == "cell_voltages", verdict
    assert verdict.confidence == "high", verdict
    assert verdict.count == len(CELLS)
    assert verdict.scale == 1.0 and verdict.units == "mV"
    # A single weak cell does not stop it being a cell block.
    assert "spread" in verdict.reason


def test_a_short_cell_block_is_ambiguous_with_pack_voltage():
    """4-7 words cannot be told from a pack voltage; never a confident guess."""
    payload = _hex_words([3800, 3801, 3802, 3803])
    verdict = didsweep.classify(payload)
    assert verdict.kind == "cell_voltages", verdict
    assert verdict.confidence == "ambiguous", verdict
    assert "pack_voltage" in verdict.alternatives, verdict.alternatives
    assert verdict.count == 4


def test_pack_voltage_decodes_at_scale_0_1():
    verdict = didsweep.classify("0DDF")  # 3551 x 0.1 = 355.1 V
    assert verdict.kind == "pack_voltage" and verdict.confidence == "high", verdict
    assert verdict.scale == 0.1 and verdict.units == "V"


def test_pack_voltage_at_scale_1_overlaps_temperature():
    verdict = didsweep.classify("015E")  # 350 -> 350 V, or 35.0 °C
    assert verdict.kind == "pack_voltage", verdict
    assert verdict.confidence == "ambiguous", verdict
    assert "temperature" in verdict.alternatives, verdict.alternatives


def test_soc_needs_context_and_is_otherwise_ambiguous():
    plain = didsweep.classify("5A")  # 90 % or 90 °C
    assert plain.kind == "soc", plain
    assert plain.confidence == "ambiguous", plain
    assert "temperature" in plain.alternatives
    hinted = didsweep.classify("5A", context="soc")
    assert hinted.kind == "soc" and hinted.confidence == "high"


def test_temperature_shapes():
    u16 = didsweep.classify("0226")  # 550 x 0.1 = 55.0 °C
    assert u16.kind == "temperature" and u16.confidence == "high", u16
    assert u16.scale == 0.1
    negative = didsweep.classify("D8")  # -40 °C (i8)
    assert negative.kind == "temperature", negative
    assert negative.scale == 1.0


def test_identification_shapes():
    vin = didsweep.classify("2C4RC1BG7MR000001".encode("ascii").hex().upper())
    assert vin.kind == "identification" and vin.confidence == "high", vin
    part = didsweep.classify("504152542D31323334")  # "PART-1234"
    assert part.kind == "identification", part
    assert part.confidence in ("high", "medium"), part


def test_unknown_is_never_invented():
    for payload in ("", "ABC", "ZZZZ", "7FFF", "AB" * 600, None):
        verdict = didsweep.classify(payload)
        assert verdict.kind == "unknown" and verdict.confidence == "unknown", (payload, verdict)
        assert verdict.scale is None and verdict.count is None


def test_a_full_uds_reply_is_only_stripped_when_asked():
    verdict = didsweep.classify("62F20C" + CELLS_HEX, did=0xF20C, full_reply=True)
    assert verdict.kind == "cell_voltages" and verdict.count == len(CELLS), verdict
    # The service layer hands over the data bytes alone: a legitimate payload
    # that happens to begin `62 <DID>` must NOT be silently shortened. Here the
    # data is 5 bytes (odd) -> unknown, not a manufactured 355.1 V.
    stripped_by_default = didsweep.classify("62F20C0DDF", did=0xF20C)
    assert stripped_by_default.kind == "unknown", stripped_by_default


def test_classify_handles_hostile_inputs_without_inventing_or_raising():
    # Unicode that upper-cases into valid hex must be refused, not rewritten.
    assert didsweep.classify("\ufb00\ufb00").kind == "unknown"
    # A junk/overflowing `did` must not raise (it simply cannot authorise a strip).
    for bad_did in (float("inf"), float("nan"), -1, 0x10000, "ZZZZ", object()):
        verdict = didsweep.classify("0DDF", did=bad_did, full_reply=True)
        assert isinstance(verdict, didsweep.Verdict), (bad_did, verdict)
    # A huge string is refused cheaply rather than upper-cased in full.
    assert didsweep.classify("ab" * 100000).kind == "unknown"


def test_classify_never_raises_on_arbitrary_input():
    import random

    rng = random.Random(7)
    for _ in range(400):
        length = rng.randint(0, 40)
        payload = "".join(rng.choice("0123456789ABCDEF") for _ in range(length))
        verdict = didsweep.classify(payload, did=rng.choice([None, 0xF20C, 0x1234]))
        assert verdict.confidence in ("high", "medium", "ambiguous", "unknown")
        verdict.as_dict()  # must be JSON-able


def test_more_than_one_shape_reports_the_alternatives():
    verdict = didsweep.classify("00B0")  # 176: SoC 88 %, 17.6 °C, 176 V
    assert verdict.confidence == "ambiguous", verdict
    assert set(verdict.alternatives) >= {"soc", "temperature"}, verdict.alternatives


# --------------------------------------------------------------------------- #
# planner bounds
# --------------------------------------------------------------------------- #
def test_plan_is_bounded_and_deterministic():
    sweep = didsweep.plan(0xF100, 0xF1FF)
    assert sweep.count == 256
    assert sweep.dids[0] == 0xF100 and sweep.dids[-1] == 0xF1FF
    assert didsweep.plan(0xF100, 0xF1FF).dids == sweep.dids  # deterministic


def test_plan_step_and_range():
    sweep = didsweep.plan(0x1000, 0x1010, step=4)
    assert sweep.dids == (0x1000, 0x1004, 0x1008, 0x100C, 0x1010)
    assert didsweep.plan(0x2000, 0x2000).dids == (0x2000,)


def test_plan_refusals():
    refusals = [
        lambda: didsweep.plan(0xF200, 0xF100),      # reversed
        lambda: didsweep.plan(0, 0xFFFF),           # absurd by default
        lambda: didsweep.plan(0, 0xFFFF, max_reads=65536),  # above the absolute cap
        lambda: didsweep.plan(0xF100, 0xF1FF, step=0),
        lambda: didsweep.plan(0xF100, 0xF1FF, max_reads=0),
        lambda: didsweep.plan(0x10000, 0x10000),    # out of 16-bit range
        lambda: didsweep.plan(0xF100, 0xF1FF, step=-1),
    ]
    for index, call in enumerate(refusals):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError(f"call {index} was not refused")
    # Raising the cap explicitly is allowed, up to the absolute ceiling.
    assert didsweep.plan(0x0000, 0x03FF, max_reads=2000).count == 1024


def test_plan_rejects_non_integer_and_boolean_arguments():
    for call in (
        lambda: didsweep.plan(0xF100, 0xF1FF, step=True),
        lambda: didsweep.plan(0xF100, 0xF1FF, step=1.9),
        lambda: didsweep.plan(0xF100, 0xF1FF, step=float("inf")),
        lambda: didsweep.plan(0xF100, 0xF1FF, max_reads=2.9),
        lambda: didsweep.plan(0xF100, 0xF1FF, max_reads=True),
        lambda: didsweep.plan(0xF100, 0xF1FF, max_reads="2.5"),
        lambda: didsweep.plan(1.5, 0xF1FF),
        lambda: didsweep.plan(True, 0xF1FF),
    ):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError("a non-integer planner argument was accepted")


# --------------------------------------------------------------------------- #
# the read-only guard
# --------------------------------------------------------------------------- #
def test_the_read_only_guard_allows_only_read_services():
    for command in ("1003", "22F100", "22F20C"):
        didsweep.assert_read_only([command])
    for command in ("2EF19001", "31010203", "27", "04", "19 02 FF", "ATFCSM1", "ATCAF0"):
        try:
            didsweep.assert_read_only([command])
        except ValueError:
            continue
        raise AssertionError(f"a non-read command slipped through: {command}")


def test_session_commands_are_session_then_reads():
    sweep = didsweep.plan(0xF200, 0xF203)
    assert didsweep.session_commands(sweep) == ("1003", "22F200", "22F201", "22F202", "22F203")


# --------------------------------------------------------------------------- #
# candidates + preset suggestion
# --------------------------------------------------------------------------- #
def _entry(did, payload, *, status="ok"):
    verdict = didsweep.classify(payload, did=int(did, 16)) if status == "ok" else None
    return {"did": did, "status": status, "hex": payload, "verdict": verdict}


def test_candidates_and_preset_from_a_sweep():
    entries = [
        _entry("F20C", CELLS_HEX),
        _entry("F20D", "0DDF"),
        _entry("F20E", "00B0"),
        _entry("F20F", "0226"),
        _entry("F2FF", "", status="no_response"),
    ]
    found = didsweep.candidates(entries)
    best = found["best"]
    assert best["cell_voltages"].did == "F20C"
    assert best["cell_voltages"].count == len(CELLS)
    assert best["pack_voltage"].did == "F20D"
    assert best["soc"].did == "F20E"
    assert best["temperature"].did == "F20F"
    suggestion = didsweep.preset_suggestion(found, module="7E7", bus="can-c")
    assert suggestion["available"] is True
    preset = suggestion["preset"]
    assert preset["cells_did"] == "F20C" and preset["count"] == len(CELLS)
    assert preset["pack_voltage_did"] == "F20D" and preset["pack_voltage_scale"] == 0.1
    assert preset["soc_did"] == "F20E" and preset["soc_scale"] == 0.5
    assert preset["verified"] is False


def test_preset_is_not_offered_when_nothing_is_confident():
    entries = [_entry("F200", "00"), _entry("F201", "FF", status="negative")]
    found = didsweep.candidates(entries)
    suggestion = didsweep.preset_suggestion(found, module="7E7")
    assert suggestion["available"] is False
    assert suggestion["preset"] is None
    # An ambiguous-only sweep must never be offered as a preset either.
    only_ambiguous = didsweep.candidates([_entry("F202", "00B0")])
    assert didsweep.preset_suggestion(only_ambiguous, module="7E7")["available"] is False


# --------------------------------------------------------------------------- #
# the service layer never puts a write on the bus
# --------------------------------------------------------------------------- #
def test_sweep_dids_reads_only():
    adapter = FakeAdapter(dids={"7E7": {0xF20C: CELLS_FRAME, 0xF20D: PACK_FRAME}})
    result = service.sweep_dids(adapter, bus="can-c", module=0x7E7,
                                start=0xF200, end=0xF20F, timeout_ms=100)
    assert result["plan"]["count"] == 16
    assert result["answered"] == 2
    functional = [c.upper() for c in adapter.commands if not c.upper().startswith(("AT", "ST"))]
    for command in functional:
        assert command[:2] not in ("2E", "31", "27", "14", "04"), command
        assert command.startswith("22") or command.startswith("10"), command
    assert any(c.startswith("22") for c in functional)
    for command in adapter.commands:
        assert not command.startswith("ATFCSM"), command
        assert command != "ATCFC0", command


def test_a_sweep_cancelled_before_start_sends_nothing():
    """A job cancelled before it runs must not put even session control on the bus."""
    adapter = FakeAdapter(dids={"7E7": {0xF20C: CELLS_FRAME}})
    cancel = threading.Event()
    cancel.set()
    result = service.sweep_dids(adapter, bus="can-c", module=0x7E7, start=0xF200, end=0xF20F,
                                cancel=cancel)
    assert result["cancelled"] is True
    assert result["dids"] == []
    assert result["answered"] == 0
    assert adapter.commands == [], adapter.commands


# --------------------------------------------------------------------------- #
# progress + cancel with the fake backend
# --------------------------------------------------------------------------- #
class SlowAdapter(FakeAdapter):
    def cmd(self, command, wait=None):
        time.sleep(0.004)
        return super().cmd(command, wait)


def test_a_sweep_reports_progress_and_stops_when_cancelled():
    adapter = SlowAdapter(dids={"7E7": {0xF200 + index: "62%04X" % (0xF200 + index) + CELLS_HEX
                                        for index in range(32)}})
    worker = BusWorker(FakeBackend(adapter=adapter), bus="can-c")
    worker.start()
    events: list[dict] = []

    def body(ctx):
        return service.sweep_dids(
            ctx.uds(), bus="can-c", module=0x7E7, start=0xF200, end=0xF21F,
            timeout_ms=100, cancel=ctx.cancel,
            progress=lambda event: (events.append(event), ctx.progress(**event)),
        )

    try:
        job = worker.submit(body, name="battery-sweep")
        deadline = time.time() + 10
        while len(events) < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert events and events[0]["kind"] == "did", events[:1]
        assert events[0]["total"] == 32
        assert worker.cancel(job.id) is True
        result = job.future.result(timeout=10)
        assert result["cancelled"] is True, result["cancelled"]
        assert len(result["dids"]) < 32, len(result["dids"])
        assert job.state == "cancelled"
    finally:
        worker.stop()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fake_cli_run(argv):
    """Run cmd_did_sweep with the adapter faked; return (exit_code, adapter)."""
    from unittest import mock

    from pacifica_obd import cli

    args = cli.build_parser().parse_args(argv)
    adapter = FakeAdapter(dids={"7E7": {0xF20C: CELLS_FRAME, 0xF20D: PACK_FRAME}})
    code = 0
    with mock.patch.object(cli, "open_elm", return_value=adapter):
        try:
            cli.cmd_did_sweep(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, adapter


def test_the_cli_sweeps_only_read_services():
    code, adapter = _fake_cli_run(
        ["did-sweep", "--module", "7E7", "--start", "F20C", "--end", "F20F", "--json"])
    assert code == 0
    functional = [c.upper() for c in adapter.commands if not c.upper().startswith(("AT", "ST"))]
    assert functional, "the CLI sent nothing"
    for command in functional:
        assert command.startswith(("10", "22")), command


def test_the_cli_refuses_a_non_read_service():
    """If the plan ever contained a non-read command the CLI refuses before opening."""
    from unittest import mock

    from pacifica_obd import cli, didsweep as module

    args = cli.build_parser().parse_args(["did-sweep", "--module", "7E7", "--json"])
    opened: list[bool] = []
    with mock.patch.object(module, "session_commands", return_value=("2EF19001",)), \
            mock.patch.object(cli, "open_elm", side_effect=lambda *a, **k: opened.append(True)):
        code = 0
        try:
            cli.cmd_did_sweep(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    assert code == 2, code
    assert opened == [], "the port was opened before the non-read service was refused"


# --------------------------------------------------------------------------- #
# HTTP route (needs the [web] extra)
# --------------------------------------------------------------------------- #
def _route_checks():
    from pacifica_obd.web.app import create_app
    from pacifica_obd.web.settings import Settings

    os.environ["PACIFICA_OBD_HOME"] = tempfile.mkdtemp()
    adapter = FakeAdapter(dids={"7E7": {0xF20C: CELLS_FRAME, 0xF20D: PACK_FRAME}})
    worker = BusWorker(FakeBackend(adapter=adapter), bus="can-c")
    app = create_app(Settings(demo=False), worker=worker)
    return app, adapter


def test_the_route_validates_and_returns_candidates():
    if not HAVE_WEB:  # pragma: no cover - optional extra
        print("    (skipped: httpx not installed)")
        return
    app, adapter = _route_checks()

    async def body(client):
        good = await client.post("/api/battery/sweep", json={
            "module": "7E7", "start": "F200", "end": "F20F", "step": 1, "timeout_ms": 100})
        assert good.status_code == 200, good.text
        payload = good.json()
        assert payload["plan"]["count"] == 16
        assert (payload["candidates"]["best"] or {}).get("cell_voltages", {}).get("did") == "F20C"
        assert payload["preset"]["available"] is True

        bad_bodies = [
            {"start": "F1FF", "end": "F100"},        # reversed
            {"start": "F100", "end": "FFFF"},        # over the default cap
            {"start": "F100", "end": "F1FF", "step": 0},
            {"start": "F100", "end": "F1FF", "max_reads": 99999},
            {"start": "ZZZZ"},                       # not a DID
        ]
        for request in bad_bodies:
            response = await client.post("/api/battery/sweep", json=request)
            assert response.status_code == 400, (request, response.status_code, response.text)
            assert response.json()["error"]["kind"] == "validation", request

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
            try:
                await body(client)
            finally:
                app.state.worker.stop()

    asyncio.run(run())
    functional = [c.upper() for c in adapter.commands if not c.upper().startswith(("AT", "ST"))]
    for command in functional:
        assert command[:2] not in ("2E", "31", "27", "14", "04"), command
        assert command.startswith(("10", "22")), command


def test_the_route_reports_progress_and_cancels():
    if not HAVE_WEB:  # pragma: no cover - optional extra
        print("    (skipped: httpx not installed)")
        return
    app, _ = _route_checks()
    app.state.worker.stop()  # re-own the worker below with a slow adapter
    slow = SlowAdapter(dids={"7E7": {0xF200 + index: "62%04X" % (0xF200 + index) + CELLS_HEX
                                          for index in range(24)}})
    worker = BusWorker(FakeBackend(adapter=slow), bus="can-c")
    worker.start()
    app.state.worker = worker

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
            task = asyncio.create_task(client.post("/api/battery/sweep", json={
                "module": "7E7", "start": "F200", "end": "F217", "timeout_ms": 100}))
            # Wait for progress to appear on the job table, then cancel it.
            holder = None
            for _ in range(400):
                jobs = (await client.get("/api/jobs")).json()["jobs"]
                running = [job for job in jobs if job["state"] == "running"]
                if running and running[0]["progress"].get("index"):
                    holder = running[0]["id"]
                    break
                await asyncio.sleep(0.01)
            assert holder, "the sweep never reported progress"
            await client.post(f"/api/jobs/{holder}/cancel", json={})
            response = await task
            assert response.status_code == 200, response.text
            assert response.json()["cancelled"] is True, response.json().get("cancelled")
        worker.stop()

    asyncio.run(run())


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all DID sweep checks passed")
