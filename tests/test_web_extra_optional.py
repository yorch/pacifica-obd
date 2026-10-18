"""The web stack is an *optional* extra: the CLI and the TUI must import and run
with FastAPI absent, and `pacifica-obd web` must print an install hint instead of
a traceback. FastAPI is blocked at the import hook here, so this is a real check
even in a synced-with-extras environment.

    uv run python tests/test_web_extra_optional.py
"""
from __future__ import annotations

import contextlib
import importlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BLOCKED = ("fastapi", "uvicorn", "pydantic", "starlette", "httpx")


class _Blocker:
    """A meta path finder that makes the web extra unimportable."""

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {name!r} (blocked by the test)", name=name)
        return None


@contextlib.contextmanager
def blocked_web_stack():
    for module in [name for name in list(sys.modules) if name.split(".")[0] in BLOCKED]:
        sys.modules.pop(module, None)
    for module in [name for name in list(sys.modules)
                   if name.startswith("pacifica_obd.web")]:
        sys.modules.pop(module, None)
    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)


def test_cli_and_tui_import_without_the_web_extra():
    with blocked_web_stack():
        cli = importlib.import_module("pacifica_obd.cli")
        tui = importlib.import_module("pacifica_obd.tui")
        assert hasattr(cli, "main") and hasattr(tui, "run")
        # The shared operation layer and the worker are dependency-free too.
        importlib.import_module("pacifica_obd.web.service")
        importlib.import_module("pacifica_obd.web.bus")
        for name in ("fastapi", "uvicorn"):
            try:
                importlib.import_module(name)
                raise AssertionError(f"{name} should have been blocked")
            except ImportError:
                pass


def test_web_subcommand_prints_an_install_hint_without_fastapi():
    with blocked_web_stack():
        cli = importlib.import_module("pacifica_obd.cli")
        stderr = io.StringIO()
        stdout = io.StringIO()
        code = 0
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            try:
                cli.main(["web", "--port", "1"])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        assert code == 1, code
        text = stderr.getvalue() + stdout.getvalue()
        assert "uv sync --extra web" in text, text
        assert "uv run --extra web pacifica-obd web" in text, text


def test_parsing_the_web_arguments_does_not_need_fastapi():
    with blocked_web_stack():
        cli = importlib.import_module("pacifica_obd.cli")
        parser = cli.build_parser()
        args = parser.parse_args(["web", "--port", "9000", "--demo", "--token"])
        assert args.command == "web" and args.port == 9000 and args.demo is True


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"ok  {name}")
    print("all optional-extra checks passed")
