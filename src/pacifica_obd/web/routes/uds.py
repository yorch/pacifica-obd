"""UDS routes: discovery, all-module DTCs, DID reads, module info, capture.

All reads. The offline dataset endpoints (module map, DID/adaptation lists) never
touch hardware and run without the worker.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Query, Request

from pacifica_obd import pacifica_data
from pacifica_obd.database import EcuDatabase
from pacifica_obd.profiles import addresses as profile_addresses
from pacifica_obd.web import deps, errors, service
from pacifica_obd.web.schemas import (
    CaptureRequest,
    DiscoveryRequest,
    DtcAllRequest,
    ModuleInfoRequest,
    ReadDidRequest,
)

router = APIRouter()


def _id3(value) -> str:
    if value is None:
        return ""
    return str(value).replace("0x", "").replace("0X", "").upper().rjust(3, "0")


@router.post("/uds/modules", summary="Discover UDS modules on a CAN bus")
async def modules(request: Request, body: DiscoveryRequest):
    worker = deps.get_worker(request)
    start = deps.parse_can_id(body.start)
    end = deps.parse_can_id(body.end)
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)

    def job(ctx):
        adapter = ctx.uds()
        session_id = ctx.open_session("modules", body.model_dump())
        try:
            result = service.discover_modules(
                adapter, bus=body.bus, start=start, end=end, timeout_ms=timeout_ms,
                identify=body.identify, cancel=ctx.cancel,
                progress=lambda event: ctx.progress(**event),
            )
            if ctx.store is not None:
                for entry in result["modules"]:
                    ctx.store.add_module(session_id, None, body.bus, entry)
            return result
        finally:
            ctx.close_session(session_id)

    return await deps.run_job(worker, "uds-modules", job, timeout_s=600.0)


@router.post("/uds/dtc-all", summary="Read DTCs from every responding module")
async def dtc_all(request: Request, body: DtcAllRequest):
    worker = deps.get_worker(request)
    start = deps.parse_can_id(body.start)
    end = deps.parse_can_id(body.end)
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)

    def job(ctx):
        adapter = ctx.uds()
        session_id = ctx.open_session("dtc-all", body.model_dump())
        try:
            result = service.read_module_dtcs(
                adapter, bus=body.bus, start=start, end=end, module=body.module,
                timeout_ms=timeout_ms, cancel=ctx.cancel,
                progress=lambda event: ctx.progress(**event),
            )
            if ctx.store is not None:
                for record in result["modules"]:
                    ctx.store.add_dtcs(session_id, None, record["bus"], record["request_id"],
                                       record["dtcs"], record["raw"])
            return result
        finally:
            ctx.close_session(session_id)

    return await deps.run_job(worker, "uds-dtc-all", job, timeout_s=900.0)


@router.post("/uds/read-did", summary="Read Data Identifiers (UDS 0x22) from a module")
async def read_did(request: Request, body: ReadDidRequest):
    worker = deps.get_worker(request)
    module = deps.parse_can_id(body.module)
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)
    dids = [deps.parse_did(did) for did in body.dids]
    if not dids:
        raise errors.validation("at least one DID is required")

    def job(ctx):
        adapter = ctx.uds()
        session_id = ctx.open_session("read-did", body.model_dump())
        try:
            result = service.read_dids(adapter, bus=body.bus, module=module, dids=dids,
                                       timeout_ms=timeout_ms, cancel=ctx.cancel)
            if ctx.store is not None:
                for record in result["dids"]:
                    ctx.store.add_reading(
                        session_id, result.get("vin"), body.bus, result["module"],
                        record["did"], record.get("name"), record.get("hex"),
                        record.get("ascii"), record["status"], record.get("raw"),
                        category="did",
                    )
            return result
        finally:
            ctx.close_session(session_id, "ok", result.get("vin"))

    return await deps.run_job(worker, "uds-read-did", job, timeout_s=180.0)


@router.post("/uds/module-info", summary="Read the identification block F186..F191")
async def module_info(request: Request, body: ModuleInfoRequest):
    worker = deps.get_worker(request)
    start = deps.parse_can_id(body.start)
    end = deps.parse_can_id(body.end)
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)

    def job(ctx):
        adapter = ctx.uds()
        session_id = ctx.open_session("module-info", body.model_dump())
        try:
            result = service.module_info(
                adapter, bus=body.bus, module=body.module, start=start, end=end,
                timeout_ms=timeout_ms, cancel=ctx.cancel,
                progress=lambda event: ctx.progress(**event),
            )
            if ctx.store is not None:
                for record in result["modules"]:
                    ctx.store.add_reading(
                        session_id, result.get("vin"), record["bus"], record["module"],
                        record["did"], record.get("name"), record.get("hex"),
                        record.get("ascii"), record["status"], record.get("raw"),
                        category="module-info",
                    )
            return result
        finally:
            ctx.close_session(session_id, "ok", result.get("vin"))

    return await deps.run_job(worker, "uds-module-info", job, timeout_s=600.0)


@router.post("/uds/capture", summary="Full snapshot: modules + DTCs + ID block, stored")
async def capture(request: Request, body: CaptureRequest):
    worker = deps.get_worker(request)
    start = deps.parse_can_id(body.start)
    end = deps.parse_can_id(body.end)
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)
    buses = ["can-c", "can-ihs"] if body.bus == "both" else [body.bus]

    def job(ctx):
        adapter = ctx.uds()
        session_id = ctx.open_session("capture", body.model_dump())
        try:
            result = service.capture_snapshot(
                adapter, buses=buses, start=start, end=end, timeout_ms=timeout_ms,
                vin=body.vin, store=ctx.store, session_id=session_id,
                cancel=ctx.cancel, progress=lambda event: ctx.progress(**event),
            )
            return result
        finally:
            ctx.close_session(session_id, "ok", result.get("vin"))

    return await deps.run_job(worker, "uds-capture", job, timeout_s=900.0)


@router.get("/uds/module-map", summary="Known module addresses (offline, no adapter)")
async def module_map(request: Request, ecu_db: str | None = Query(default=None)):
    def build() -> dict:
        data: dict = {"bundled": {}, "external": None}
        for row in pacifica_data.modules():
            bus = row.get("bus") or "?"
            data["bundled"].setdefault(bus, []).append({
                "module": row.get("name"), "ecu": row.get("ecu"),
                "powertrain": row.get("powertrain"),
                "request": _id3(row.get("request")), "response": _id3(row.get("response")),
            })
        for rows in data["bundled"].values():
            rows.sort(key=lambda item: (item["request"], str(item["module"])))
        db = EcuDatabase(path=ecu_db)
        try:
            if db.available:
                data["external"] = {
                    "path": str(db.path),
                    "modules": [
                        {"ecu": row["ecu"], "name": row["name"], "bus": row["bus"],
                         "request": _id3(None if row["request"] is None else "%03X" % row["request"]),
                         "response": _id3(None if row["response"] is None else "%03X" % row["response"])}
                        for row in db.pn_modules()
                    ],
                }
        finally:
            db.close()
        return data

    return await asyncio.to_thread(build)


@router.get("/uds/modules/known", summary="Profile module addresses (offline)")
async def known_modules(request: Request, bus: str = Query(default="can-c")):
    if bus not in ("can-c", "can-ihs"):
        raise errors.validation(f"unknown bus: {bus!r}")
    return {
        "bus": bus,
        "modules": [
            {"module": name, "request_id": "%03X" % request_id, "response_id": "%03X" % response}
            for name, request_id, response in profile_addresses(bus)
        ],
    }


@router.get("/uds/dids", summary="Dataset DIDs for an ECU (offline)")
async def dids(request: Request, ecu: str = Query(...)):
    rows = pacifica_data.dids(ecu)
    return {"ecu": ecu, "dids": rows}


@router.get("/uds/ecus", summary="Dataset ECUs and their DID counts (offline)")
async def ecus(request: Request):
    raw = pacifica_data.dataset()
    name_by_ecu = {row.get("ecu"): row for row in pacifica_data.modules()}
    out = []
    for ecu, rows in (raw.get("dids") or {}).items():
        module = name_by_ecu.get(ecu) or {}
        out.append({
            "ecu": ecu,
            "name": module.get("name"),
            "bus": module.get("bus"),
            "request": _id3(module.get("request")) or None,
            "response": _id3(module.get("response")) or None,
            "powertrain": module.get("powertrain"),
            "dids": len(rows),
        })
    out.sort(key=lambda item: (item["bus"] or "", item["request"] or "", item["ecu"]))
    return {"ecus": out}


@router.get("/uds/adaptations", summary="Dataset adaptations for an ECU (offline)")
async def adaptations(request: Request, ecu: str = Query(...)):
    return {"ecu": ecu, "adaptations": pacifica_data.adaptations(ecu)}


@router.get("/dataset/info", summary="Bundled dataset summary (offline)")
async def dataset_info(request: Request):
    raw = pacifica_data.dataset()
    modules = raw.get("modules") or []
    return {
        "path": str(Path(__file__).resolve().parents[2] / "data" / "pacifica.json"),
        "modules": len(modules),
        "buses": sorted({row.get("bus") for row in modules if row.get("bus")}),
        "powertrains": sorted({row.get("powertrain") for row in modules if row.get("powertrain")}),
        "ecus_with_dids": len(raw.get("dids") or {}),
        "dids": sum(len(rows) for rows in (raw.get("dids") or {}).values()),
        "adaptations": sum(len(rows) for rows in (raw.get("adaptations") or {}).values()),
        "dtc_texts": len(raw.get("dtc_text") or {}),
        "seed_key": "seed_key" in raw,
    }
