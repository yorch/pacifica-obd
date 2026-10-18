"""Tool backends for the AI layer (design §5.7).

Two synchronous backends implement :class:`pacifica_obd.web.ai.tools.ToolBackend`:

* :class:`CacheToolBackend` reads the local database and the bundled dataset. It
  **never** touches the adapter, so it is safe to run on any thread.
* :class:`LiveToolBackend` runs *inside a bus-worker job* and is the only place a
  tool may read the vehicle, and only when the operator's settings allow it.

:class:`WebToolBackend` is the async facade the chat loop sees: it routes
``source: "cache"`` calls to a thread (cache) and ``source: "live"`` calls through
``BusWorker`` so two bus conversations can never interleave. No handler and no
tool ever holds the adapter directly.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pacifica_obd import pacifica_data
from pacifica_obd.profiles import detect_powertrain, year_from_vin
from pacifica_obd.store import Store
from pacifica_obd.web import errors, service
from pacifica_obd.web.ai import tools as tool_registry
from pacifica_obd.web.ai.redact import pseudonym

__all__ = ["CacheToolBackend", "LiveToolBackend", "WebToolBackend"]

_MAX_LIMIT = 500


def _open_store() -> Store | None:
    try:
        store = Store(readonly=True)
    except Exception:  # noqa: BLE001
        return None
    return store if store.available else None


class _BaseBackend:
    config = None

    def __init__(self, config=None):
        self.config = config

    # -- helpers -----------------------------------------------------------
    def _redact_vin(self, vin):
        if not vin:
            return vin
        if self.config is not None and getattr(self.config, "redact_vin", True):
            return f"<VIN:redacted {pseudonym(vin)}>"
        return vin

    def _table(self, table: str, args: dict) -> Any:
        from pacifica_obd.web.routes import db as db_routes

        if table not in db_routes.TABLES:
            raise ValueError(f"unknown table {table!r}")
        params = {str(k): v for k, v in (args.get("filters") or {}).items()}
        if args.get("limit") is not None:
            params["limit"] = args["limit"]
        if args.get("offset") is not None:
            params["offset"] = args["offset"]
        return db_routes._query_table(table, params)

    def _store_rows(self, sql, params=()) -> list[dict]:
        store = _open_store()
        if store is None:
            return []
        try:
            return store._query(sql, params)
        finally:
            store.close()

    def _sessions(self, args: dict) -> list[dict]:
        limit = max(1, min(int(args.get("limit") or 20), 200))
        if args.get("vin"):
            return self._store_rows(
                "SELECT * FROM sessions WHERE vin = ? ORDER BY id DESC LIMIT ?",
                (args["vin"], limit))
        return self._store_rows("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))

    def _samples(self, args: dict) -> list[dict]:
        limit = max(1, min(int(args.get("limit") or 200), 5000))
        clauses, values = [], []
        for column in ("vin", "module", "did"):
            if args.get(column):
                clauses.append(f"{column} = ?")
                values.append(args[column])
        if args.get("from"):
            clauses.append("ts >= ?")
            values.append(args["from"])
        if args.get("to"):
            clauses.append("ts <= ?")
            values.append(args["to"])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._store_rows(
            f"SELECT ts, vin, module, did, name, value_num, value_text, unit, status"
            f" FROM samples{where} ORDER BY ts DESC LIMIT ?", tuple(values) + (limit,))

    def _series(self, args: dict) -> dict:
        from pacifica_obd.web.routes import db as db_routes

        return db_routes._series(args)

    def _vehicle_context(self) -> dict:
        rows = self._store_rows("SELECT * FROM vehicles ORDER BY last_seen DESC LIMIT 5")
        vehicles = [{
            "vin": self._redact_vin(row.get("vin")),
            "make": row.get("make"), "model": row.get("model"), "year": row.get("year"),
            "powertrain": row.get("powertrain"), "last_seen": row.get("last_seen"),
        } for row in rows]
        modules = [row.get("request_id") for row in
                   self._store_rows("SELECT DISTINCT request_id FROM modules LIMIT 200")]
        return {
            "source": "stored sessions",
            "vehicles": vehicles,
            "powertrain_detected": detect_powertrain([m for m in modules if m]),
            "detection_basis": "HCP (7E2) / BPCM (7E7) answering means PHEV",
            "year_from_vin": year_from_vin((rows[0] or {}).get("vin")) if rows else None,
        }

    def _self_test(self, name: str) -> dict:
        return {"available": False, "reason": f"{name} needs a vehicle bus read",
                "hint": "enable 'allow the assistant to read from the bus' and use source=live"}

    # -- dispatch (router by tool name) -----------------------------------
    def call(self, name: str, args: dict) -> Any:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ValueError(f"no handler for tool {name}")
        return handler(args or {})


class CacheToolBackend(_BaseBackend):
    """Store/dataset reads only; never opens the adapter."""

    def __init__(self, config=None, *, worker=None):
        super().__init__(config)
        self.worker = worker

    # -- adapter status ----------------------------------------------------
    def _tool_get_adapter_status(self, args):
        if self.worker is None:
            return {"attached": False}
        backend = self.worker.backend
        info = {}
        getter = getattr(backend, "cached_info", None)
        if getter is not None:
            try:
                info = getter()
            except Exception:  # noqa: BLE001
                info = {}
        return {"attached": bool(getattr(backend, "attached", False)), "bus": self.worker.bus,
                "port": info.get("port"), "status": info.get("status"),
                "protocol": info.get("protocol"), "voltage": info.get("voltage")}

    def _tool_read_vin(self, args):
        rows = self._store_rows(
            "SELECT vin FROM vehicles WHERE vin IS NOT NULL ORDER BY last_seen DESC LIMIT 1")
        vin = rows[0].get("vin") if rows else None
        return {"vin": self._redact_vin(vin) if vin else None,
                "source": "stored", "redacted": bool(self.config and self.config.redact_vin)}

    def _tool_read_generic_dtcs(self, args):
        rows = self._store_rows(
            "SELECT code, status, flags, module, bus, ts FROM dtcs ORDER BY id DESC LIMIT 100")
        return {"dtcs": rows, "source": "stored", "total": len(rows)}

    def _tool_read_readiness(self, args):
        return self._self_test("readiness")

    def _tool_read_freeze_frame(self, args):
        return self._self_test("freeze frame")

    def _tool_read_live_pids(self, args):
        return self._self_test("live PID read")

    def _tool_discover_modules(self, args):
        rows = self._store_rows(
            "SELECT DISTINCT request_id, response_id, bus, kind FROM modules LIMIT 200")
        return {"modules": rows, "source": "stored", "count": len(rows),
                "note": "stored discovery; a live scan needs source=live"}

    def _tool_read_module_dtcs(self, args):
        rows = self._store_rows(
            "SELECT module, bus, code, status, flags, ts FROM dtcs ORDER BY id DESC LIMIT 200")
        return {"dtcs": rows, "source": "stored"}

    def _tool_read_did(self, args):
        limit = 100
        rows = self._store_rows(
            "SELECT module, bus, did, name, value_hex, value_ascii, status, ts FROM readings"
            " WHERE category = 'did' ORDER BY id DESC LIMIT ?", (limit,))
        return {"readings": rows, "source": "stored"}

    def _tool_get_module_identification(self, args):
        rows = self._store_rows(
            "SELECT module, bus, did, name, value_ascii, value_hex, status FROM readings"
            " WHERE category = 'module-info' ORDER BY id DESC LIMIT 100")
        return {"identification": rows, "source": "stored"}

    def _tool_get_module_map(self, args):
        data = {"bundled": {}}
        for row in pacifica_data.modules():
            data["bundled"].setdefault(row.get("bus") or "?", []).append({
                "ecu": row.get("ecu"), "name": row.get("name"),
                "request": row.get("request"), "response": row.get("response"),
            })
        return data

    def _tool_get_vehicle_context(self, args):
        return self._vehicle_context()

    def _tool_list_dataset_modules(self, args):
        return pacifica_data.modules()[:200]

    def _tool_get_dataset_dids(self, args):
        return pacifica_data.dids(args.get("ecu") or "")[:200]

    def _tool_explain_dtc_code(self, args):
        code = str(args.get("code") or "").upper()
        text = pacifica_data.dtc_text(code[1:]) if len(code) >= 2 else None
        return {"code": code, "description": text}

    def _tool_list_sessions(self, args):
        return {"sessions": self._sessions(args)}

    def _tool_get_session(self, args):
        from pacifica_obd.web.routes import db as db_routes

        session_id = int(args["session_id"])
        return db_routes._session_detail(session_id)

    def _tool_search_db(self, args):
        return self._table(str(args["table"]), args)

    def _tool_get_samples(self, args):
        return {"samples": self._samples(args)}

    def _tool_get_sample_series(self, args):
        return self._series(args)

    def _tool_compare_sessions(self, args):
        from pacifica_obd.web import reports as reports_mod

        store = _open_store()
        if store is None:
            return {"available": False, "reason": "no local database found"}
        try:
            first = reports_mod.dump_from_session(store, int(args["a"]))
            second = reports_mod.dump_from_session(store, int(args["b"]))
        finally:
            store.close()
        return reports_mod.compare_sessions(first, second)

    def _tool_build_report(self, args):
        return {"written": False, "report": None,
                "reason": "report building is disabled for the assistant in this build"}


class LiveToolBackend(_BaseBackend):
    """Bus reads, constructed inside a worker job with the adapter in hand."""

    def __init__(self, ctx, config=None):
        super().__init__(config)
        self.ctx = ctx
        self.cache = CacheToolBackend(config, worker=ctx.worker)

    @property
    def adapter(self):
        return self.ctx.uds()

    @property
    def conn(self):
        return self.ctx.obd()

    def _tool_get_adapter_status(self, args):
        try:
            info = service.obd_info(self.conn)
        except Exception:  # noqa: BLE001
            info = {}
        return {"attached": True, "protocol": info.get("protocol"),
                "voltage": info.get("voltage"), "adapter": info.get("adapter"),
                "bus": self.ctx.worker.bus}

    def _tool_read_vin(self, args):
        vin = None
        try:
            vin = service.obd_vin(self.conn)
        except Exception:  # noqa: BLE001
            vin = None
        if not vin:
            try:
                vin = service.uds_vin(self.adapter, timeout_ms=100, cancel=self.ctx.cancel)
            except Exception:  # noqa: BLE001
                vin = None
        return {"vin": self._redact_vin(vin) if vin else None}

    def _tool_read_generic_dtcs(self, args):
        return service.obd_dtcs(self.conn)

    def _tool_read_readiness(self, args):
        return service.obd_readiness(self.conn)

    def _tool_read_freeze_frame(self, args):
        return service.obd_freeze(self.conn)

    def _tool_read_live_pids(self, args):
        names = [str(name) for name in args.get("pids") or []]
        return {"pids": service.obd_live(self.conn, names)}

    def _tool_discover_modules(self, args):
        start = int(str(args.get("start") or "700"), 16)
        end = int(str(args.get("end") or "7FF"), 16)
        return service.discover_modules(
            self.adapter, bus=args.get("bus") or "can-c", start=start, end=end,
            identify=bool(args.get("identify")), timeout_ms=100, cancel=self.ctx.cancel)

    def _tool_read_module_dtcs(self, args):
        start = int(str(args.get("start") or "700"), 16)
        end = int(str(args.get("end") or "7FF"), 16)
        return service.read_module_dtcs(
            self.adapter, bus=args.get("bus") or "can-c", start=start, end=end,
            module=args.get("module"), timeout_ms=100, cancel=self.ctx.cancel)

    def _tool_read_did(self, args):
        dids = [service.parse_did(did) for did in args.get("dids") or []]
        return service.read_dids(self.adapter, bus=args.get("bus") or "can-c",
                                 module=args["module"], dids=dids, timeout_ms=100,
                                 cancel=self.ctx.cancel)

    def _tool_get_module_identification(self, args):
        return service.module_info(self.adapter, bus=args.get("bus") or "can-c",
                                   module=args.get("module"), timeout_ms=100,
                                   cancel=self.ctx.cancel)

    def _tool_build_report(self, args):
        return self.cache._tool_build_report(args)


class WebToolBackend:
    """Async facade: cache reads on a thread, live reads on the bus worker."""

    def __init__(self, worker, config, *, runner):
        self.worker = worker
        self.config = config
        self._runner = runner  # async (name, fn) -> result

    async def dispatch(self, name: str, args: dict) -> dict:
        spec = tool_registry.TOOLS.get(name)
        if spec is None:
            return {"error": f"unknown tool: {name}", "source": "registry"}

        def cache_call():
            return tool_registry.dispatch(name, args, CacheToolBackend(self.config,
                                                                       worker=self.worker))

        try:
            if spec.source != "vehicle-bus" or args.get("source") != "live":
                return await asyncio.to_thread(cache_call)

            if not self.config.allow_live_bus:
                return {"error": "live bus reads are not enabled in settings",
                        "source": "registry", "untrusted": True}

            def live_call(ctx):
                return tool_registry.dispatch(name, args, LiveToolBackend(ctx, self.config))

            return await self._runner(f"ai-{name}", live_call)
        except Exception as exc:  # noqa: BLE001 - a failed tool must not kill the turn
            from pacifica_obd.web.ai.keys import redact_text

            return {"error": redact_text(str(exc))[:300], "source": "registry",
                    "untrusted": True}
