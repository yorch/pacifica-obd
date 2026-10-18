"""Local persistence for pacifica-obd.

Two complementary stores, both local to the user:

* a SQLite database (``pacifica.db``) holding structured data — vehicles,
  sessions, discovered modules, DTCs, and read data identifiers; and
* an append-only JSONL log (``events.jsonl``) recording every interaction.

Location (first match wins):

* ``$PACIFICA_OBD_DB`` / ``$PACIFICA_OBD_LOG`` for the individual file paths,
* ``$PACIFICA_OBD_HOME`` for the directory,
* ``$XDG_DATA_HOME/pacifica-obd`` (Linux convention),
* ``~/.local/share/pacifica-obd``.

Logging can be disabled with ``--no-log`` or ``PACIFICA_OBD_NO_LOG=1``.  All
failures degrade to a no-op (``available`` becomes ``False``) so persistence can
never crash a diagnostic command.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vin        TEXT PRIMARY KEY,
    make       TEXT,
    model      TEXT,
    year       INTEGER,
    profile_id TEXT,
    powertrain TEXT,
    nickname   TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    notes      TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    command    TEXT NOT NULL,
    args       TEXT,
    port       TEXT,
    vin        TEXT,
    status     TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    name       TEXT,
    detail     TEXT
);
CREATE TABLE IF NOT EXISTS modules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER, ts TEXT, vin TEXT,
    bus         TEXT, request_id TEXT, response_id TEXT, kind TEXT,
    vin_read    TEXT, part_number TEXT
);
CREATE TABLE IF NOT EXISTS dtcs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER, ts TEXT, vin TEXT,
    bus          TEXT, module TEXT, code TEXT, failure_type TEXT,
    status       TEXT, flags TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER, ts TEXT, vin TEXT,
    bus         TEXT, module TEXT, did TEXT, name TEXT,
    value_hex   TEXT, value_ascii TEXT, status TEXT, raw TEXT, category TEXT
);
CREATE TABLE IF NOT EXISTS samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER, ts TEXT, vin TEXT,
    bus        TEXT, module TEXT, did TEXT, name TEXT,
    value_num  REAL, value_text TEXT, unit TEXT, status TEXT, raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_dtcs_vin      ON dtcs(vin);
CREATE INDEX IF NOT EXISTS idx_readings_vin  ON readings(vin);
CREATE INDEX IF NOT EXISTS idx_modules_vin   ON modules(vin);
CREATE INDEX IF NOT EXISTS idx_samples_vin   ON samples(vin);
CREATE INDEX IF NOT EXISTS idx_samples_sess  ON samples(session_id, ts);
-- Opt-in AI tables (design §4.8). Deliberately outside `events`: that table is a
-- record of vehicle interactions, not of chat. Additive and behind the same
-- degrade-to-no-op behaviour as everything else here.
CREATE TABLE IF NOT EXISTS ai_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    provider      TEXT,
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_est      REAL
);
CREATE TABLE IF NOT EXISTS ai_conversations (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title      TEXT
);
CREATE TABLE IF NOT EXISTS ai_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    ts              TEXT NOT NULL,
    role            TEXT,
    content         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_messages ON ai_messages(conversation_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_dir() -> Path:
    override = os.environ.get("PACIFICA_OBD_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "pacifica-obd"
    return Path.home() / ".local" / "share" / "pacifica-obd"


def db_path() -> Path:
    override = os.environ.get("PACIFICA_OBD_DB")
    return Path(override).expanduser() if override else data_dir() / "pacifica.db"


def log_path() -> Path:
    override = os.environ.get("PACIFICA_OBD_LOG")
    return Path(override).expanduser() if override else data_dir() / "events.jsonl"


def logging_enabled(flag_disabled: bool = False) -> bool:
    if flag_disabled or os.environ.get("PACIFICA_OBD_NO_LOG") == "1":
        return False
    return True


class Store:
    """SQLite + JSONL persistence that degrades to a no-op on any failure."""

    def __init__(self, db_file: Path | str | None = None,
                 log_file: Path | str | None = None, readonly: bool = False):
        self.db_file = Path(db_file) if db_file else db_path()
        self.log_file = Path(log_file) if log_file else log_path()
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        if readonly:
            self._connect_readonly()
        else:
            self._connect_writable()

    # -- connection --------------------------------------------------------
    def _connect_writable(self) -> None:
        try:
            self.db_file.parent.mkdir(parents=True, exist_ok=True)
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_file, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            # lightweight migrations for databases created by older versions
            for statement in (
                "ALTER TABLE vehicles ADD COLUMN profile_id TEXT",
                "ALTER TABLE vehicles ADD COLUMN powertrain TEXT",
                "ALTER TABLE vehicles ADD COLUMN nickname TEXT",
                "ALTER TABLE samples ADD COLUMN unit TEXT",
            ):
                try:
                    conn.execute(statement)
                except sqlite3.Error:
                    pass
            conn.commit()
            self._conn = conn
        except (sqlite3.Error, OSError):
            self._conn = None

    def _connect_readonly(self) -> None:
        if not self.db_file.exists():
            self._conn = None
            return
        try:
            uri = self.db_file.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
            self._conn = conn
        except (sqlite3.Error, OSError):
            self._conn = None

    @property
    def available(self) -> bool:
        with self._lock:
            return self._conn is not None

    # -- low level ---------------------------------------------------------
    def _append_log(self, record: dict, mirror: bool = True) -> None:
        record.setdefault("ts", _now())
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass
        if not mirror:
            return
        # mirror every event into the SQLite events table
        self._execute(
            "INSERT INTO events (session_id, ts, kind, name, detail) VALUES (?, ?, ?, ?, ?)",
            (
                record.get("session_id"),
                record["ts"],
                record.get("kind"),
                record.get("name") or record.get("command") or record.get("code") or record.get("did"),
                json.dumps(record, default=str),
            ),
        )

    def _execute(self, sql: str, params: tuple = ()) -> int | None:
        with self._lock:
            if self._conn is None:
                return None
            try:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.Error:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                return None

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            if self._conn is None:
                return []
            try:
                return [dict(row) for row in self._conn.execute(sql, params).fetchall()]
            except sqlite3.Error:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                return []

    # -- sessions ----------------------------------------------------------
    def start_session(self, command: str, args: dict | None = None, port: str | None = None) -> int | None:
        session_id = self._execute(
            "INSERT INTO sessions (started_at, command, args, port) VALUES (?, ?, ?, ?)",
            (_now(), command, json.dumps(args or {}, default=str), port),
        )
        self._append_log(
            {"kind": "command", "session_id": session_id, "command": command,
             "args": args, "port": port}
        )
        return session_id

    def finish_session(self, session_id: int | None, status: str = "ok", vin: str | None = None) -> None:
        if session_id is not None:
            self._execute(
                "UPDATE sessions SET ended_at = ?, status = ?, vin = COALESCE(?, vin) WHERE id = ?",
                (_now(), status, vin, session_id),
            )
        self._append_log({"kind": "result", "session_id": session_id, "status": status, "vin": vin})

    def event(self, session_id: int | None, kind: str, name: str | None = None,
              detail: dict | None = None) -> None:
        self._append_log({"kind": kind, "session_id": session_id, "name": name, "detail": detail})

    def backfill_vin(self, session_id: int | None, vin: str | None) -> None:
        """Attach a late-discovered VIN to rows recorded earlier in the session."""
        if not session_id or not vin:
            return
        for table in ("modules", "dtcs", "readings", "samples"):
            self._execute(
                "UPDATE %s SET vin = ? WHERE session_id = ? AND vin IS NULL" % table,
                (vin, session_id),
            )
        self._append_log({"kind": "vin", "session_id": session_id, "vin": vin})

    # -- domain data -------------------------------------------------------
    def upsert_vehicle(self, vin: str, make: str | None = None, model: str | None = None,
                       year: int | None = None, profile_id: str | None = None,
                       powertrain: str | None = None, nickname: str | None = None) -> None:
        if not vin:
            return
        now = _now()
        self._execute(
            """INSERT INTO vehicles (vin, make, model, year, profile_id, powertrain, nickname, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(vin) DO UPDATE SET last_seen = excluded.last_seen,
                   make = COALESCE(excluded.make, vehicles.make),
                   model = COALESCE(excluded.model, vehicles.model),
                   year = COALESCE(excluded.year, vehicles.year),
                   profile_id = COALESCE(excluded.profile_id, vehicles.profile_id),
                   powertrain = COALESCE(excluded.powertrain, vehicles.powertrain),
                   nickname = COALESCE(excluded.nickname, vehicles.nickname)""",
            (vin, make, model, year, profile_id, powertrain, nickname, now, now),
        )
        self._append_log({"kind": "vehicle", "vin": vin, "make": make, "model": model,
                          "year": year, "profile_id": profile_id, "powertrain": powertrain})

    def add_module(self, session_id: int | None, vin: str | None, bus: str, module: dict) -> None:
        self._execute(
            """INSERT INTO modules (session_id, ts, vin, bus, request_id, response_id, kind, vin_read, part_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, _now(), vin, bus, module.get("request_id"), module.get("response_id"),
             module.get("kind"), module.get("vin"), module.get("part_number")),
        )
        self._append_log(
            {"kind": "module", "session_id": session_id, "bus": bus,
             "name": module.get("request_id"), "module": module}
        )

    def add_dtcs(self, session_id: int | None, vin: str | None, bus: str, module: str,
                 dtcs: list[dict], raw: str | None = None) -> None:
        for dtc in dtcs:
            flags = dtc.get("flags")
            self._execute(
                """INSERT INTO dtcs (session_id, ts, vin, bus, module, code, failure_type, status, flags, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, _now(), vin, bus, module, dtc.get("code"), dtc.get("failure_type"),
                 dtc.get("status"), json.dumps(flags) if flags is not None else None, raw),
            )
            self._append_log({"kind": "dtc", "session_id": session_id, "bus": bus,
                              "module": module, **dtc})

    def add_reading(self, session_id: int | None, vin: str | None, bus: str, module: str,
                    did: str, name: str | None, value_hex: str | None, value_ascii: str | None,
                    status: str, raw: str | None = None, category: str | None = None) -> None:
        self._execute(
            """INSERT INTO readings (session_id, ts, vin, bus, module, did, name, value_hex, value_ascii, status, raw, category)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, _now(), vin, bus, module, did, name, value_hex, value_ascii, status, raw, category),
        )
        self._append_log({"kind": "reading", "session_id": session_id, "bus": bus, "module": module,
                          "did": did, "name": name, "hex": value_hex, "ascii": value_ascii,
                          "status": status, "category": category})

    # -- queries -----------------------------------------------------------
    def vehicles(self) -> list[dict]:
        return self._query("SELECT * FROM vehicles ORDER BY last_seen DESC")

    def latest_vehicle(self) -> dict | None:
        rows = self._query("SELECT * FROM vehicles ORDER BY last_seen DESC LIMIT 1")
        return rows[0] if rows else None

    def add_sample(self, session_id: int | None, vin: str | None, bus: str, module: str,
                   did: str, name: str | None, value_num: float | None, value_text: str | None,
                   unit: str | None = None, status: str = "ok", raw: str | None = None,
                   ts: str | None = None) -> None:
        self._execute(
            """INSERT INTO samples (session_id, ts, vin, bus, module, did, name, value_num, value_text, unit, status, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, ts or _now(), vin, bus, module, did, name, value_num, value_text,
             unit, status, raw),
        )
        # high-rate samples go to the JSONL log only, not the events table
        self._append_log({"kind": "sample", "session_id": session_id, "module": module, "did": did,
                          "value": value_num, "text": value_text, "unit": unit, "status": status},
                         mirror=False)

    def sessions(self, limit: int = 50) -> list[dict]:
        return self._query("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))

    # -- AI usage + opt-in conversations (design §4.8) ---------------------
    def ai_record_usage(self, provider: str | None, model: str | None,
                        input_tokens: int, output_tokens: int,
                        cost_est: float) -> None:
        self._execute(
            "INSERT INTO ai_usage (ts, provider, model, input_tokens, output_tokens, cost_est)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), provider, model, int(input_tokens or 0), int(output_tokens or 0),
             float(cost_est or 0.0)),
        )

    def ai_save_conversation(self, conversation_id: str, messages: list[dict],
                             title: str | None = None) -> None:
        if not conversation_id:
            return
        now = _now()
        self._execute(
            """INSERT INTO ai_conversations (id, created_at, updated_at, title)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at,
                   title = COALESCE(excluded.title, ai_conversations.title)""",
            (conversation_id, now, now, title),
        )
        self._execute("DELETE FROM ai_messages WHERE conversation_id = ?", (conversation_id,))
        for message in messages or []:
            self._execute(
                "INSERT INTO ai_messages (conversation_id, ts, role, content) VALUES (?, ?, ?, ?)",
                (conversation_id, now, str(message.get("role") or ""),
                 str(message.get("content") or "")[:20000]),
            )

    def ai_list_conversations(self, limit: int = 50) -> list[dict]:
        return self._query(
            "SELECT id, created_at, updated_at, title FROM ai_conversations"
            " ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)),))

    def ai_get_conversation(self, conversation_id: str) -> dict | None:
        rows = self._query("SELECT * FROM ai_conversations WHERE id = ?", (conversation_id,))
        if not rows:
            return None
        messages = self._query(
            "SELECT role, content, ts FROM ai_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,))
        return {"conversation": rows[0], "messages": messages}

    def ai_delete_conversations(self) -> None:
        self._execute("DELETE FROM ai_messages")
        self._execute("DELETE FROM ai_conversations")


    def vehicle_summary(self, vin: str) -> dict:
        return {
            "vin": vin,
            "dtcs": self._query("SELECT * FROM dtcs WHERE vin = ? ORDER BY ts DESC", (vin,)),
            "modules": self._query("SELECT * FROM modules WHERE vin = ? ORDER BY ts DESC", (vin,)),
            "readings": self._query("SELECT * FROM readings WHERE vin = ? ORDER BY ts DESC", (vin,)),
            "samples": self._query("SELECT * FROM samples WHERE vin = ? ORDER BY ts DESC", (vin,)),
        }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
