"""Optional read-only access to an external ECU database.

An external ECU database can carry FCA/Stellantis module addresses, DIDs,
adaptations and more. It is entirely optional: the user points at one (there is
no built-in default), we open it strictly read-only, and degrade to a no-op when
it is missing, so nothing here is ever required for the core tool to work.

Override the path with ``PACIFICA_OBD_ECU_DB``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# inittype -> bus is inferred from the database's ECU table: 7 ~= CAN-C, 9 ~= CAN-IHS.
_INITTYPE_BUS = {7: "can-c", 9: "can-ihs"}


def default_db() -> Path | None:
    """Path from ``PACIFICA_OBD_ECU_DB``, or None when unset (the feature is opt-in)."""
    override = os.environ.get("PACIFICA_OBD_ECU_DB")
    if override:
        return Path(override).expanduser()
    return None


def _hex_to_int(text) -> int | None:
    if text is None or text == "":
        return None
    try:
        return int(str(text).strip().lower().removeprefix("0x"), 16)
    except (ValueError, TypeError):
        return None


class EcuDatabase:
    """Read-only view of an external ECU database (no-op if unavailable)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_db()
        self._conn: sqlite3.Connection | None = None
        if self.path is None:
            return
        try:
            if not self.path.exists():
                return
            uri = self.path.resolve().as_uri() + "?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")  # block all writes, incl. ATTACH/TEMP
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()  # validate
            self._conn = conn
        except (sqlite3.Error, OSError, RuntimeError, ValueError):
            self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        if self._conn is None:
            return []
        try:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    def tables(self) -> list[str]:
        rows = self._query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [row["name"] for row in rows]

    def modules(self) -> list[dict]:
        """Every addressed ECU: name, family, bus, request/response CAN IDs."""
        rows = self._query(
            "SELECT unit_name, ECUNAME, inittype, canid, canidresp, adapter "
            "FROM ECUUnits WHERE canid IS NOT NULL AND canid <> ''"
        )
        modules = []
        for row in rows:
            modules.append(
                {
                    "name": row.get("unit_name"),
                    "ecu": row.get("ECUNAME"),
                    "inittype": row.get("inittype"),
                    "bus": _INITTYPE_BUS.get(row.get("inittype")),
                    "request": _hex_to_int(row.get("canid")),
                    "response": _hex_to_int(row.get("canidresp")),
                    "adapter": row.get("adapter"),
                }
            )
        return modules

    def pn_modules(self) -> list[dict]:
        """PowerNet (`*_PN`) modules — the ones FORScan-style tools select."""
        return [m for m in self.modules() if (m.get("ecu") or "").endswith("_PN")]

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Escape hatch for other tables (read-only). Callers own the SQL."""
        return self._query(sql, params)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
