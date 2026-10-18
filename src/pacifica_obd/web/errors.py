"""The single HTTP error envelope (RFC7807-flavoured, per the design §4.2).

Every failure leaves the server as::

    {"error": {"code": "adapter_no_port", "kind": "adapter",
               "message": "...", "status": "no_response",
               "detail": {"hint": "check ls /dev/cu.usb*"}}}

``kind`` is a stable, coarse category (``validation``/``auth``/``readonly``/
``bus``/``adapter``/``not_found``/``domain``/``timeout``/``internal``); ``code``
is a specific machine string a client can branch on. Messages are always
redacted of the auth token before serialisation.
"""

from __future__ import annotations

from typing import Any

# kind -> default HTTP status, used when a caller does not pin one.
_KIND_STATUS = {
    "validation": 400,
    "auth": 401,
    "readonly": 403,
    "refused": 403,
    "not_found": 404,
    "bus": 409,
    "confirm": 409,
    "domain": 422,
    "rate_limit": 429,
    "adapter": 503,
    "timeout": 504,
    "internal": 500,
}


class ApiError(Exception):
    """An error that maps to a documented HTTP status + envelope."""

    def __init__(self, code: str, kind: str, message: str, *,
                 status: int | None = None, http_status: int | None = None,
                 http: str | None = None, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.message = message
        self.status = status
        self.http_status = http_status or http or _KIND_STATUS.get(kind, 500)
        self.detail = detail or {}

    def envelope(self, redact=None) -> dict:
        message = redact(self.message) if redact else self.message
        detail = {key: (redact(value) if isinstance(value, str) else value)
                  for key, value in self.detail.items()}
        return {
            "error": {
                "code": self.code,
                "kind": self.kind,
                "message": message,
                "status": self.status,
                "detail": detail,
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApiError({self.http_status}, {self.code!r}, {self.message!r})"


# -- constructors for the common cases ------------------------------------- #
def validation(message: str, *, code: str = "validation", detail: dict | None = None) -> ApiError:
    return ApiError(code, "validation", message, detail=detail)


def not_found(message: str, *, code: str = "not_found") -> ApiError:
    return ApiError(code, "not_found", message)


def adapter(message: str, *, code: str = "adapter", status: str = "no_response",
            hint: str | None = None) -> ApiError:
    detail = {"hint": hint} if hint else {}
    return ApiError(code, "adapter", message, status=status, detail=detail)


def busy(message: str) -> ApiError:
    return ApiError("bus_busy", "bus", message)


def readonly(message: str = "this build of the web UI is read-only") -> ApiError:
    return ApiError("read_only", "readonly", message)


def timeout(message: str = "operation exceeded its time budget") -> ApiError:
    return ApiError("timeout", "timeout", message)


def internal(message: str) -> ApiError:
    return ApiError("internal", "internal", message)


def from_exception(exc: BaseException) -> ApiError:
    """Map a domain exception onto the envelope."""
    from pacifica_obd.adapter import AdapterError
    from pacifica_obd.elm import Elm327Error
    from pacifica_obd.web.bus import BusBusy, JobCancelled

    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, BusBusy):
        return busy(str(exc))
    if isinstance(exc, JobCancelled):
        return ApiError("cancelled", "bus", str(exc))
    if isinstance(exc, AdapterError):
        return adapter(str(exc), code="adapter")
    if isinstance(exc, Elm327Error):
        return adapter(str(exc), code="elm")
    if isinstance(exc, ValueError):
        return validation(str(exc))
    if isinstance(exc, TimeoutError):
        return timeout(str(exc) or "operation timed out")
    return internal(str(exc) or exc.__class__.__name__)


def redactor(token: str | None):
    """Return a ``str -> str`` that masks the auth token wherever it appears."""

    def _redact(value: Any) -> str:
        text = "" if value is None else str(value)
        if token:
            text = text.replace(token, "***")
        return text

    return _redact
