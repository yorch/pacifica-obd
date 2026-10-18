"""Database browser routes (design §3.10, §4.3 "Persistence / DB browser").

Strictly read-only: every connection is opened with ``Store(readonly=True)``
(SQLite ``mode=ro``), table names come from a fixed allowlist, order columns are
whitelisted, and every user value is a bound parameter — so there is no path
where a crafted ``table``/``order``/``vin`` can escape into SQL.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse

from pacifica_obd.store import Store, db_path
from pacifica_obd.web import errors
from pacifica_obd.web.downsample import downsample

router = APIRouter()

# table -> (filterable columns, free-text columns, default order, timestamp column)
TABLES: dict[str, dict] = {
    "vehicles": {
        "columns": ["vin", "make", "model", "year", "profile_id", "powertrain", "nickname"],
        "search": ["vin", "make", "model", "nickname"], "order": "last_seen", "ts": "last_seen",
    },
    "sessions": {
        "columns": ["vin", "command", "status", "port"],
        "search": ["command", "status"], "order": "id", "ts": "started_at",
    },
    "events": {
        "columns": ["session_id", "kind", "name"],
        "search": ["kind", "name", "detail"], "order": "id", "ts": "ts",
    },
    "modules": {
        "columns": ["vin", "bus", "request_id", "response_id", "kind"],
        "search": ["request_id", "response_id"], "order": "id", "ts": "ts",
    },
    "dtcs": {
        "columns": ["vin", "bus", "module", "code", "failure_type", "status"],
        "search": ["code", "flags"], "order": "id", "ts": "ts",
    },
    "readings": {
        "columns": ["vin", "bus", "module", "did", "name", "category", "status"],
        "search": ["did", "name", "value_ascii"], "order": "id", "ts": "ts",
    },
    "samples": {
        "columns": ["vin", "bus", "module", "did", "name", "status", "session_id"],
        "search": ["did", "name", "value_text"], "order": "id", "ts": "ts",
    },
}

MAX_LIMIT = 1000
DEFAULT_LIMIT = 50


def _table(name: str) -> dict:
    meta = TABLES.get(name)
    if meta is None:
        raise errors.not_found(f"unknown table {name!r}", code="unknown_table")
    return meta


def _open() -> Store:
    return Store(readonly=True)


def _where(meta: dict, params: dict) -> tuple[str, list]:
    clauses: list[str] = []
    values: list = []
    for column in meta["columns"]:
        value = params.get(column)
        if value not in (None, ""):
            clauses.append(f"{column} = ?")
            values.append(value)
    ts = meta["ts"]
    if params.get("from"):
        clauses.append(f"{ts} >= ?")
        values.append(params["from"])
    if params.get("to"):
        clauses.append(f"{ts} <= ?")
        values.append(params["to"])
    query = params.get("q")
    if query:
        like = f"%{query}%"
        ors = " OR ".join(f"{column} LIKE ?" for column in meta["search"])
        if ors:
            clauses.append(f"({ors})")
            values.extend([like] * len(meta["search"]))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", values


def _order(meta: dict, params: dict) -> str:
    column = params.get("order") or meta["order"]
    allowed = set(meta["columns"]) | {meta["order"], meta["ts"], "id", "ts", "started_at"}
    if column not in allowed:
        column = meta["order"]
    direction = "ASC" if str(params.get("dir", "desc")).lower() == "asc" else "DESC"
    return f" ORDER BY {column} {direction}"


def _limit(params: dict) -> tuple[int, int]:
    try:
        limit = int(params.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(params.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


def _params(request: Request) -> dict:
    return {key: value for key, value in request.query_params.items()}


def _query_table(name: str, params: dict) -> dict:
    meta = _table(name)
    store = _open()
    try:
        if not store.available:
            return {"available": False, "table": name, "rows": [], "total": 0,
                    "db_path": str(db_path()), "columns": []}
        where, values = _where(meta, params)
        order = _order(meta, params)
        limit, offset = _limit(params)
        total_rows = store._query(f"SELECT COUNT(*) AS n FROM {name}{where}", tuple(values))
        total = total_rows[0]["n"] if total_rows else 0
        rows = store._query(
            f"SELECT * FROM {name}{where}{order} LIMIT ? OFFSET ?",
            tuple(values) + (limit, offset),
        )
        columns = list(rows[0].keys()) if rows else _columns(store, name)
        return {"available": True, "table": name, "rows": rows, "total": total,
                "columns": columns, "limit": limit, "offset": offset,
                "db_path": str(db_path())}
    finally:
        store.close()


def _columns(store: Store, name: str) -> list[str]:
    rows = store._query(f"PRAGMA table_info({name})")
    return [row["name"] for row in rows]


def _schema() -> dict:
    store = _open()
    try:
        if not store.available:
            return {"available": False, "db_path": str(db_path()), "tables": []}
        tables = []
        for name, meta in TABLES.items():
            count_rows = store._query(f"SELECT COUNT(*) AS n FROM {name}")
            tables.append({
                "name": name,
                "columns": _columns(store, name),
                "filterable": meta["columns"],
                "order": meta["order"],
                "rows": count_rows[0]["n"] if count_rows else 0,
            })
        return {"available": True, "db_path": str(db_path()), "tables": tables}
    finally:
        store.close()


@router.get("/db/schema", summary="Tables, columns and row counts")
async def schema(request: Request):
    return await asyncio.to_thread(_schema)


@router.get("/db/csv", summary="CSV export of a filtered table")
async def csv_export(request: Request, table: str = Query(...)):
    data = await asyncio.to_thread(_query_table, table, {**_params(request), "limit": MAX_LIMIT})
    if not data["available"]:
        raise errors.ApiError("db_unavailable", "adapter", "no local database found",
                              http_status=503, detail={"path": data.get("db_path")})
    buffer = io.StringIO()
    columns = data["columns"] or (list(data["rows"][0].keys()) if data["rows"] else [])
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in data["rows"]:
        writer.writerow(["" if row.get(column) is None else row.get(column) for column in columns])
    return PlainTextResponse(buffer.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="pacifica-{table}.csv"',
    })


def _series(params: dict) -> dict:
    store = _open()
    try:
        if not store.available:
            return {"available": False, "series": [], "db_path": str(db_path())}
        limit = 200000
        clauses: list[str] = []
        values: list = []
        for column in ("vin", "module", "did", "name", "bus", "session_id", "status"):
            if params.get(column):
                clauses.append(f"{column} = ?")
                values.append(params[column])
        if params.get("from"):
            clauses.append("ts >= ?")
            values.append(params["from"])
        if params.get("to"):
            clauses.append("ts <= ?")
            values.append(params["to"])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = store._query(
            f"SELECT module, did, name, unit, ts, value_num, value_text, status "
            f"FROM samples{where} ORDER BY ts ASC LIMIT ?",
            tuple(values) + (limit,),
        )
        try:
            max_points = max(10, min(5000, int(params.get("max_points") or 2000)))
        except (TypeError, ValueError):
            max_points = 2000
        groups: dict[tuple, dict] = {}
        for row in rows:
            key = (row.get("module"), row.get("did"), row.get("name"))
            group = groups.setdefault(key, {
                "id": "%s:%s" % (row.get("module") or "?", row.get("did") or "?"),
                "module": row.get("module"), "did": row.get("did"), "name": row.get("name"),
                "unit": row.get("unit"), "points": [], "raw_count": 0, "statuses": {},
            })
            if row.get("value_num") is None and row.get("value_text") in (None, ""):
                continue
            timestamp = _epoch_ms(row.get("ts"))
            value = row.get("value_num")
            if value is None:
                try:
                    value = float(row.get("value_text"))
                except (TypeError, ValueError):
                    continue
            group["points"].append((timestamp, float(value)))
            group["raw_count"] += 1
            status = row.get("status") or "ok"
            group["statuses"][status] = group["statuses"].get(status, 0) + 1
        series = []
        for group in groups.values():
            sampled, method = downsample(group["points"], max_points)
            series.append({
                "id": group["id"], "module": group["module"], "did": group["did"],
                "name": group["name"], "unit": group["unit"],
                "points": [[t, v] for t, v in sampled],
                "raw_count": group["raw_count"], "downsampled": method,
                "statuses": group["statuses"],
            })
        series.sort(key=lambda item: (item["module"] or "", item["did"] or ""))
        return {"available": True, "series": series, "max_points": max_points,
                "db_path": str(db_path()), "rows": len(rows)}
    finally:
        store.close()


def _epoch_ms(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        text = ts.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() * 1000.0
    except (ValueError, TypeError):
        return 0.0


def _session_detail(session_id: int, sample_limit: int = 200) -> dict:
    store = _open()
    try:
        if not store.available:
            raise errors.ApiError("db_unavailable", "adapter", "no local database found",
                                  http_status=503)
        rows = store._query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if not rows:
            raise errors.not_found(f"no session {session_id}", code="session_not_found")
        session = rows[0]
        children = {}
        for name in ("events", "modules", "dtcs", "readings"):
            children[name] = store._query(
                f"SELECT * FROM {name} WHERE session_id = ? ORDER BY id LIMIT 500", (session_id,)
            )
        children["samples"] = store._query(
            "SELECT * FROM samples WHERE session_id = ? ORDER BY id LIMIT ?",
            (session_id, sample_limit),
        )
        return {"session": session, "children": children}
    finally:
        store.close()


def _vehicle_summary(vin: str) -> dict:
    store = _open()
    try:
        if not store.available:
            raise errors.ApiError("db_unavailable", "adapter", "no local database found",
                                  http_status=503)
        vehicle_rows = store._query("SELECT * FROM vehicles WHERE vin = ?", (vin,))
        summary = {"vin": vin, "vehicle": vehicle_rows[0] if vehicle_rows else None,
                   "counts": {}, "dtcs": [], "modules": [], "readings": [], "samples": []}
        for name in ("dtcs", "modules", "readings", "samples"):
            summary[name] = store._query(
                f"SELECT * FROM {name} WHERE vin = ? ORDER BY ts DESC LIMIT 100", (vin,)
            )
            counts = store._query(f"SELECT COUNT(*) AS n FROM {name} WHERE vin = ?", (vin,))
            summary["counts"][name] = counts[0]["n"] if counts else 0
        return summary
    finally:
        store.close()


@router.get("/db/sessions/{session_id}", summary="One session with its children")
async def session_detail(request: Request, session_id: int):
    data = await asyncio.to_thread(_session_detail, session_id)
    data["rows"] = data.get("children", {})
    return data


@router.get("/db/vehicles/{vin}/summary", summary="Everything stored for a VIN (parity with `show`)")
async def vehicle_summary(request: Request, vin: str):
    return await asyncio.to_thread(_vehicle_summary, vin)


@router.get("/db/samples/series", summary="Chart-ready, downsampled sample series")
async def samples_series(request: Request):
    return await asyncio.to_thread(_series, _params(request))


@router.get("/db/{table}", summary="A filtered, paginated table view")
async def table_view(request: Request, table: str):
    return await asyncio.to_thread(_query_table, table, _params(request))
