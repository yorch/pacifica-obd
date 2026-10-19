"""The read-path guard: mode 01 only, never mode 04 (which clears DTCs).

Regression for a real hazard: python-OBD seeds ``supported_commands`` from
``commands.base_commands()``, which includes ``CLEAR_DTC`` (``b"04"``). Any code
that enumerates ``supported_commands`` and queries each entry — ``live``,
``scan``, the dump — would therefore erase stored codes, freeze-frame data and
readiness monitors on a car that was only being *read*.

    uv run python tests/test_obd_guard.py
"""
from __future__ import annotations

import argparse
from unittest import mock

from pacifica_obd import cli
from pacifica_obd.obd_guard import REFUSED_COMMANDS, command_text, is_read_only_command


class FakeCommand:
    def __init__(self, name: str, command: bytes, value=None):
        self.name = name
        self.command = command
        self.value = value
        self.desc = name


class FakeResponse:
    def __init__(self, value=None):
        self.value = value

    def is_null(self) -> bool:
        return self.value is None


class FakeConn:
    """A connection whose supported set looks exactly like a real one."""

    def __init__(self):
        import obd.commands as C

        self.supported_commands = list(C.base_commands()) + [
            FakeCommand("RPM", b"010C", 812.5),
            FakeCommand("SPEED", b"010D", 0),
        ]
        self.queried: list[str] = []

    def query(self, command):
        self.queried.append(command_text(command))
        return FakeResponse(getattr(command, "value", None))

    def close(self) -> None:
        pass

    def port_name(self) -> str:
        return "/dev/cu.fake"

    def protocol_name(self) -> str:
        return "6"

    def status(self) -> str:
        return "Car connected"


def test_predicate_allows_only_mode_01():
    assert is_read_only_command(FakeCommand("RPM", b"010C")) is True
    for name, mode in (("CLEAR_DTC", b"04"), ("GET_DTC", b"03"),
                       ("PIDS_9A", b"0900"), ("MIDS_A", b"0600"),
                       ("GET_CURRENT_DTC", b"07"), ("ELM_VOLTAGE", b"ATRV")):
        assert is_read_only_command(FakeCommand(name, mode)) is False, name
    # defence in depth: a refused name is never allowed, whatever its mode byte
    assert is_read_only_command(FakeCommand("CLEAR_DTC", b"0104")) is False
    assert REFUSED_COMMANDS


def test_live_never_sends_a_non_read():
    conn = FakeConn()
    args = argparse.Namespace(json=False, port=None, baud=115200, timeout=1.0,
                              check_voltage=False)
    with mock.patch.object(cli, "connect", return_value=conn):
        cli.cmd_live(args)
    assert conn.queried, "live should still read the mode-01 PIDs"
    assert all(q.startswith("01") for q in conn.queried), conn.queried
    assert not any(q.startswith(("03", "04", "06", "07", "09", "AT")) for q in conn.queried), \
        conn.queried
    assert "04" not in conn.queried


def test_scan_and_dump_counts_exclude_non_reads():
    conn = FakeConn()
    args = argparse.Namespace(json=True, port=None, baud=115200, timeout=1.0,
                              check_voltage=False)
    with mock.patch.object(cli, "connect", return_value=conn), \
            mock.patch.object(cli, "jdump") as dumped:
        cli.cmd_scan(args)
    payload = dumped.call_args.args[0]
    # The two mode-01 sensor PIDs plus the mode-01 support bitmap (0100) — but
    # NOT CLEAR_DTC (04), GET_DTC (03), PIDS_9A (09), MIDS_A (06) or the AT ones.
    assert payload["supported_pids"] == 3, payload["supported_pids"]
    for command in conn.supported_commands:
        text = command_text(command)
        if command.name in {"RPM", "SPEED", "PIDS_A"}:
            assert text.startswith("01"), (command.name, text)
        else:
            assert not text.startswith("01"), (command.name, text)


def test_watch_refuses_a_non_read_pid():
    """`watch --pids CLEAR_DTC` used to reach mode 04 (clear DTCs)."""
    import argparse
    from unittest import mock

    conn = FakeConn()
    args = argparse.Namespace(json=True, port=None, baud=115200, timeout=1.0,
                              check_voltage=False, pids="CLEAR_DTC,RPM",
                              interval=0.01, csv=None)

    def stop_after_one_round(*_args):
        # the loop is `while True`, so the only way out is the interrupt it handles
        raise KeyboardInterrupt

    with mock.patch.object(cli, "connect", return_value=conn), \
            mock.patch.object(cli, "jdump"), \
            mock.patch.object(cli.time, "sleep", stop_after_one_round):
        try:
            cli.cmd_watch(args)
        except SystemExit:  # acceptable: it may exit rather than return
            pass
    assert conn.queried, "watch should still poll the mode-01 PID"
    assert all(q.startswith("01") for q in conn.queried), conn.queried
    assert "04" not in conn.queried, "watch sent mode 04 (clear DTCs)"


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all read-path guard checks passed")
