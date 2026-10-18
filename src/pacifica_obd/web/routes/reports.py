"""Report routes (design §3.11).

A report is generated *locally* and written **only** under the tool's data dir
(never the repo, never an arbitrary path). Every read is from the registry by id,
so there is no user-controlled filesystem path anywhere in this module — the
path-traversal answer for reports is "there is no path parameter".
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from pacifica_obd import report as report_mod
from pacifica_obd.store import Store
from pacifica_obd.web import deps, errors
from pacifica_obd.web import reports as reports_mod
from pacifica_obd.web.schemas import CompareRequest, ReportDumpRequest, ReportFromSessionRequest

router = APIRouter()

ALLOWED_FORMATS = ("json", "xml", "html")


def _registry(request: Request) -> reports_mod.ReportRegistry:
    return request.app.state.reports


def _open_store() -> Store:
    return Store(readonly=True)


def _report(request: Request, report_id: str) -> reports_mod.StoredReport:
    found = _registry(request).get(report_id)
    if found is None:
        raise errors.not_found(f"no report {report_id}", code="report_not_found")
    return found


@router.post("/reports/dump", summary="Capture and write a dump (json/xml/html)")
async def dump(request: Request, body: ReportDumpRequest):
    worker = deps.get_worker(request)
    start = deps.parse_can_id(body.start)
    end = deps.parse_can_id(body.end)
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)
    formats = [fmt.lower() for fmt in body.formats if fmt]
    if not formats:
        raise errors.validation("at least one format is required")

    def job(ctx):
        session_id = ctx.open_session("dump", body.model_dump())
        try:
            result = reports_mod.build_web_dump(
                ctx, bus=body.bus, start=start, end=end, timeout_ms=timeout_ms,
                generic=body.generic, uds=body.uds, vin=body.vin,
            )
            if ctx.store is not None and result.get("vehicle", {}).get("vin"):
                vehicle = result["vehicle"]
                ctx.store.upsert_vehicle(
                    vehicle["vin"], make=vehicle.get("make"), model=vehicle.get("model"),
                    year=vehicle.get("year"), profile_id=vehicle.get("profile_id"),
                    powertrain=vehicle.get("powertrain"),
                )
            return result
        finally:
            ctx.close_session(session_id, "ok", None)

    result = await deps.run_job(worker, "reports-dump", job, timeout_s=900.0)
    stored = await asyncio.to_thread(_registry(request).add, result, formats, source="live")
    return stored.meta()


@router.get("/reports", summary="Reports generated this session")
async def list_reports(request: Request):
    return {"reports": _registry(request).list()}


@router.post("/reports/from-session", summary="Rebuild a dump from a stored session")
async def from_session(request: Request, body: ReportFromSessionRequest):
    formats = [fmt.lower() for fmt in body.formats if fmt] or ["json", "html"]
    store = await asyncio.to_thread(_open_store)
    try:
        dump = await asyncio.to_thread(reports_mod.dump_from_session, store, body.session_id)
    finally:
        await asyncio.to_thread(store.close)
    stored = await asyncio.to_thread(_registry(request).add, dump, formats, source="session")
    return stored.meta()


@router.post("/reports/compare", summary="Structural diff of two sessions")
async def compare(request: Request, body: CompareRequest):
    store = await asyncio.to_thread(_open_store)
    try:
        first = await asyncio.to_thread(reports_mod.dump_from_session, store, body.a)
        second = await asyncio.to_thread(reports_mod.dump_from_session, store, body.b)
    finally:
        await asyncio.to_thread(store.close)
    diff = reports_mod.compare_sessions(first, second)
    if first.get("session"):
        diff["a"]["session"] = first["session"]
    if second.get("session"):
        diff["b"]["session"] = second["session"]
    return diff


@router.get("/reports/{report_id}", summary="Report metadata")
async def report_meta(request: Request, report_id: str):
    return _report(request, report_id).meta()


@router.get("/reports/{report_id}/html", summary="Report HTML (review template, or raw)")
async def report_html(request: Request, report_id: str, raw: int = Query(default=0)):
    found = _report(request, report_id)
    if raw:
        body = report_mod.dump_to_html(found.dump)
    else:
        body = reports_mod.render_review_report(found.dump, report_id=found.id)
    return HTMLResponse(body)


@router.get("/reports/{report_id}/download/{fmt}", summary="Download one format")
async def download(request: Request, report_id: str, fmt: str):
    found = _report(request, report_id)
    fmt = fmt.lower()
    if fmt not in found.paths:
        if fmt not in ALLOWED_FORMATS:
            raise errors.validation(f"unknown format: {fmt!r}", code="bad_format")
        raise errors.not_found(f"report {report_id} has no {fmt} file", code="format_not_found")
    path = found.paths[fmt]
    media = {"json": "application/json", "xml": "application/xml", "html": "text/html"}[fmt]
    return FileResponse(path, media_type=media, filename=f"pacifica-report-{report_id}.{fmt}")


@router.get("/reports/{report_id}/zip", summary="Zip of every written format")
async def download_zip(request: Request, report_id: str):
    found = _report(request, report_id)
    data = await asyncio.to_thread(_registry(request).zip_bytes, found)
    return Response(data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="pacifica-report-{report_id}.zip"',
    })
