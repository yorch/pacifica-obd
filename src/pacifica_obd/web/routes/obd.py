"""Generic OBD-II routes (design §4.3 "Generic OBD-II").

Reads only: there is deliberately **no** ``POST /api/obd/dtc/clear`` here —
clearing codes is a write and does not exist in this build.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from pacifica_obd.web import deps, service

router = APIRouter()


def _session_job(name: str, fn, args: dict | None = None):
    """Wrap a read body so it is recorded like a CLI invocation."""

    def job(ctx):
        session_id = ctx.open_session(name, args or {})
        try:
            result = fn(ctx)
        finally:
            ctx.close_session(session_id)
        return result

    return job


@router.get("/obd/dtc", summary="Stored/pending/permanent DTCs (modes 03/07/0A)")
async def dtc(request: Request):
    worker = deps.get_worker(request)
    job = _session_job("dtc", lambda ctx: service.obd_dtcs(ctx.obd()))
    return await deps.run_job(worker, "obd-dtc", job, timeout_s=90.0)


@router.get("/obd/live", summary="Current value of every supported PID")
async def live(request: Request):
    worker = deps.get_worker(request)
    job = _session_job("live", lambda ctx: {"pids": service.obd_live(ctx.obd())})
    return await deps.run_job(worker, "obd-live", job, timeout_s=180.0)


@router.get("/obd/pids", summary="Supported PIDs (the value picker)")
async def pids(request: Request):
    worker = deps.get_worker(request)
    job = _session_job("pids", lambda ctx: {"pids": service.obd_pids(ctx.obd())})
    return await deps.run_job(worker, "obd-pids", job, timeout_s=90.0)


@router.get("/obd/readiness", summary="Readiness / I-M monitors")
async def readiness(request: Request):
    worker = deps.get_worker(request)
    job = _session_job("readiness", lambda ctx: service.obd_readiness(ctx.obd()))
    return await deps.run_job(worker, "obd-readiness", job, timeout_s=60.0)


@router.get("/obd/freeze", summary="Freeze-frame snapshot behind a DTC")
async def freeze(request: Request):
    worker = deps.get_worker(request)
    job = _session_job("freeze", lambda ctx: service.obd_freeze(ctx.obd()))
    return await deps.run_job(worker, "obd-freeze", job, timeout_s=90.0)


@router.post("/obd/scan", summary="One-shot VIN + DTC + PID summary (parity with `scan`)")
async def scan(request: Request):
    worker = deps.get_worker(request)

    def job(ctx):
        import obd

        session_id = ctx.open_session("scan", {})
        try:
            conn = ctx.obd()
            dtcs = service.obd_dtcs(conn)
            supported = len([c for c in conn.supported_commands
                             if service.is_read_only_command(c)])
            vin = service.obd_vin(conn)
            if vin and ctx.store is not None:
                ctx.store.upsert_vehicle(vin)
            if ctx.store is not None and session_id is not None:
                ctx.store.add_dtcs(session_id, vin, "obd", "PCM", dtcs["stored"], None)
            return {
                "status": str(conn.status()),
                "protocol": conn.protocol_name(),
                "vin": vin,
                "stored_dtcs": dtcs["stored"],
                "pending_dtcs": dtcs["pending"],
                "permanent_dtcs": dtcs["permanent"],
                "supported_pids": supported,
            }
        finally:
            ctx.close_session(session_id)

    return await deps.run_job(worker, "obd-scan", job, timeout_s=180.0)
