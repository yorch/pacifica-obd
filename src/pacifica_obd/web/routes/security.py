"""Security-access (``0x27``) routes (design §3.7).

Four explicit steps, nothing implicit:

* ``POST /api/security/explain`` — offline selector explanation; a refusal lists
  the candidate algorithms and an unlock is impossible.
* ``POST /api/security/seed``    — read one seed (read-only).
* ``POST /api/security/preview`` — read the seed, resolve the algorithm, compute
  the key and show the exact wire request; returns a **confirm token** bound to
  the held seed. Sends nothing.
* ``POST /api/security/unlock``  — accept only the token plus the typed module id
  and send the *reviewed* key against the held seed.

Owner decision §9.2 governs the freshness rule: the default path never issues a
second ``27 <level>`` request. ``strict_seed_reread`` (a server setting, never a
client field) re-reads and compares instead; a changed seed aborts with no key
sent.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request

from pacifica_obd.security import SecurityLeaseStore, SendRefused
from pacifica_obd.web import audit, deps, errors, service
from pacifica_obd.web.schemas import (
    SecurityPreviewRequest,
    SecuritySeedRequest,
    SecurityTargetRequest,
    SecurityUnlockRequest,
)

router = APIRouter()
#: The unlock route is registered *only* when writes are enabled; read-only mode
#: replaces it with a 403 handler in the app factory.
unlock_router = APIRouter()


def _leases(request: Request) -> SecurityLeaseStore:
    return request.app.state.security


def _field_ids(body) -> dict:
    return {
        "device_id": body.device_id,
        "variant_id": body.variant_id,
        "supplier": body.supplier,
    }


@router.post("/security/explain", summary="Explain the 0x27 algorithm choice (offline)")
async def explain(request: Request, body: SecurityTargetRequest):
    if body.level % 2 == 0 or not 0x01 <= body.level <= 0x7D:
        raise errors.validation("security level must be an odd value in 0x01..0x7D")
    info = await asyncio.to_thread(
        service.security_explain, body.module, body.level, body.device_id,
        body.variant_id, body.supplier,
    )
    return {
        "status": info.get("status"),
        "reason": info.get("reason"),
        "algorithm": info.get("algorithm"),
        "candidates": info.get("candidates", []),
        "ecu": info.get("ecu"),
        "request_id": info.get("request_id"),
        "level": body.level,
        "sent": False,
    }


@router.post("/security/seed", summary="Read one 0x27 seed (no key sent)")
async def seed(request: Request, body: SecuritySeedRequest):
    worker = deps.get_worker(request)
    if body.level % 2 == 0 or not 0x01 <= body.level <= 0x7D:
        raise errors.validation("security level must be an odd value in 0x01..0x7D")
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)

    def job(ctx):
        adapter = ctx.uds()
        result = service.read_security_seed(adapter, bus=body.bus, module=body.module,
                                            level=body.level, timeout_ms=timeout_ms,
                                            cancel=ctx.cancel)
        session_id = ctx.open_session("security-seed", body.model_dump())
        audit.record(ctx.store, session_id, "security-seed",
                     f"{result.get('module')} 27{body.level:02X}",
                     {"module": result.get("module"), "bus": body.bus,
                      "level": "%02X" % body.level, "status": result.get("status"),
                      "seed_bytes": len(result.get("seed") or "") // 2})
        ctx.close_session(session_id, result.get("status") or "ok")
        result["sent"] = False
        return result

    # A fresh seed request supersedes any lease previously previewed for this
    # target, so an old token cannot send a key against a newer challenge.
    _leases(request).invalidate(body.module, body.level, body.bus)
    return await deps.run_job(worker, "security-seed", job, exclusive=True, timeout_s=30.0)


@router.post("/security/preview", summary="Compute the key and show the exact bytes (sends nothing)")
async def preview(request: Request, body: SecurityPreviewRequest):
    worker = deps.get_worker(request)
    if body.level % 2 == 0 or not 0x01 <= body.level <= 0x7D:
        raise errors.validation("security level must be an odd value in 0x01..0x7D")
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)
    info = await asyncio.to_thread(
        service.security_explain, body.module, body.level, body.device_id,
        body.variant_id, body.supplier,
    )
    if info.get("status") != "resolved":
        # Refuse **before** touching the bus: no key on an unresolved choice.
        return {
            "status": info.get("status", "unmapped"),
            "reason": info.get("reason"),
            "algorithm": None,
            "candidates": info.get("candidates", []),
            "ecu": info.get("ecu"),
            "request_id": info.get("request_id"),
            "seed": None, "key": None, "request": None,
            "confirm_token": None, "sent": False,
        }

    # A new challenge supersedes any earlier preview for this target, even if
    # this read later fails.
    lease_store = _leases(request)

    def job(ctx):
        adapter = ctx.uds()
        # Reserve the challenge epoch *before* reading the seed. Stamping the lease
        # at issue time (after the read) let a racing seed request promote an older
        # seed to the newest epoch, so both consume() and authorize() accepted it.
        epoch = lease_store.reserve(body.module, body.level, body.bus)
        result = service.preview_security_key(
            adapter, bus=body.bus, module=body.module, level=body.level,
            device_id=body.device_id, variant_id=body.variant_id,
            supplier=body.supplier, timeout_ms=timeout_ms, cancel=ctx.cancel,
        )
        result["generation"] = str(getattr(ctx.worker.backend, "generation", 0))
        result["epoch"] = epoch
        # The TTL starts when the seed was acquired, not when the lease was issued.
        result["acquired"] = time.monotonic()
        session_id = ctx.open_session("security-preview", body.model_dump(exclude={"timeout_ms"}))
        audit.record(ctx.store, session_id, "security-preview",
                     f"{result.get('module')} 27{body.level:02X}",
                     {"module": result.get("module"), "bus": body.bus,
                      "level": "%02X" % body.level, "status": result.get("status"),
                      "algorithm": result.get("algorithm"),
                      "seed_bytes": len(result.get("seed") or "") // 2,
                      "request": (result.get("request") or "")[:4] + "…"
                      if result.get("request") else None,
                      "outcome": "previewed"})
        ctx.close_session(session_id, result.get("status") or "ok")
        return result

    result = await deps.run_job(worker, "security-preview", job, exclusive=True, timeout_s=30.0)
    if result.get("status") == "preview" and result.get("key") and result.get("seed"):
        generation = str(result.get("generation")
                         or getattr(worker.backend, "generation", 0))
        lease = lease_store.issue(
            result.get("module") or body.module, body.level, result["seed"],
            bus=body.bus, generation=generation, ttl_s=30.0,
            key_hex=result["key"], algorithm=result.get("algorithm"),
            params={"device_id": body.device_id, "variant_id": body.variant_id,
                    "supplier": body.supplier},
            epoch=result.get("epoch"), created=result.get("acquired"),
        )
        result["confirm_token"] = lease.token
        result["expires_in_s"] = lease.ttl_s
        result["bus"] = body.bus
        result["sent"] = False
    else:
        result["confirm_token"] = None
    return result


async def _unlock(request: Request, body: SecurityUnlockRequest):
    worker = deps.get_worker(request)
    lease_store = _leases(request)
    lease = lease_store.lookup(body.confirm_token)
    if lease is None:
        raise errors.ApiError("confirm_stale", "confirm",
                              "this preview expired; review the key again",
                              status="expired", detail={"reason": "expired"})
    if lease.used:
        raise errors.ApiError("confirm_stale", "confirm",
                              "this preview was already used; review the key again",
                              status="consumed", detail={"reason": "consumed"})
    # The typed acknowledgement must name the module, before any bus access.
    from pacifica_obd.security import normalize_module

    try:
        acknowledged = normalize_module(body.acknowledgement)
    except ValueError as exc:
        raise errors.ApiError("confirm_stale", "confirm", str(exc),
                              status="acknowledgement") from exc
    if acknowledged != lease.module:
        raise errors.ApiError(
            "confirm_stale", "confirm",
            f"type the module id ({lease.module}) to confirm",
            status="acknowledgement",
            detail={"module": lease.module},
        )

    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)
    strict = bool(getattr(deps.get_settings(request), "strict_seed_reread", False))

    def job(ctx):
        adapter = ctx.uds()
        session_id = ctx.open_session("security-unlock",
                                      {"module": lease.module, "level": lease.level,
                                       "bus": lease.bus, "strict": strict})

        def read_seed():
            current = service.read_security_seed(adapter, bus=lease.bus, module=lease.module,
                                                 level=lease.level, timeout_ms=timeout_ms,
                                                 cancel=ctx.cancel)
            return current.get("seed")

        decision = lease_store.consume(
            body.confirm_token, module=lease.module, level=lease.level, bus=lease.bus,
            generation=str(getattr(ctx.worker.backend, "generation", 0)),
            strict_seed_reread=strict, read_seed=read_seed,
        )
        if not decision.send:
            audit.record(ctx.store, session_id, "security-refused",
                         f"{lease.module} 27{lease.level:02X}",
                         {"module": lease.module, "bus": lease.bus,
                          "level": "%02X" % lease.level, "status": decision.status,
                          "reason": decision.reason, "sent": False})
            ctx.close_session(session_id, decision.status)
            raise errors.ApiError(
                "confirm_stale", "confirm", decision.reason,
                status=decision.status,
                detail={"reason": decision.status, "re_read": decision.re_read},
            )
        key_hex = decision.lease.key_hex if decision.lease else lease.key_hex
        if not key_hex:
            audit.record(ctx.store, session_id, "security-refused",
                         f"{lease.module} 27{lease.level:02X}",
                         {"module": lease.module, "status": "no_key", "sent": False})
            ctx.close_session(session_id, "no_key")
            raise errors.ApiError("confirm_stale", "confirm",
                                  "no key was held for this preview", status="no_key")
        try:
            result = service.send_security_key_guarded(
                adapter, bus=lease.bus, module=lease.module, level=lease.level,
                key_hex=key_hex, timeout_ms=timeout_ms, cancel=ctx.cancel,
                # Re-checked at the last instant: opening the session below can
                # outlive the lease, and a stale positive decision must not send.
                authorize=lambda: lease_store.authorize(
                    body.confirm_token, module=lease.module, level=lease.level,
                    bus=lease.bus,
                    generation=str(getattr(ctx.worker.backend, "generation", 0))),
            )
        except SendRefused as exc:
            audit.record(ctx.store, session_id, "security-refused",
                         f"{lease.module} 27{lease.level + 1:02X}",
                         {"module": lease.module, "bus": lease.bus,
                          "level": "%02X" % (lease.level + 1), "sent": False,
                          "outcome": "refused_at_transmit", "reason": str(exc)[:200]})
            ctx.close_session(session_id, "refused")
            raise errors.ApiError("confirm_stale", "confirm", str(exc),
                                  status="superseded") from exc
        except Exception as exc:  # noqa: BLE001 - audit the failure, then re-raise
            # The failure may have happened before or during the write, so the
            # audit says "unknown" rather than claiming the key went out.
            audit.record(ctx.store, session_id, "security-send",
                         f"{lease.module} 27{lease.level + 1:02X}",
                         {"module": lease.module, "bus": lease.bus,
                          "level": "%02X" % (lease.level + 1), "sent": None,
                          "outcome": "error", "error": str(exc)[:200],
                          "key_bytes": len(key_hex) // 2})
            ctx.close_session(session_id, "error")
            raise
        audit.record(ctx.store, session_id, "security-send",
                     f"{lease.module} 27{lease.level + 1:02X}",
                     {"module": lease.module, "bus": lease.bus,
                      "level": "%02X" % (lease.level + 1), "sent": True,
                      "outcome": result.get("status"),
                      "unlocked": bool(result.get("unlocked")),
                      "key_bytes": len(key_hex) // 2,
                      "seed_changed": bool(decision.re_read and result.get("status") == "rejected")})
        ctx.close_session(session_id, result.get("status") or "ok")
        result["algorithm"] = lease.algorithm
        result["strict"] = strict
        result["re_read"] = decision.re_read
        return result

    return await deps.run_job(worker, "security-unlock", job, exclusive=True, timeout_s=30.0)


@unlock_router.post("/security/unlock", summary="Send a reviewed 0x27 key (write)")
async def unlock(request: Request, body: SecurityUnlockRequest):
    return await _unlock(request, body)
