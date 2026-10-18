"""FastAPI dependencies and the async↔worker bridge.

A route never touches the adapter: it :func:`run_job`\\ s a callable on the
:class:`BusWorker` thread and awaits the result. That is the whole point of the
worker — one owner, no concurrent bus conversations.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from pacifica_obd.adapter import AdapterError
from pacifica_obd.elm import Elm327Error
from pacifica_obd.store import Store, logging_enabled
from pacifica_obd.web import errors
from pacifica_obd.web.bus import (
    BusBusy,
    BusWorker,
    FakeBackend,
    FakeAdapter,
    FakeOBD,
    JobCancelled,
    SerialBackend,
)
from pacifica_obd.web.settings import Settings

# Domain failures we translate into the documented error envelope. Anything else
# is a bug and is allowed to surface as an internal error (logged server-side).
_KNOWN_ERRORS = (AdapterError, Elm327Error, ValueError, TimeoutError, BusBusy, JobCancelled)


def default_store_factory() -> Store | None:
    """A writable store for recording, or None when logging is off/broken."""
    if not logging_enabled():
        return None
    try:
        store = Store()
    except Exception:  # noqa: BLE001 - persistence must never break a request
        return None
    return store if store.available else None


def build_backend(settings: Settings):
    if settings.demo:
        from pacifica_obd.web.demo import demo_backend

        return demo_backend()
    return SerialBackend(
        port=settings.port_name,
        baudrate=settings.baud,
        timeout=settings.timeout_s,
        check_voltage=settings.check_voltage,
        protocol=settings.generic_protocol,
    )


def build_worker(settings: Settings, store_factory: Callable[[], Store | None] | None = None) -> BusWorker:
    worker = BusWorker(build_backend(settings), bus=settings.bus)
    worker.store_factory = store_factory or default_store_factory
    return worker


def parse(fn: Callable, value):
    """Run a service-layer validator; a ``ValueError`` becomes a 400 envelope."""
    from pacifica_obd.web import service

    try:
        return fn(value)
    except ValueError as exc:
        raise errors.validation(str(exc)) from exc


def parse_can_id(value) -> int:
    from pacifica_obd.web import service

    return parse(service.parse_can_id, value)


def parse_did(value) -> int:
    from pacifica_obd.web import service

    return parse(service.parse_did, value)


def parse_timeout_ms(value) -> int:
    from pacifica_obd.web import service

    return parse(service.parse_timeout_ms, value)


def get_worker(request) -> BusWorker:
    return request.app.state.worker


def get_settings(request) -> Settings:
    return request.app.state.settings


async def run_job(worker: BusWorker, name: str, fn: Callable, *,
                  exclusive: bool = False, timeout_s: float | None = 120.0) -> Any:
    """Submit ``fn`` to the worker and await its result."""
    try:
        job = worker.submit(fn, name=name, exclusive=exclusive)
    except BusBusy as exc:
        raise errors.busy(str(exc)) from exc
    wrapped = asyncio.wrap_future(job.future)
    if timeout_s is None:
        try:
            return await wrapped
        except _KNOWN_ERRORS as exc:
            raise errors.from_exception(exc) from exc
    try:
        return await asyncio.wait_for(wrapped, timeout=timeout_s)
    except asyncio.TimeoutError:
        worker.cancel(job.id)
        raise errors.timeout(f"'{name}' exceeded {timeout_s:g}s") from None
    except asyncio.CancelledError:
        worker.cancel(job.id)
        raise
    except _KNOWN_ERRORS as exc:
        # Map the domain failure onto the documented envelope; anything else is a
        # genuine bug and is left to the 500 handler (and the server log).
        raise errors.from_exception(exc) from exc


def start_stream(worker: BusWorker, name: str, fn: Callable) -> str:
    """Submit a long-running, bus-holding job and return its id immediately.

    Refused with 409 ``bus_busy`` when *any* other bus operation is running or
    queued: an indefinite stream must never silently queue behind a scan.
    """
    try:
        job = worker.submit(fn, name=name, holds_bus=True, exclusive=True)
    except BusBusy as exc:
        raise errors.busy(str(exc)) from exc
    return job.id


__all__ = [
    "default_store_factory", "build_backend", "build_worker", "get_worker",
    "get_settings", "run_job", "start_stream", "FakeAdapter", "FakeOBD", "FakeBackend",
]
