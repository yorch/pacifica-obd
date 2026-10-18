"""FastAPI application factory for the pacifica-obd web UI.

Responsibilities kept here (so the route modules stay thin):

* the async↔worker wiring — ``app.state.worker`` is the only thing that ever
  touches the adapter;
* the **guard middleware**: Host validation (DNS-rebinding), bearer-token auth
  (optional), Origin validation on non-GET requests, and security headers
  including a strict CSP. No CORS middleware is ever added, so the app is
  same-origin only;
* the **error envelope** handlers;
* the static mount (build-free front end served straight from the source tree).

Read-only is structural: :data:`READ_ONLY` is a constant, ``app.state.read_only``
is set True, and no write route exists to be disabled.
"""

from __future__ import annotations

import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from pacifica_obd.web import errors
from pacifica_obd.web.bus import BusWorker
from pacifica_obd.web.deps import build_worker, default_store_factory
from pacifica_obd.web.reports import ReportRegistry
from pacifica_obd.web.settings import Settings

STATIC_DIR = Path(__file__).parent / "static"

# Read-only is a property of this build, not a runtime switch.
READ_ONLY = True

_CSP = (
    "default-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "style-src 'self'; script-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Report HTML is a self-contained, JS-free document meant for an iframe/new tab.
_REPORT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
)


def tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("pacifica-obd")
    except Exception:  # noqa: BLE001
        return "0.1.0"


def _hostname(header: str | None) -> str:
    """Strip the port from a Host header, handling ``[::1]:8765``."""
    if not header:
        return ""
    header = header.strip()
    if header.startswith("["):
        end = header.find("]")
        return header[1:end].lower() if end != -1 else header.lower()
    return header.rsplit(":", 1)[0].lower() if ":" in header else header.lower()


def _envelope(exc: errors.ApiError, redact) -> JSONResponse:
    return JSONResponse(exc.envelope(redact=redact), status_code=exc.http_status)


def _report_document_path(path: str) -> bool:
    """Report documents are addressed by an unguessable 96-bit id, not by token.

    A browser cannot send an ``Authorization`` header on an iframe/anchor
    navigation, so the review HTML and its downloads rely on the report id being
    an unguessable capability (plus loopback-only binding, the Host/Origin checks,
    and ``Referrer-Policy: no-referrer``). Everything that can *act* still needs
    the token; only reading one already-generated report does not.
    """
    parts = path.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "api" or parts[1] != "reports":
        return False
    report_id, action = parts[2], parts[3]
    if not re.fullmatch(r"[0-9a-f]{12}", report_id):
        return False
    return action in ("html", "zip") or (action == "download" and len(parts) == 5)


def create_app(settings: Settings | None = None, *, worker: BusWorker | None = None) -> FastAPI:
    settings = settings or Settings()
    redact = errors.redactor(settings.token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not app.state.worker.running:
            app.state.worker.start()
        try:
            yield
        finally:
            app.state.worker.stop()

    app = FastAPI(
        title="pacifica-obd web",
        version=tool_version(),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.read_only = READ_ONLY
    app.state.reports = ReportRegistry()

    if worker is None:
        worker = build_worker(settings)
    app.state.worker = worker

    if settings.demo:
        # Seed the demo DB and hand the worker a ready store.
        from pacifica_obd.web.demo import seed_store

        store = default_store_factory()
        seed_store(store)
        worker.store_factory = (lambda: store) if store is not None else (lambda: None)
        worker._store = store
        worker._store_ready = True

    # The worker is started eagerly so a non-lifespan ASGI test client still has
    # one; the lifespan handler is idempotent.
    worker.start()

    # -- guard middleware --------------------------------------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        settings_: Settings = request.app.state.settings
        path = request.url.path

        if not settings_.allow_remote:
            hostname = _hostname(request.headers.get("host"))
            if hostname not in ("127.0.0.1", "localhost", "::1"):
                return _envelope(
                    errors.ApiError("foreign_host", "auth",
                                    "Host header is not a loopback address",
                                    http_status=403, status="rejected",
                                    detail={"hint": "bind 127.0.0.1; use --allow-remote to relax"}),
                    redact,
                )

        if path.startswith("/api"):
            if settings_.auth_required and not _report_document_path(path):
                header = request.headers.get("authorization", "")
                token = header[7:] if header.lower().startswith("bearer ") else ""
                if not token or not secrets.compare_digest(token, settings_.token or ""):
                    return _envelope(
                        errors.ApiError("auth_required", "auth",
                                        "missing or invalid bearer token",
                                        status="rejected"),
                        redact,
                    )
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                if origin and origin not in settings_.local_origins():
                    return _envelope(
                        errors.ApiError("bad_origin", "auth",
                                        "cross-origin request refused",
                                        http_status=403, status="rejected",
                                        detail={"origin": origin}),
                        redact,
                    )

        response = await call_next(request)
        is_report_html = path.endswith("/html") and "/reports/" in path
        response.headers["Content-Security-Policy"] = _REPORT_CSP if is_report_html else _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        options = "SAMEORIGIN" if is_report_html else "DENY"
        response.headers["X-Frame-Options"] = options
        return response

    # -- error envelope ----------------------------------------------------
    @app.exception_handler(errors.ApiError)
    async def _api_error(request: Request, exc: errors.ApiError):
        return _envelope(exc, redact)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        api = errors.ApiError(
            "bad_request", "validation", "request did not match the expected shape",
            status="rejected", detail={"errors": exc.errors()[:8]},
        )
        return _envelope(api, redact)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        kind = "not_found" if exc.status_code == 404 else "internal"
        code = "not_found" if exc.status_code == 404 else "http_error"
        api = errors.ApiError(code, kind, str(exc.detail), http_status=exc.status_code)
        return _envelope(api, redact)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        api = errors.from_exception(exc)
        if api.http_status >= 500:
            api = errors.ApiError("internal", "internal", redact(str(exc)),
                                  http_status=api.http_status)
        return _envelope(api, redact)

    # -- routers -----------------------------------------------------------
    from pacifica_obd.web.routes import db as db_routes
    from pacifica_obd.web.routes import live as live_routes
    from pacifica_obd.web.routes import obd as obd_routes
    from pacifica_obd.web.routes import reports as report_routes
    from pacifica_obd.web.routes import system as system_routes
    from pacifica_obd.web.routes import uds as uds_routes

    app.include_router(system_routes.router, prefix="/api")
    app.include_router(obd_routes.router, prefix="/api")
    app.include_router(live_routes.router, prefix="/api")
    app.include_router(uds_routes.router, prefix="/api")
    app.include_router(db_routes.router, prefix="/api")
    app.include_router(report_routes.router, prefix="/api")

    # Any unmatched /api path is a documented 404 envelope, for every method: the
    # static mount below would otherwise answer a stray POST with a bare 405.
    @app.api_route("/api/{rest:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def _api_not_found(rest: str):
        raise errors.not_found(f"no such endpoint: /api/{rest}")

    # -- static (mounted last so /api wins) --------------------------------
    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app
