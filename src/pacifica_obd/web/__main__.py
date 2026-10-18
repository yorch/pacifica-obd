"""``pacifica-obd-web`` entry point (and the target of ``pacifica-obd web``).

Imports of uvicorn/FastAPI are lazy so that importing this module without the
``[web]`` extra installed is harmless (the CLI relies on that to print an install
hint instead of a traceback).
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from pacifica_obd.web.settings import DEFAULT_HOST, DEFAULT_PORT, Settings, resolve_token


def _display_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def run_server(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
               token: str | bool | None = None, read_only: bool = True,
               demo: bool = False, open_browser: bool = False,
               allow_remote: bool = False, log_level: str = "info") -> None:
    # Deliberately a plain import: a missing optional dependency must surface as
    # ImportError so the CLI can print an install hint (see cli.cmd_web).
    import uvicorn

    from pacifica_obd.web.app import create_app

    resolved = resolve_token(token)
    if allow_remote and not resolved:
        raise SystemExit("--allow-remote requires a token (use --token or PACIFICA_OBD_WEB_TOKEN)")
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        raise SystemExit(
            f"refusing to bind {host!r}: pass --allow-remote (and --token) to do so deliberately"
        )

    settings = Settings(host=host, port=port, token=resolved, demo=demo,
                        allow_remote=allow_remote, log_level=log_level,
                        read_only=bool(read_only))
    app = create_app(settings)

    display = _display_host(host)
    url = f"http://{display}:{port}/"
    # The token travels only in the URL fragment: never sent to the server, never
    # logged, never a Referer.
    shown = f"{url}#t={resolved}" if resolved else url
    # flush: the URL (with its one-time token) must be visible immediately, even
    # when stdout is a pipe or a log file.
    print("pacifica-obd web UI" + (" (read-only)" if read_only else " (writes enabled)"))
    if demo:
        print("  demo mode: synthetic adapter + a seeded demo database")
    if not read_only:
        print("  writes enabled: 0x27 unlock and 2E/31 writes require a typed confirmation")
    print(f"  {shown}")
    print("  the UI talks only to this machine; Ctrl-C to stop")
    sys.stdout.flush()
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(shown)).start()

    uvicorn.run(app, host=host, port=port, log_level=log_level, access_log=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="pacifica-obd-web",
        description="Local web UI for pacifica-obd.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port")
    parser.add_argument("--token", nargs="?", const=True, default=None,
                        help="require a bearer token (bare flag generates one)")
    parser.add_argument("--read-only", dest="read_only", action="store_true",
                        help="disable every write route (this is the default)")
    parser.add_argument("--allow-writes", dest="allow_writes", action="store_true",
                        help="enable the guarded write surface (0x27 / 2E / 31)")
    parser.add_argument("--demo", action="store_true",
                        help="synthetic adapter + seeded DB (no car needed)")
    parser.add_argument("--open", action="store_true", help="open a browser")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow a non-loopback bind (requires --token)")
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    args = parser.parse_args(argv)
    if args.read_only and args.allow_writes:
        raise SystemExit("--read-only and --allow-writes are mutually exclusive")
    try:
        run_server(host=args.host, port=args.port, token=args.token,
                   read_only=not args.allow_writes,
                   demo=args.demo, open_browser=args.open, allow_remote=args.allow_remote,
                   log_level=args.log_level)
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise SystemExit(
            "the web UI needs FastAPI + uvicorn; install the extra:\n"
            "  uv sync --extra web\n"
            "  uv run --extra web pacifica-obd-web"
        ) from exc


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
