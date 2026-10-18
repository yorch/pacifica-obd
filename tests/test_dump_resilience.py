"""Partial-dump behaviour: a mid-scan adapter failure must not lose the capture.

Runs without a vehicle or an adapter (everything is mocked):

    uv run python tests/test_dump_resilience.py

Exit codes covered: ``0`` complete, ``3`` started but aborted (see docs/usage.md).
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest import mock

import pacifica_obd.cli as cli
from pacifica_obd.adapter import AdapterError
from pacifica_obd.elm import Elm327Error
from pacifica_obd.report import dump_to_html

PARSER = cli.build_parser()


class FakeConn:
    """Minimal stand-in for python-OBD's ``OBD`` used by the generic pass."""

    supported_commands: list = []

    def port_name(self) -> str:
        return "/dev/cu.fake"

    def protocol_name(self) -> str:
        return "6"

    def status(self) -> str:
        return "Car connected"

    def query(self, command):
        class _Status:
            def is_null(self) -> bool:
                return True

        return _Status()

    def close(self) -> None:
        pass


class FakeElm:
    """Minimal stand-in for the raw ELM transport used by the UDS pass."""

    def cmd(self, command: str, wait: float | None = None) -> str:
        if command.startswith("ATSH"):
            return "OK"
        if command == "1902FF":
            return "7E8 03 7F 19 11"
        return "NO DATA"

    def close(self) -> None:
        pass


def _probe_one_then_die(*_args, **_kwargs):
    yield {"request_id": "7E0", "response_id": "7E8", "kind": "uds"}
    raise Elm327Error("serial write failed: device not configured")


def test_adapter_gone_at_open_yields_labelled_partial():
    out = tempfile.mkdtemp()
    args = PARSER.parse_args(["dump", "--no-generic", "--out", out, "--formats", "json"])
    with mock.patch.object(cli, "open_elm", side_effect=AdapterError("device not configured")):
        dump = cli.build_vehicle_dump(args)
    assert "device not configured" in (dump["aborted"] or "")
    assert dump["totals"] == {"modules": 0, "dtcs": 0, "readings": 0}


def test_partial_files_written_and_exit_code_is_three():
    out = tempfile.mkdtemp()
    args = PARSER.parse_args(["dump", "--no-generic", "--out", out, "--formats", "json,html"])
    code = 0
    try:
        with mock.patch.object(cli, "open_elm", side_effect=AdapterError("device not configured")):
            cli.cmd_dump(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    assert code == 3
    assert sorted(os.listdir(out)) == ["vehicle.html", "vehicle.json"]
    with open(os.path.join(out, "vehicle.json"), encoding="utf-8") as handle:
        assert json.load(handle)["aborted"]


def test_partial_module_survives_a_mid_module_failure():
    out = tempfile.mkdtemp()
    args = PARSER.parse_args(["dump", "--no-generic", "--out", out, "--formats", "json"])
    with mock.patch.object(cli, "open_elm", return_value=FakeElm()), \
         mock.patch.object(cli, "configure_bus", return_value=None), \
         mock.patch.object(cli, "_probe_ids_multi", _probe_one_then_die), \
         mock.patch.object(cli, "_read_did", side_effect=Elm327Error("gone")):
        dump = cli.build_vehicle_dump(args)
    assert "gone" in (dump["aborted"] or "")
    assert [m["request_id"] for m in dump["modules"]] == ["7E0"]


def test_generic_success_path_is_unchanged():
    out = tempfile.mkdtemp()
    args = PARSER.parse_args(["dump", "--no-uds", "--out", out, "--formats", "json"])
    with mock.patch.object(cli, "connect", return_value=FakeConn()), \
         mock.patch.object(cli, "_obd_value", return_value=None), \
         mock.patch.object(cli, "_codes", return_value=[]):
        dump = cli.build_vehicle_dump(args)
    assert dump["aborted"] is None
    assert dump["generic"]["dtcs"] == {"stored": [], "pending": [], "permanent": []}
    assert dump["adapter"]["port"] == "/dev/cu.fake"


def test_html_banner_only_when_aborted():
    base = {"vehicle": {}, "modules": [], "generic": {}, "totals": {}}
    assert "Partial capture" not in dump_to_html({**base, "aborted": None})
    assert "Partial capture" in dump_to_html({**base, "aborted": "boom"})


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all partial-dump checks passed")
