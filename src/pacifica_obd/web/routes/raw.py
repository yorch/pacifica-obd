"""Raw console routes (design §3.14): one bounded command, tier-classified.

* ``GET  /api/raw/allowlist`` — the read/confirm/deny tables as data, so the UI
  and the tests share :mod:`pacifica_obd.raw_guard` as one source of truth. With
  ``?command=`` it also returns the classification of that command, which is how
  the console's tier badge stays exactly in step with the server.
* ``POST /api/raw`` — send one command. The **read** tier is sent immediately
  through the serialized :class:`BusWorker` job path (the handler never touches
  the adapter). The **confirm** tier is refused in read-only mode; in write mode
  it acquires the adapter, binds the token to that adapter generation, and
  returns a single-use confirm token. The **deny** tier (including anything the
  parser rejects) is refused with 4xx and never reaches the adapter.
* ``POST /api/raw/confirm`` — accept **only** the token and the typed
  acknowledgement, and send the reviewed bytes. Mirrors ``routes/writes.py``: the
  command bytes live server-side, the tier and the bytes are re-checked inside the
  same job, the adapter is acquired before the generation check, the cancellation
  flag is checked immediately before transmit, and the outcome is written to the
  ``events`` table with the exact bytes.

Two bypasses were closed after adversarial review (see ``tests/test_web_raw.py``):

* **Framing.** ``ATCAF0`` is refused outright: with auto-formatting off the
  adapter sends the hex bytes literally, so the first data byte becomes the
  ISO-TP PCI and ``0104000000000000`` would deliver service ``04`` (clear DTCs)
  while classifying as a mode-01 read. Before every hex request this module still
  re-asserts ``ATCAF1`` and refuses if the adapter does not confirm it, so framing
  left off by something else (a prior CLI ``raw``, another app) cannot smuggle a
  service through this endpoint either.
* **Argument-bearing AT writes.** ``ATWM``/``ATSW`` take an argument, so the deny
  families are matched as prefixes, not exact strings.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from pacifica_obd import raw_guard
from pacifica_obd.adapter import AdapterError
from pacifica_obd.uds import collect_frames, messages_from_frames, parse_uds_frame
from pacifica_obd.web import audit, deps, errors, service
from pacifica_obd.web.confirm import ConfirmError, ConfirmTokenStore
from pacifica_obd.web.schemas import RawCommandRequest, RawConfirmRequest

router = APIRouter()

RAW_WARNING = (
    "The exact bytes go to the adapter and can change its state (headers, "
    "filters, protocol framing). This tool cannot undo that."
)


def _store(request: Request) -> ConfirmTokenStore:
    return request.app.state.confirm


def _generation(ctx) -> str:
    return str(getattr(ctx.worker.backend, "generation", 0))


def _acknowledged(lines) -> bool:
    """True only for a clean ``OK``.

    Substring matching accepted ``"NOT OK"`` and ``"OK\\n?"``, which would let a
    request through after the adapter refused the command it depends on.
    """
    for line in lines or []:
        text = str(line).strip().upper()
        if not text:
            continue
        if text in {"OK", "OK."}:
            return True
        # any other non-empty line is a refusal ("?", "NOT OK", "ERROR", "NO DATA")
        return False
    return False


def _refused(decision: raw_guard.Decision) -> errors.ApiError:
    return errors.ApiError(
        "raw_refused", "refused", decision.reason, http_status=403,
        status="refused",
        detail={"tier": decision.tier, "command": decision.command,
                "note": decision.note, "pointer": decision.pointer},
    )


def _parse_lines(lines) -> tuple[str | None, str | None, list[dict]]:
    """Reuse the UDS frame reassembly for the raw adapter lines."""
    text_lines = [str(line) for line in lines]
    frames: list[dict] = []
    for header, group in collect_frames(text_lines):
        for message in messages_from_frames(group):
            frames.append({"response_id": header, "uds": message,
                           "service": message[:2] if len(message) >= 2 else None})
    response_id, uds_hex = parse_uds_frame(text_lines)
    return response_id, uds_hex, frames


def _result(decision: raw_guard.Decision, lines, *, sent: bool) -> dict:
    response_id, uds_hex, frames = _parse_lines(lines)
    return {
        "tier": decision.tier,
        "command": decision.command,
        "kind": decision.kind,
        "service": decision.service,
        "reason": decision.reason,
        "note": decision.note,
        "pointer": decision.pointer,
        "lines": [str(line) for line in lines],
        "response_id": response_id,
        "uds": uds_hex,
        "frames": frames,
        "sent": sent,
    }


def _send(adapter, decision: raw_guard.Decision, timeout_ms: int, cancel=None) -> list[str]:
    """Send exactly the classified bytes, in automatic ISO-TP framing."""
    if decision.kind == "hex":
        # Framing off turns the first data byte into the PCI, so a hex "read" could
        # deliver any service; and CAN extended addressing (ATCEA) moves the first
        # data byte into the address position, which ATCAF1 does not undo. Restore
        # framing, refuse if it is not accepted, and re-check the cancellation that
        # may have arrived during that exchange.
        if not _acknowledged(adapter.cmd("ATCAF1")):
            raise AdapterError(
                "the adapter would not restore ISO-TP automatic formatting "
                "(ATCAF1); refusing to send a hex request"
            )
        if cancel is not None and cancel.is_set():
            raise AdapterError("cancelled before the request was sent")
    return list(adapter.cmd(decision.command, wait=service.read_wait(timeout_ms)))


# --------------------------------------------------------------------------- #
# the tiers as data
# --------------------------------------------------------------------------- #
@router.get("/raw/allowlist", summary="Raw console tiers (read / confirm / deny)")
async def allowlist(request: Request, command: str | None = None):
    payload = raw_guard.allowlist()
    payload["read_only"] = bool(deps.get_settings(request).read_only)
    if command is not None:
        payload["decision"] = raw_guard.classify(command).as_dict()
    return payload


# --------------------------------------------------------------------------- #
# send (read) or preview (confirm)
# --------------------------------------------------------------------------- #
@router.post("/raw", summary="Send one raw command (read tier) or preview it (confirm tier)")
async def raw_send(request: Request, body: RawCommandRequest):
    worker = deps.get_worker(request)
    decision = raw_guard.classify(body.command)
    if decision.kind == "invalid":
        raise errors.validation(decision.reason, code="raw_invalid")
    if decision.tier == raw_guard.DENY:
        raise _refused(decision)

    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)

    if decision.tier == raw_guard.CONFIRM:
        if deps.get_settings(request).read_only:
            raise errors.readonly(
                "raw confirm-tier commands need --allow-writes; the read tier "
                "is always available"
            )

        def preview_job(ctx):
            # Acquire the adapter so the token binds the generation of the
            # connection that will actually send the bytes.
            ctx.uds()
            entry = _store(request).issue(
                kind="raw", module=decision.command, bus=worker.bus,
                request=decision.command, ack=decision.command,
                params={"command": decision.command, "timeout_ms": timeout_ms,
                        "generation": _generation(ctx)},
                ttl_s=120.0,
            )
            session_id = ctx.open_session("raw-preview", {"command": decision.command})
            audit.record(ctx.store, session_id, "raw-preview", decision.command,
                         {"command": decision.command, "tier": raw_guard.CONFIRM,
                          "sent": False, "outcome": "previewed"})
            ctx.close_session(session_id, "ok")
            return {"token": entry.token, "ack": entry.ack, "ttl_s": entry.ttl_s}

        prepared = await deps.run_job(worker, "raw-preview", preview_job,
                                      exclusive=True, timeout_s=30.0)
        return {
            "tier": raw_guard.CONFIRM,
            "sent": False,
            "command": decision.command,
            "reason": decision.reason,
            "note": decision.note,
            "pointer": decision.pointer,
            "confirm_token": prepared["token"],
            "acknowledgement": prepared["ack"],
            "request": decision.command,
            "expires_in_s": prepared["ttl_s"],
            "warning": RAW_WARNING,
        }

    def job(ctx):
        session_id = ctx.open_session("raw", {"command": decision.command,
                                              "tier": raw_guard.READ})
        try:
            adapter = ctx.uds()
            # Checked after acquisition, immediately before the transmit.
            if ctx.cancelled():
                raise service.OperationCancelled("cancelled before transmit")
            lines = _send(adapter, decision, timeout_ms, ctx.cancel)
        except service.OperationCancelled:
            ctx.close_session(session_id, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - audit, never claim transmission
            audit.record(ctx.store, session_id, "raw-send", decision.command,
                         {"command": decision.command, "tier": raw_guard.READ,
                          "sent": None, "outcome": "error", "error": str(exc)[:200]})
            ctx.close_session(session_id, "error")
            raise
        result = _result(decision, lines, sent=True)
        result["audit"] = audit.record(
            ctx.store, session_id, "raw-send", decision.command,
            {"command": decision.command, "tier": raw_guard.READ, "sent": True,
             "outcome": "ok", "lines": result["lines"][:16],
             "response_id": result["response_id"]})
        ctx.close_session(session_id, "ok")
        return result

    return await deps.run_job(worker, "raw", job, exclusive=True, timeout_s=30.0)


# --------------------------------------------------------------------------- #
# confirm (write mode only): send the reviewed bytes
# --------------------------------------------------------------------------- #
@router.post("/raw/confirm", summary="Send a reviewed confirm-tier raw command")
async def raw_confirm(request: Request, body: RawConfirmRequest):
    worker = deps.get_worker(request)
    try:
        entry = _store(request).consume(body.confirm_token, kind="raw",
                                        acknowledgement=body.acknowledgement)
    except ConfirmError as exc:
        raise errors.ApiError("confirm_stale", "confirm", str(exc),
                              status=exc.status) from exc

    params = dict(entry.params)
    decision = raw_guard.classify(params.get("command"))
    # Defence in depth: a token may never carry a deny-tier or malformed command
    # onto the transmit path, and the bytes must be exactly what was previewed.
    if (decision.kind == "invalid" or decision.tier != raw_guard.CONFIRM
            or decision.command != entry.request):
        raise errors.ApiError("confirm_stale", "confirm",
                              "this token does not name a confirm-tier command",
                              status="tier_mismatch")
    timeout_ms = int(params.get("timeout_ms", 100))

    def job(ctx):
        session_id = ctx.open_session("raw", {"command": decision.command,
                                              "tier": raw_guard.CONFIRM})
        try:
            adapter = ctx.uds()
            if params.get("generation") != _generation(ctx):
                audit.record(ctx.store, session_id, "raw-send", decision.command,
                             {"command": decision.command, "tier": raw_guard.CONFIRM,
                              "sent": False, "outcome": "generation_mismatch"})
                ctx.close_session(session_id, "generation_mismatch")
                raise errors.ApiError(
                    "confirm_stale", "confirm",
                    "the adapter connection changed since the preview; submit again",
                    status="generation_mismatch")
            if ctx.cancelled():
                raise service.OperationCancelled("cancelled before transmit")
            lines = _send(adapter, decision, timeout_ms, ctx.cancel)
        except service.OperationCancelled:
            ctx.close_session(session_id, "cancelled")
            raise
        except errors.ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - audit, never claim transmission
            audit.record(ctx.store, session_id, "raw-send", decision.command,
                         {"command": decision.command, "tier": raw_guard.CONFIRM,
                          "sent": None, "outcome": "error", "error": str(exc)[:200]})
            ctx.close_session(session_id, "error")
            raise
        result = _result(decision, lines, sent=True)
        result["audit"] = audit.record(
            ctx.store, session_id, "raw-send", decision.command,
            {"command": decision.command, "tier": raw_guard.CONFIRM, "sent": True,
             "outcome": "ok", "lines": result["lines"][:16],
             "response_id": result["response_id"]})
        ctx.close_session(session_id, "ok")
        return result

    return await deps.run_job(worker, "raw", job, exclusive=True, timeout_s=30.0)
