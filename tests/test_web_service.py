"""Service-layer checks: the shared read operations against synthetic frames.

Runs with no vehicle and no adapter:

    uv run python tests/test_web_service.py

The adapter is ``web.bus.FakeAdapter``, which replays spaced ELM/UDS lines and
records every command it sees, so the tests can also assert the safety rules
(an ``ATCRA`` receive filter, and *no* manual flow control on a read path).
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pacifica_obd import cli  # noqa: E402
from pacifica_obd.web import service  # noqa: E402
from pacifica_obd.web.bus import FAKE_VIN, FakeAdapter, FakeOBD  # noqa: E402


def _no_manual_flow_control(adapter) -> bool:
    """No read path may switch the STN into indexed framing."""
    for command in adapter.commands:
        if command.startswith("ATFCSM") or command.startswith("ATFCSH") or command == "ATCFC0":
            return False
    return True


def test_discovery_keeps_responders_and_maps_response_ids():
    adapter = FakeAdapter()
    result = service.discover_modules(adapter, bus="can-c", start=0x700, end=0x7FF, timeout_ms=10)
    ids = [(row["request_id"], row["response_id"]) for row in result["modules"]]
    assert ids == [("7E0", "7E8"), ("7E1", "7E9"), ("7E2", "7EA"), ("7E7", "7EF")], ids
    assert result["protocol"] == 33
    assert result["powertrain"] == "phev"  # 7E2/7E7 answered => hybrid
    assert all(row["kind"] == "positive" for row in result["modules"])
    assert _no_manual_flow_control(adapter), "a read path used manual flow control"


def test_a_refused_receive_filter_is_not_read_through():
    """A stale filter must never be left in place for a later module read."""
    adapter = FakeAdapter(refuse_filter=True)
    result = service.read_module_dtcs(adapter, bus="can-c", start=0x7E0, end=0x7E1, timeout_ms=10)
    # The adapter would not accept a responder filter, so the module is reported
    # as not addressable rather than read through the previous module's filter.
    assert all(row["status"] == "no_response" for row in result["modules"]), result["modules"]
    assert any(command == "ATAR" for command in adapter.commands)

    # Discovery is tolerant (it clears the filter and still reports what answered),
    # but it never keeps a filter the adapter refused.
    discovered = FakeAdapter(refuse_filter=True)
    found = service.discover_modules(discovered, bus="can-c", start=0x7E0, end=0x7E1, timeout_ms=10)
    assert [row["request_id"] for row in found["modules"]] == ["7E0", "7E1"]
    assert discovered.filter is None


def test_discovery_never_returns_a_stranger_for_a_mapped_id():
    """The receive filter must be applied, so a stray answer is not attributed."""
    adapter = FakeAdapter()
    service.discover_modules(adapter, bus="can-c", start=0x7E0, end=0x7E0, timeout_ms=10)
    assert any(command.startswith("ATCRA 7E8") for command in adapter.commands)


def test_discovery_is_cancellable():
    import threading

    adapter = FakeAdapter()
    cancel = threading.Event()
    cancel.set()
    result = service.discover_modules(adapter, start=0x700, end=0x7FF, cancel=cancel)
    assert result["modules"] == []
    assert result["cancelled"] is True


def test_all_module_dtcs_decodes_and_keeps_every_target():
    adapter = FakeAdapter()
    result = service.read_module_dtcs(adapter, bus="can-c", start=0x7E0, end=0x7E2, timeout_ms=10)
    by_id = {row["request_id"]: row for row in result["modules"]}
    assert by_id["7E0"]["status"] == "ok"
    assert [dtc["code"] for dtc in by_id["7E0"]["dtcs"]] == ["P0300"]
    assert by_id["7E0"]["dtcs"][0]["flags"] == ["confirmed"]
    assert by_id["7E1"]["status"] == "ok" and by_id["7E1"]["dtcs"] == []
    assert by_id["7E2"]["status"] == "negative"
    assert result["total_dtcs"] == 1
    assert result["incomplete"] is True  # the negative module is surfaced, not hidden
    assert _no_manual_flow_control(adapter)


def test_read_dids_multi_frame_vin_and_statuses():
    adapter = FakeAdapter()
    result = service.read_dids(adapter, bus="can-c", module=0x7E0,
                               dids=[0xF190, 0xF187, 0xF1FF], timeout_ms=10)
    records = {record["did"]: record for record in result["dids"]}
    assert records["F190"]["status"] == "ok"
    assert records["F190"]["ascii"] == FAKE_VIN  # 17 chars => multi-frame reassembly
    assert records["F187"]["ascii"] == "PART1234"
    assert records["F1FF"]["status"] in ("negative", "no_response", "unexpected")
    assert result["vin"] == FAKE_VIN
    assert any(command.startswith("ATCRA") for command in adapter.commands)


def test_module_info_reads_the_whole_identification_block():
    adapter = FakeAdapter()
    result = service.module_info(adapter, bus="can-c", module=0x7E0, timeout_ms=10)
    dids = [record["did"] for record in result["modules"]]
    assert dids == ["F100", "F186", "F187", "F188", "F189", "F18A", "F18C", "F190", "F191"], dids
    assert result["vin"] == FAKE_VIN


def test_capture_snapshot_collects_modules_dtcs_and_readings():
    store_home = tempfile.mkdtemp()
    os.environ["PACIFICA_OBD_HOME"] = store_home
    from pacifica_obd.store import Store

    store = Store()
    session_id = store.start_session("capture", {})
    try:
        adapter = FakeAdapter()
        snapshot = service.capture_snapshot(
            adapter, buses=["can-c"], start=0x7E0, end=0x7E1, timeout_ms=10,
            store=store, session_id=session_id,
        )
        assert snapshot["aborted"] is None
        assert snapshot["vin"] == FAKE_VIN
        assert [module["request_id"] for module in snapshot["modules"]] == ["7E0", "7E1"]
        assert len(snapshot["dtcs"]) == 2
        # Only 7E0/7E1 answered, so the hybrid-only hint is absent.
        assert snapshot["powertrain"] is None
        assert store._query("SELECT COUNT(*) AS n FROM readings WHERE session_id = ?",
                            (session_id,))[0]["n"] > 0
        assert store._query("SELECT COUNT(*) AS n FROM modules WHERE session_id = ?",
                            (session_id,))[0]["n"] == 2
    finally:
        store.close()
        os.environ.pop("PACIFICA_OBD_HOME", None)


def test_capture_reports_a_partial_run_instead_of_raising():
    class DyingAdapter(FakeAdapter):
        def cmd(self, command, wait=None):
            if command == "1902FF":
                from pacifica_obd.elm import Elm327Error

                raise Elm327Error("serial write failed: device not configured")
            return super().cmd(command, wait)

    snapshot = service.capture_snapshot(DyingAdapter(), buses=["can-c"],
                                        start=0x7E0, end=0x7E0, timeout_ms=10)
    assert "device not configured" in (snapshot["aborted"] or "")


def test_generic_obd_reads_never_send_a_write_command():
    """A `live` read must not be able to send mode 04 (CLEAR_DTC)."""
    conn = FakeOBD()
    # python-OBD's supported_commands includes CLEAR_DTC (mode 04); the fake does too.
    assert any(c.name == "CLEAR_DTC" for c in conn.supported_commands)
    service.obd_live(conn)
    assert "CLEAR_DTC" not in conn.queried, conn.queried
    assert all(service.is_read_only_command(c)
               for c in conn.supported_commands if c.name in conn.queried)
    for command in conn.queried:
        assert command != "CLEAR_DTC"

    pids = service.obd_pids(conn)
    assert "CLEAR_DTC" not in {row["name"] for row in pids}
    assert all(row["command"].startswith("01") for row in pids), pids

    try:
        service.obd_live(conn, ["CLEAR_DTC"])
        raise AssertionError("obd_live accepted CLEAR_DTC")
    except ValueError as exc:
        assert "CLEAR_DTC" in str(exc)
    assert "CLEAR_DTC" not in conn.queried

    # And a mixed list is silently reduced to its read-only members.
    rows = service.obd_live(conn, ["RPM", "CLEAR_DTC"])
    assert [row["name"] for row in rows] == ["RPM"]


def test_address_module_sets_the_receive_filter_for_every_module():
    adapter = FakeAdapter()
    service.read_module_dtcs(adapter, bus="can-c", start=0x7E0, end=0x7E1, timeout_ms=10)
    addressed = [c for c in adapter.commands if c.startswith("ATCRA")]
    # Each module it reads must be addressed with its own responder filter.
    assert "ATCRA 7E8" in addressed and "ATCRA 7E9" in addressed, addressed
    assert _no_manual_flow_control(adapter)


def test_generic_obd_reads():
    conn = FakeOBD()
    dtcs = service.obd_dtcs(conn)
    assert dtcs["stored"][0]["code"] == "P0300" and dtcs["total"] == 1
    pids = service.obd_pids(conn)
    assert {row["pid"] for row in pids} >= {"0C", "0D", "05"}
    live = service.obd_live(conn, ["RPM", "SPEED"])
    assert live[0]["name"] == "RPM" and live[0]["value"]["unit"] == "rpm"
    readiness = service.obd_readiness(conn)
    assert readiness["available"] is True and readiness["ready"] is False
    assert [m["name"] for m in readiness["monitors"]] == ["misfire", "components"]
    freeze = service.obd_freeze(conn)
    assert freeze["dtc"] is None


def test_validators_match_the_cli_argparse_types():
    for good in ("7E0", "0x7e0", "700"):
        assert service.parse_can_id(good) == cli._can_id(good)
    for bad in ("ZZZ", "800", "-1"):
        try:
            service.parse_can_id(bad)
            raise AssertionError(f"parse_can_id accepted {bad!r}")
        except ValueError as web_error:
            try:
                cli._can_id(bad)
                raise AssertionError(f"cli._can_id accepted {bad!r}")
            except Exception as cli_error:  # argparse.ArgumentTypeError
                assert str(web_error) == str(cli_error), (web_error, cli_error)
    assert service.parse_did("f190") == cli._did("f190")
    assert service.parse_hex_request("1003") == cli._hex_request("1003")
    assert service.parse_timeout_ms("100") == cli._timeout_ms("100")
    for bad, fn, cli_fn in (("zz", service.parse_did, cli._did),
                            ("1", service.parse_hex_request, cli._hex_request),
                            ("0", service.parse_timeout_ms, cli._timeout_ms)):
        try:
            fn(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
        try:
            cli_fn(bad)
            raise AssertionError(f"cli accepted {bad!r}")
        except Exception:
            pass


def test_dtc_description_uses_the_bundled_dataset():
    assert service.dtc_description("P0300")
    assert service.dtc_description("not a code") is None


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all web service checks passed")
