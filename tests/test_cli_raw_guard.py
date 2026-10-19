"""The CLI's ``raw`` command obeys the same tier guard as the web console.

The CLI is the expert escape hatch, so the deny tier is *reachable* — but only
behind an explicit ``--force``, and classification happens before the port is
opened so a refused command never touches the adapter.

    uv run python tests/test_cli_raw_guard.py
"""
from __future__ import annotations

import argparse
import contextlib
import io
from unittest import mock

from pacifica_obd import cli

PARSER = cli.build_parser()


class FakeElm:
    def __init__(self, port, baud, timeout=None):
        self.sent: list[str] = []

    def cmd(self, command, wait=None):
        self.sent.append(command)
        return ["OK"]

    def close(self) -> None:
        pass


def run(argv: list[str], *, force_elm: bool = True):
    """Run cmd_raw with the adapter faked; return (exit_code, elm_or_None)."""
    args = PARSER.parse_args(argv)
    made: list[FakeElm] = []

    def factory(port, baud, timeout=None):
        elm = FakeElm(port, baud, timeout)
        made.append(elm)
        return elm

    code = 0
    out = io.StringIO()
    with mock.patch.object(cli, "find_port", return_value="/dev/cu.fake"), \
            mock.patch.object(cli, "Elm327", side_effect=factory), \
            contextlib.redirect_stderr(out):
        try:
            cli.cmd_raw(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    elm = made[0] if made else None
    return code, elm, out.getvalue()


def test_a_read_command_is_sent():
    code, elm, _ = run(["raw", "ATI", "--json"])
    assert code == 0
    assert elm is not None and elm.sent == ["ATI"], elm.sent if elm else None


def test_the_deny_tier_is_refused_and_never_opens_the_port():
    for command in ("04", "2E F1 90 01", "27", "31010203", "ATCAF0", "ATCEA01", "ATSP0"):
        code, elm, text = run(["raw", command, "--json"])
        assert code == 2, (command, code)
        assert elm is None, f"{command} reached the adapter"
        assert "refusing" in text, (command, text)


def test_the_confirm_tier_needs_confirm():
    code, elm, text = run(["raw", "ATSTFF", "--json"])
    assert code == 2 and elm is None and "--confirm" in text
    code, elm, _ = run(["raw", "ATSTFF", "--confirm", "--json"])
    assert code == 0 and elm is not None and elm.sent == ["ATSTFF"]


def test_force_reaches_the_deny_tier_and_warns():
    code, elm, text = run(["raw", "2E F1 90 01", "--force", "--json"])
    assert code == 0, text
    assert elm is not None and elm.sent == ["2E F1 90 01"], elm.sent if elm else None
    assert "--force" in text and "cannot undo" in text, text


def test_framing_and_flow_control_flags_need_force():
    for flag in ("--no-auto-format", "--no-flow-control"):
        code, elm, text = run(["raw", "ATI", flag, "--json"])
        assert code == 2 and elm is None, (flag, code)
        assert "force" in text.lower(), (flag, text)
        code, elm, _ = run(["raw", "ATI", flag, "--force", "--json"])
        assert code == 0 and elm is not None


def test_a_smuggled_second_command_is_refused():
    for command in ("ATI;ATZ", "ATI\n2E F1 90 01", "ATI`ATZ"):
        code, elm, _ = run(["raw", command, "--json"])
        assert code == 2 and elm is None, command


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all CLI raw-guard checks passed")
