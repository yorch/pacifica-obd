"""Guarded write routes (design §3.8): ``write-did`` (0x2E) and ``routine`` (0x31).

Both are two-phase and share one confirm-token store:

* ``POST /api/writes/<op>``         — preview: address the module, require an
  extended session, read the current value, show the exact wire request, and
  return a single-use token bound to those bytes **and to the adapter
  generation**.
* ``POST /api/writes/<op>/confirm`` — accept **only** the token and the typed
  module id. The parameters live server-side, so a client cannot confirm
  something different from what it previewed.

Safety properties enforced here:

* a token is invalidated by an adapter disconnect/reconnect (generation check, in
  the same serialized job as the send);
* a ``2E`` write re-checks the current value inside that same job — there is no
  check/send window;
* a DID whose current value cannot be read is refused at preview rather than
  written blind;
* the job checks its cancellation flag immediately before transmitting, so a
  timed-out HTTP request cannot still send a write;
* every preview and every send/refusal writes an audit event; a pre-transmission
  failure is never recorded as ``sent``.

When the server runs read-only these routes are not registered at all; the app
factory installs a 403 handler under ``/api/writes`` instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from pacifica_obd.web import audit, deps, errors, service
from pacifica_obd.web.confirm import ConfirmError, ConfirmTokenStore
from pacifica_obd.web.schemas import (
    RoutinePreviewRequest,
    WriteConfirmRequest,
    WriteDidPreviewRequest,
)

router = APIRouter()

SGWRISK = (
    "The Security Gateway usually blocks writes on 2018+ vehicles. This tool "
    "cannot undo a write; record the original value first."
)


def _store(request: Request) -> ConfirmTokenStore:
    return request.app.state.confirm


def _generation(ctx) -> str:
    return str(getattr(ctx.worker.backend, "generation", 0))


def _refusal(exc) -> errors.ApiError:
    """A safety refusal from the service layer is a 403, not an adapter failure."""
    if isinstance(exc, errors.ApiError) and exc.kind == "adapter" and "refusing" in exc.message:
        return errors.ApiError("refused", "refused", exc.message, http_status=403,
                               status=exc.status)
    return exc


async def _run(worker, name, job, timeout_s=60.0):
    try:
        return await deps.run_job(worker, name, job, exclusive=True, timeout_s=timeout_s)
    except errors.ApiError as exc:
        refusal = _refusal(exc)
        if refusal is exc:
            raise
        raise refusal from exc


# --------------------------------------------------------------------------- #
# 0x2E write data by identifier
# --------------------------------------------------------------------------- #
@router.post("/writes/write-did", summary="Preview a 0x2E write (sends nothing)")
async def write_did_preview(request: Request, body: WriteDidPreviewRequest):
    worker = deps.get_worker(request)
    did = deps.parse_did(body.did)
    data = service.parse_hex_payload(body.data)
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)

    def job(ctx):
        session_id = ctx.open_session("write-did-preview", body.model_dump())
        try:
            result = service.preview_write_did(
                ctx.uds(), bus=body.bus, module=body.module, did=did, data=data,
                timeout_ms=timeout_ms, cancel=ctx.cancel)
        except Exception as exc:  # noqa: BLE001 - audit the refusal, then re-raise
            audit.record(ctx.store, session_id, "write-preview", None,
                         {"module": str(body.module), "bus": body.bus, "service": "2E",
                          "request": None, "outcome": "refused",
                          "error": str(exc)[:200]})
            ctx.close_session(session_id, "refused")
            raise
        entry = _store(request).issue(
            kind="write-did", module=result["module"], bus=body.bus,
            request=result["request"],
            params={"module": result["module"], "bus": body.bus, "did": "%04X" % did,
                    "data": data, "timeout_ms": timeout_ms,
                    "generation": _generation(ctx),
                    "expected_current": result.get("current")},
        )
        audit.record(ctx.store, session_id, "write-preview", result["request"],
                     {"module": result["module"], "bus": body.bus, "service": "2E",
                      "request": result["request"], "current": result.get("current"),
                      "outcome": "previewed"})
        ctx.close_session(session_id, "ok")
        result["confirm_token"] = entry.token
        result["expires_in_s"] = entry.ttl_s
        result["acknowledgement"] = entry.module
        result["warning"] = SGWRISK
        return result

    return await _run(worker, "write-did-preview", job, timeout_s=30.0)


@router.post("/writes/write-did/confirm", summary="Execute a reviewed 0x2E write")
async def write_did_confirm(request: Request, body: WriteConfirmRequest):
    worker = deps.get_worker(request)
    try:
        entry = _store(request).consume(body.confirm_token, kind="write-did",
                                        acknowledgement=body.acknowledgement)
    except ConfirmError as exc:
        raise errors.ApiError("confirm_stale", "confirm", str(exc),
                              status=exc.status) from exc
    params = dict(entry.params)

    def job(ctx):
        session_id = ctx.open_session("write-did", params)
        if params.get("generation") not in (None, _generation(ctx)):
            audit.record(ctx.store, session_id, "write-send", entry.request,
                         {"module": params["module"], "bus": params["bus"], "service": "2E",
                          "request": entry.request, "sent": False,
                          "outcome": "generation_mismatch"})
            ctx.close_session(session_id, "generation_mismatch")
            raise errors.ApiError(
                "confirm_stale", "confirm",
                "the adapter connection changed since the preview; review again",
                status="generation_mismatch")
        # Acquire the adapter *before* the final authorisation: acquisition can
        # itself bump the generation, and the token's TTL can lapse while the
        # session is opening. The earlier check is a fast fail, not the gate.
        try:
            adapter = ctx.uds()
            _store(request).authorize(
                entry.token, kind="write-did",
                generation=str(getattr(ctx.worker.backend, "generation", 0)))
        except ConfirmError as exc:
            audit.record(ctx.store, session_id, "write-send", entry.request,
                         {"module": params["module"], "bus": params["bus"], "service": "2E",
                          "request": entry.request, "sent": False, "outcome": exc.status})
            ctx.close_session(session_id, exc.status)
            raise errors.ApiError("confirm_stale", "confirm", str(exc),
                                  status=exc.status) from exc
        try:
            result = service.execute_write_did(
                adapter, bus=params["bus"], module=params["module"],
                did=int(params["did"], 16), data=params["data"],
                timeout_ms=int(params.get("timeout_ms", 100)),
                expected_current=params.get("expected_current"),
                cancel=ctx.cancel)
        except errors.ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - audit, never claim transmission
            audit.record(ctx.store, session_id, "write-send", entry.request,
                         {"module": params["module"], "bus": params["bus"], "service": "2E",
                          "request": entry.request, "sent": None, "outcome": "error",
                          "error": str(exc)[:200]})
            ctx.close_session(session_id, "error")
            raise
        result["audit"] = audit.record(
            ctx.store, session_id, "write-send", result.get("request") or entry.request,
            {"module": params["module"], "bus": params["bus"], "service": "2E",
             "request": result.get("request"), "did": params["did"],
             "data": params["data"], "sent": bool(result.get("sent")),
             "outcome": result.get("status"),
             "current": result.get("current")})
        if result.get("status") == "confirm_stale":
            ctx.close_session(session_id, "confirm_stale")
            raise errors.ApiError("confirm_stale", "confirm",
                                  result.get("reason") or "the value changed",
                                  status="precondition_changed",
                                  detail={"current": result.get("current")})
        ctx.close_session(session_id, result.get("status") or "ok")
        return result

    return await _run(worker, "write-did", job, timeout_s=60.0)


# --------------------------------------------------------------------------- #
# 0x31 routine control
# --------------------------------------------------------------------------- #
@router.post("/writes/routine", summary="Preview a 0x31 routine (sends nothing)")
async def routine_preview(request: Request, body: RoutinePreviewRequest):
    worker = deps.get_worker(request)
    rid = deps.parse_did(body.rid)
    data = service.parse_hex_payload(body.data, allow_empty=True)
    if int(body.sub) not in (1, 2, 3):
        raise errors.validation("routine subfunction must be 1, 2 or 3")
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)

    def job(ctx):
        session_id = ctx.open_session("routine-preview", body.model_dump())
        try:
            result = service.preview_routine(
                ctx.uds(), bus=body.bus, module=body.module, rid=rid,
                sub=int(body.sub), data=data, timeout_ms=timeout_ms, cancel=ctx.cancel)
        except Exception as exc:  # noqa: BLE001 - audit then re-raise
            audit.record(ctx.store, session_id, "write-preview", None,
                         {"module": str(body.module), "bus": body.bus, "service": "31",
                          "request": None, "outcome": "refused",
                          "error": str(exc)[:200]})
            ctx.close_session(session_id, "refused")
            raise
        entry = _store(request).issue(
            kind="routine", module=result["module"], bus=body.bus,
            request=result["request"],
            params={"module": result["module"], "bus": body.bus,
                    "rid": "%04X" % rid, "sub": int(body.sub), "data": data,
                    "timeout_ms": timeout_ms, "generation": _generation(ctx)},
        )
        audit.record(ctx.store, session_id, "write-preview", result["request"],
                     {"module": result["module"], "bus": body.bus, "service": "31",
                      "request": result["request"], "outcome": "previewed"})
        ctx.close_session(session_id, "ok")
        result["confirm_token"] = entry.token
        result["expires_in_s"] = entry.ttl_s
        result["acknowledgement"] = entry.module
        result["warning"] = SGWRISK
        return result

    return await _run(worker, "routine-preview", job, timeout_s=30.0)


@router.post("/writes/routine/confirm", summary="Execute a reviewed 0x31 routine")
async def routine_confirm(request: Request, body: WriteConfirmRequest):
    worker = deps.get_worker(request)
    try:
        entry = _store(request).consume(body.confirm_token, kind="routine",
                                        acknowledgement=body.acknowledgement)
    except ConfirmError as exc:
        raise errors.ApiError("confirm_stale", "confirm", str(exc),
                              status=exc.status) from exc
    params = dict(entry.params)

    def job(ctx):
        session_id = ctx.open_session("routine", params)
        if params.get("generation") not in (None, _generation(ctx)):
            audit.record(ctx.store, session_id, "write-send", entry.request,
                         {"module": params["module"], "bus": params["bus"], "service": "31",
                          "request": entry.request, "sent": False,
                          "outcome": "generation_mismatch"})
            ctx.close_session(session_id, "generation_mismatch")
            raise errors.ApiError(
                "confirm_stale", "confirm",
                "the adapter connection changed since the preview; review again",
                status="generation_mismatch")
        try:
            adapter = ctx.uds()
            _store(request).authorize(
                entry.token, kind="routine",
                generation=str(getattr(ctx.worker.backend, "generation", 0)))
        except ConfirmError as exc:
            audit.record(ctx.store, session_id, "write-send", entry.request,
                         {"module": params["module"], "bus": params["bus"], "service": "31",
                          "request": entry.request, "sent": False, "outcome": exc.status})
            ctx.close_session(session_id, exc.status)
            raise errors.ApiError("confirm_stale", "confirm", str(exc),
                                  status=exc.status) from exc
        try:
            result = service.execute_routine(
                adapter, bus=params["bus"], module=params["module"],
                rid=int(params["rid"], 16), sub=int(params["sub"]),
                data=params.get("data", ""),
                timeout_ms=int(params.get("timeout_ms", 100)), cancel=ctx.cancel)
        except errors.ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - audit, never claim transmission
            audit.record(ctx.store, session_id, "write-send", entry.request,
                         {"module": params["module"], "bus": params["bus"], "service": "31",
                          "request": entry.request, "sent": None, "outcome": "error",
                          "error": str(exc)[:200]})
            ctx.close_session(session_id, "error")
            raise
        result["audit"] = audit.record(
            ctx.store, session_id, "write-send", result.get("request"),
            {"module": params["module"], "bus": params["bus"], "service": "31",
             "request": result.get("request"), "rid": params["rid"],
             "sub": params["sub"], "data": params.get("data", ""),
             "sent": bool(result.get("sent")),
             "outcome": result.get("status")})
        ctx.close_session(session_id, result.get("status") or "ok")
        return result

    return await _run(worker, "routine", job, timeout_s=60.0)
