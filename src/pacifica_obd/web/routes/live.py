"""Live streaming routes: PID watch and UDS DID monitor (design §3.3, §4.4).

The streams are ordinary jobs on the :class:`BusWorker` thread that publish
events into their :class:`StreamChannel`; the HTTP handler only fans those events
out as SSE. Because the acquisition job *holds the bus*, a concurrent scan is
refused with 409 ``bus_busy`` rather than interleaving on the wire.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from pacifica_obd.adapter import configure_bus
from pacifica_obd.profiles import response_id as module_response_id
from pacifica_obd.uds import enter_session, read_did
from pacifica_obd.web import deps, errors, service
from pacifica_obd.web.schemas import MonitorStartRequest, StopRequest, WatchStartRequest

router = APIRouter()

DEFAULT_WATCH = ["RPM", "SPEED", "COOLANT_TEMP", "ENGINE_LOAD", "CONTROL_MODULE_VOLTAGE"]

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _pid_hex(command) -> str | None:
    text = service.command_text(command)
    if text.startswith("01") and len(text) >= 4:
        return text[2:4]
    return None


def _pid_entry(command, resp) -> dict:
    base = {"key": command.name, "label": command.name, "module": "PCM",
            "did": _pid_hex(command), "value": None, "unit": None, "text": None}
    if resp.is_null():
        base["status"] = "no_response"
        return base
    payload = service.jsonable(resp.value)
    if isinstance(payload, dict) and "value" in payload:
        base.update({"value": payload.get("value"), "unit": payload.get("unit"), "status": "ok"})
    else:
        base.update({"text": str(payload), "status": "ok"})
    return base


def _watch_body(params: dict):
    def job(ctx):
        import obd

        conn = ctx.obd()
        names = params["values"] or DEFAULT_WATCH
        requested = [getattr(obd.commands, name) for name in names if hasattr(obd.commands, name)]
        commands = [c for c in requested if service.is_read_only_command(c)]
        if not commands:
            refused = sorted({c.name for c in requested if not service.is_read_only_command(c)}
                             | {name for name in names if not hasattr(obd.commands, name)})
            raise ValueError(f"not a read-only PID: {', '.join(refused) or ', '.join(names)}")
        interval = max(0.05, params["interval_ms"] / 1000)
        recording = bool(params.get("store"))
        session_id = ctx.open_session("watch", params) if recording else None
        vin = None
        if recording and ctx.store is not None:
            row = ctx.store.latest_vehicle()
            vin = row.get("vin") if row else None
        count = 0
        started = time.monotonic()
        ctx.emit("status", {"state": "running", "source": "pid",
                            "requested_ms": params["interval_ms"], "interval_s": interval,
                            "signals": [c.name for c in commands], "recording": recording})
        try:
            while not ctx.cancelled():
                cycle = time.monotonic()
                values: dict[str, dict] = {}
                for command in commands:
                    if ctx.cancelled():
                        break
                    resp = conn.query(command)
                    entry = _pid_entry(command, resp)
                    values[entry["key"]] = entry
                    if recording and ctx.store is not None:
                        ctx.store.add_sample(
                            session_id, vin, "can-c", entry["module"], entry["did"] or "?",
                            entry["label"], entry["value"], entry["text"], entry["unit"],
                            entry["status"], None,
                        )
                count += 1
                elapsed = time.monotonic() - started
                ctx.emit("sample", {"t": _now_ms(), "seq": count, "source": "pid",
                                    "values": values})
                ctx.emit("status", {"state": "running", "source": "pid", "samples": count,
                                    "requested_ms": params["interval_ms"],
                                    "achieved_hz": round(count / elapsed, 2) if elapsed else None,
                                    "recording": recording})
                delay = interval - (time.monotonic() - cycle)
                if delay > 0:
                    time.sleep(delay)
        finally:
            ctx.close_session(session_id, "ok", vin)
            ctx.emit("status", {"state": "stopped", "samples": count})
        return {"samples": count, "achieved_hz": round(count / max(1e-6, time.monotonic() - started), 2)}

    return job


def _monitor_body(params: dict):
    def job(ctx):
        adapter = ctx.uds()
        dids = [service.parse_did(did) for did in params["dids"]]
        if not dids:
            raise ValueError("no DIDs selected")
        wait = service.read_wait(params["timeout_ms"])
        bus = params["bus"]
        interval = max(0.05, params["interval_ms"] / 1000)
        recording = bool(params.get("store"))
        session_id = ctx.open_session("monitor", params) if recording else None

        if params.get("module") is not None:
            modules = [service.as_request_id(params["module"])]
        else:
            modules = []

        configure_bus(adapter, bus, params["timeout_ms"])
        if not modules:
            modules = [target["request_id"] for target in service.probe_modules(
                adapter, start=params["start"], end=params["end"], wait=wait,
                receive=module_response_id, cancel=ctx.cancel,
            )]
        if not modules:
            raise ValueError("no modules responded")
        expects: dict[str, str | None] = {}
        for rid in modules:
            mapped, ok = service.address_module(adapter, rid)
            if not ok:
                continue
            expects[rid] = enter_session(adapter, rid, wait) or mapped
        count = 0
        started = time.monotonic()
        ctx.emit("status", {"state": "running", "source": "did", "modules": modules,
                            "dids": ["%04X" % did for did in dids],
                            "requested_ms": params["interval_ms"], "recording": recording})
        try:
            while not ctx.cancelled():
                cycle = time.monotonic()
                values: dict[str, dict] = {}
                for rid in modules:
                    if ctx.cancelled():
                        break
                    mapped, ok = service.address_module(adapter, rid)
                    if not ok:
                        continue
                    expects[rid] = enter_session(adapter, rid, wait) or mapped
                    for did in dids:
                        record = read_did(adapter, did, wait, expect_header=expects[rid])
                        key = "%s:%04X" % (rid, did)
                        number = None
                        hex_value = record.get("hex")
                        if hex_value and len(hex_value) <= 4:
                            try:
                                number = int(hex_value, 16)
                            except ValueError:
                                number = None
                        entry = {
                            "key": key, "label": record.get("name") or "%04X" % did,
                            "module": rid, "did": "%04X" % did, "value": number,
                            "unit": None, "text": record.get("ascii") or hex_value,
                            "status": record["status"],
                        }
                        values[key] = entry
                        if recording and ctx.store is not None:
                            ctx.store.add_sample(
                                session_id, None, bus, rid, entry["did"], entry["label"],
                                number, entry["text"], None, entry["status"], record.get("raw"),
                            )
                count += 1
                elapsed = time.monotonic() - started
                ctx.emit("sample", {"t": _now_ms(), "seq": count, "source": "did",
                                    "values": values})
                ctx.emit("status", {"state": "running", "source": "did", "samples": count,
                                    "requested_ms": params["interval_ms"],
                                    "achieved_hz": round(count / elapsed, 2) if elapsed else None,
                                    "recording": recording})
                delay = interval - (time.monotonic() - cycle)
                if delay > 0:
                    time.sleep(delay)
        finally:
            ctx.close_session(session_id, "ok", None)
            ctx.emit("status", {"state": "stopped", "samples": count})
        return {"samples": count}

    return job


@router.post("/live/watch/start", summary="Start a PID watch (SSE stream)")
async def watch_start(request: Request, body: WatchStartRequest):
    worker = deps.get_worker(request)
    # Validate *before* starting: a watch must never be the path that sends a
    # mode-04 clear. The allowlist is the same one the service layer uses.
    if body.source == "pid":
        import obd as obd_commands

        for name in body.values:
            command = getattr(obd_commands.commands, name, None)
            if command is None:
                raise errors.validation(f"unknown PID: {name}")
            if not service.is_read_only_command(command):
                raise errors.validation(f"not a read-only PID: {name}")
    params = {
        "source": body.source, "values": body.values, "module": body.module,
        "bus": body.bus, "start": deps.parse_can_id(body.start),
        "end": deps.parse_can_id(body.end), "timeout_ms": deps.parse_timeout_ms(body.timeout_ms),
        "interval_ms": max(50, int(body.interval_ms)), "store": body.store,
    }
    body_fn = _watch_body(params) if body.source == "pid" else _monitor_body({**params, "dids": body.values})
    stream_id = deps.start_stream(worker, "watch" if body.source == "pid" else "monitor", body_fn)
    return {"stream_id": stream_id, "source": body.source, "params": params}


@router.post("/uds/monitor/start", summary="Start a UDS DID monitor (SSE stream)")
async def monitor_start(request: Request, body: MonitorStartRequest):
    worker = deps.get_worker(request)
    params = {
        "source": "did", "dids": body.dids, "module": body.module, "bus": body.bus,
        "start": deps.parse_can_id(body.start), "end": deps.parse_can_id(body.end),
        "timeout_ms": deps.parse_timeout_ms(body.timeout_ms),
        "interval_ms": max(50, int(body.interval_ms)), "store": body.store,
    }
    stream_id = deps.start_stream(worker, "monitor", _monitor_body(params))
    return {"stream_id": stream_id, "source": "did", "params": params}


@router.get("/live/streams", summary="Active and recent streams")
async def streams(request: Request):
    worker = deps.get_worker(request)
    return {
        "streams": [
            {**item.snapshot(), "subscribers": item.channel.subscribers}
            for item in worker.jobs() if item.holds_bus
        ]
    }


@router.post("/live/watch/stop", summary="Stop a stream")
async def watch_stop(request: Request, body: StopRequest):
    worker = deps.get_worker(request)
    job = worker.get(body.stream_id)
    if job is None:
        raise errors.not_found(f"no stream {body.stream_id}", code="stream_not_found")
    worker.cancel(body.stream_id)
    job.channel.publish("done", {"reason": "stopped"})
    return {"stopped": True, "stream_id": body.stream_id}


@router.post("/uds/monitor/stop", summary="Stop a UDS monitor")
async def monitor_stop(request: Request, body: StopRequest):
    return await watch_stop(request, body)


def _int_or_none(text: str | None) -> int | None:
    try:
        return int(text) if text not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def _sse(job, last_event_id: int | None) -> StreamingResponse:
    async def generator():
        loop = asyncio.get_running_loop()
        subscription, queue = job.channel.subscribe(loop, last_event_id)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    seq, event, data = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # heartbeat: keeps idle proxies from closing
                    continue
                if event in ("closed", "done"):
                    # One terminal event, then end the response: a subscriber that
                    # arrives after the job finished must not get heartbeats forever.
                    yield f"id: {seq}\nevent: done\ndata: {json.dumps(data, default=str)}\n\n"
                    break
                yield f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            job.channel.unsubscribe(subscription)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


@router.get("/live/watch/stream/{stream_id}", summary="SSE: live samples")
async def watch_stream(request: Request, stream_id: str,
                       last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
    job = deps.get_worker(request).get(stream_id)
    if job is None:
        raise errors.not_found(f"no stream {stream_id}", code="stream_not_found")
    return await _sse(job, _int_or_none(last_event_id))


@router.get("/uds/monitor/stream/{stream_id}", summary="SSE: live UDS samples")
async def monitor_stream(request: Request, stream_id: str,
                         last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
    return await watch_stream(request, stream_id, last_event_id)
