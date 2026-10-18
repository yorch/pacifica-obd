"""Audit events for guarded operations (design §4.5 point 7).

Every preview and every send is written to the store's ``events`` table (and the
JSONL log) with the module, the exact wire request and the outcome. Two rules:

* a security key is **never** written — only its byte length and a non-secret
  request prefix are recorded;
* a store failure degrades to a no-op, exactly like every other persistence path
  in this project (``Store`` must never break a command).
"""

from __future__ import annotations

__all__ = ["record", "KINDS"]


KINDS = (
    "write-preview",
    "write-send",
    "security-preview",
    "security-send",
    "security-seed",
    "security-refused",
    "raw-preview",
    "raw-send",
)


def record(store, session_id, kind: str, name: str | None, detail: dict) -> dict:
    """Append one audit event; return ``{stored, session_id}`` for the response."""
    if store is None:
        return {"stored": False, "session_id": session_id, "kind": kind}
    try:
        store.event(session_id, kind, name, detail)
        return {"stored": True, "session_id": session_id, "kind": kind}
    except Exception:  # noqa: BLE001 - auditing must never break a guarded action
        return {"stored": False, "session_id": session_id, "kind": kind}
