"""System / adapter routes (design §4.3 "System / adapter")."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from pacifica_obd.web import deps, errors, service
from pacifica_obd.web.app import tool_version
from pacifica_obd.web.bus import list_serial_ports
from pacifica_obd.web.schemas import BusRequest, ConnectRequest

router = APIRouter()

VALID_BUSES = ("can-c", "can-ihs", "both")


def adapter_state(worker) -> dict:
    """Cached adapter info — never touches the adapter (safe from async)."""
    backend = worker.backend
    getter = getattr(backend, "cached_info", None)
    if getter is None:
        return {"attached": bool(getattr(backend, "attached", False))}
    return getter()


def _configure_backend(backend, body: ConnectRequest) -> None:
    for field in ("port", "baud", "timeout_s", "check_voltage"):
        value = getattr(body, field, None)
        if value is None:
            continue
        attr = {"port": "requested_port", "baud": "baudrate",
                "timeout_s": "timeout", "check_voltage": "check_voltage"}[field]
        if hasattr(backend, attr):
            setattr(backend, attr, value)


@router.get("/health", summary="Liveness + adapter/read-only state")
async def health(request: Request):
    worker = deps.get_worker(request)
    settings = deps.get_settings(request)
    status = worker.status()
    info = adapter_state(worker)
    return {
        "ok": True,
        "version": tool_version(),
        "read_only": True,
        "auth_required": settings.auth_required,
        "adapter": {
            "attached": status["attached"],
            "port": info.get("port"),
            "status": info.get("status"),
            "protocol": info.get("protocol"),
            "adapter": info.get("adapter"),
            "voltage": info.get("voltage"),
            "bus": status["bus"],
        },
        "job": {
            "busy": status["busy"],
            "holder": status["holder"],
            "holder_id": status["holder_id"],
            "queued": status["queued"],
        },
    }


@router.get("/ports", summary="Candidate serial ports (passive enumeration)")
async def ports(request: Request):
    found = await asyncio.to_thread(list_serial_ports)
    return {"ports": found}


@router.post("/connect", summary="Open the adapter")
async def connect(request: Request, body: ConnectRequest | None = None):
    body = body or ConnectRequest()
    worker = deps.get_worker(request)

    def job(ctx):
        # Configure *inside* the job: a rejected (409) connect must not have
        # already changed the backend's port/baud for the next request.
        _configure_backend(ctx.worker.backend, body)
        conn = ctx.obd()
        info = service.obd_info(conn)
        info["attached"] = True
        return info

    return await deps.run_job(worker, "connect", job, exclusive=True, timeout_s=30.0)


@router.post("/disconnect", summary="Close the adapter")
async def disconnect(request: Request):
    worker = deps.get_worker(request)

    def job(ctx):
        ctx.worker.backend.close()
        return {"attached": False}

    return await deps.run_job(worker, "disconnect", job, exclusive=True, timeout_s=15.0)


@router.get("/info", summary="Adapter + vehicle info (parity with `info`)")
async def info(request: Request):
    worker = deps.get_worker(request)

    def job(ctx):
        return service.obd_info(ctx.obd())

    return await deps.run_job(worker, "info", job, timeout_s=30.0)


@router.post("/vin", summary="Read the VIN (mode 09 02, then UDS F190)")
async def vin(request: Request):
    worker = deps.get_worker(request)

    def job(ctx):
        value = None
        try:
            value = service.obd_vin(ctx.obd())
        except Exception:  # noqa: BLE001 - mode 09 02 is often unserved
            value = None
        if not value:
            value = service.uds_vin(ctx.uds(), timeout_ms=100, cancel=ctx.cancel)
        return {"vin": value}

    return await deps.run_job(worker, "vin", job, timeout_s=120.0)


@router.get("/status", summary="Worker + bus status (the TUI status line)")
async def status(request: Request):
    worker = deps.get_worker(request)
    return {
        "bus": worker.bus,
        "read_only": True,
        "attached": bool(getattr(worker.backend, "attached", False)),
        "adapter": adapter_state(worker),
        "job": worker.status(),
        "streams": [
            {"id": item.id, "name": item.name, "subscribers": item.channel.subscribers}
            for item in worker.jobs() if item.holds_bus and item.state == "running"
        ],
    }


@router.post("/bus", summary="Select the default bus")
async def set_bus(request: Request, body: BusRequest):
    if body.bus not in VALID_BUSES:
        raise errors.validation(f"unknown bus: {body.bus!r}")
    deps.get_worker(request).bus = body.bus
    return {"bus": body.bus}


@router.get("/jobs/{job_id}", summary="Poll a long job")
async def job_detail(request: Request, job_id: str):
    found = deps.get_worker(request).get(job_id)
    if found is None:
        raise errors.not_found(f"no job {job_id}", code="job_not_found")
    return found.snapshot()


@router.post("/jobs/{job_id}/cancel", summary="Cooperatively cancel a job")
async def cancel_job(request: Request, job_id: str):
    ok = deps.get_worker(request).cancel(job_id)
    if not ok:
        raise errors.not_found(f"no job {job_id}", code="job_not_found")
    return {"cancelled": True, "job_id": job_id}


@router.get("/jobs", summary="List recent jobs")
async def jobs(request: Request):
    return {"jobs": [item.snapshot() for item in deps.get_worker(request).jobs()]}
