"""Single-use confirm tokens for guarded writes (design §4.5).

A browser write is two-phase:

1. ``POST .../write-did`` (preview) validates the parameters, does the read half
   on the bus worker and returns a 256-bit token plus the exact wire request.
2. ``POST .../write-did/confirm`` accepts **only** the token and the typed
   acknowledgement. The payload lives server-side in this store, so a tampered
   or replayed request cannot change what was previewed.

Properties enforced here:

* bound to ``(kind, module, bus, exact service bytes, params)``;
* **single use** — consumed before the job runs, so a double submit cannot write
  twice;
* expires after 120 s by default;
* never persisted to SQLite or the JSONL log (it is an ephemeral capability);
* the typed acknowledgement must name the module (``7E0``/``744``).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

__all__ = ["ConfirmError", "ConfirmEntry", "ConfirmTokenStore", "DEFAULT_TTL_S"]

DEFAULT_TTL_S = 120.0


class ConfirmError(RuntimeError):
    """A token was unknown, expired, reused, mismatched or unacknowledged."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ConfirmEntry:
    token: str
    kind: str
    module: str
    bus: str
    request: str
    params: dict = field(default_factory=dict)
    created: float = 0.0
    ttl_s: float = DEFAULT_TTL_S
    used: bool = False
    #: Set by :meth:`ConfirmTokenStore.authorize`, at the moment of transmit.
    authorized: bool = False
    #: When set, the typed acknowledgement must equal this string instead of the
    #: normalised module id (the raw console acknowledges the exact command bytes).
    ack: str | None = None

    def expired(self, now: float) -> bool:
        return now - self.created >= self.ttl_s


def _normalise_module(value) -> str:
    text = str(value).strip().upper()
    text = text[2:] if text.startswith("0X") else text
    try:
        number = int(text, 16)
    except ValueError as exc:
        raise ConfirmError("mismatch", f"not a CAN id: {value!r}") from exc
    if not 0 <= number <= 0x7FF:
        raise ConfirmError("mismatch", f"CAN id out of range: {value!r}")
    return "%03X" % number


def _normalise_ack(value) -> str:
    """Canonicalise a typed acknowledgement: uppercase, whitespace removed.

    Control characters are refused: a raw command is a single line, so an
    acknowledgement containing ``\\r``/``\\n``/``\\x1b`` is never a real one and is
    rejected rather than silently collapsed.
    """
    text = str(value)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise ConfirmError("acknowledgement",
                           "the acknowledgement cannot contain control characters")
    return "".join(text.split()).upper()


class ConfirmTokenStore:
    """In-process, thread-safe store of previewed-but-not-yet-sent operations."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S, *, clock=time.monotonic):
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, ConfirmEntry] = {}

    def __len__(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._entries)

    def issue(self, *, kind: str, module, bus: str, request: str,
              params: dict | None = None, ttl_s: float | None = None,
              ack: str | None = None) -> ConfirmEntry:
        if ack is None:
            ack_value, module_id = None, _normalise_module(module)
        else:
            ack_value, module_id = _normalise_ack(ack), str(module)
        entry = ConfirmEntry(
            token=secrets.token_urlsafe(32),
            kind=str(kind),
            module=module_id,
            bus=str(bus),
            request=str(request).upper(),
            params=dict(params or {}),
            created=self._clock(),
            ttl_s=float(self.ttl_s if ttl_s is None else ttl_s),
            ack=ack_value,
        )
        with self._lock:
            self._purge_locked()
            self._entries[entry.token] = entry
        return entry

    def peek(self, token: str) -> ConfirmEntry:
        """Look up a live, unconsumed entry (for the typed-ack check)."""
        with self._lock:
            self._purge_locked()
            entry = self._entries.get(token or "")
            if entry is None:
                raise ConfirmError("expired", "confirm token is unknown or expired")
            if entry.used:
                raise ConfirmError("consumed", "confirm token was already used")
            return entry

    def consume(self, token: str, *, kind: str, acknowledgement: str) -> ConfirmEntry:
        """Burn the token and return the exact previewed operation.

        The acknowledgement must equal the module id the token names. A wrong
        acknowledgement does **not** consume the token (a typo should not force a
        fresh preview), but it never reaches the bus.
        """
        with self._lock:
            self._purge_locked()
            entry = self._entries.get(token or "")
            if entry is None:
                raise ConfirmError("expired", "confirm token is unknown or expired")
            if entry.used:
                raise ConfirmError("consumed", "confirm token was already used")
            if entry.kind != kind:
                raise ConfirmError(
                    "mismatch",
                    f"token belongs to a {entry.kind!r} preview, not {kind!r}",
                )
            if entry.ack is not None:
                matches = _normalise_ack(acknowledgement) == entry.ack
            else:
                try:
                    matches = _normalise_module(acknowledgement) == entry.module
                except ConfirmError:
                    matches = False
            if not matches:
                expected = entry.ack if entry.ack is not None else entry.module
                raise ConfirmError(
                    "acknowledgement",
                    f"type {expected} to confirm",
                )
            burned = ConfirmEntry(**{**entry.__dict__, "used": True})
            self._entries[token] = burned
            return entry

    def authorize(self, token: str, *, kind: str, generation: str = "") -> ConfirmEntry:
        """The **final** gate: re-validate immediately before the actual transmit.

        ``consume`` burns the token at the start of the job, but the job then
        acquires the adapter, opens a session and reads the precondition — seconds
        during which the token can expire and the adapter generation can change.
        Without this the earlier positive decision would still authorise the write
        (adversarial review, highest-risk finding). A successful authorisation is
        itself single-use, so one token cannot authorise two transmits.
        """
        with self._lock:
            self._purge_locked()  # drops anything past its TTL
            entry = self._entries.get(token or "")
            if entry is None:
                raise ConfirmError("expired", "confirm token expired before the send")
            if entry.kind != kind:
                raise ConfirmError(
                    "mismatch",
                    f"token belongs to a {entry.kind!r} preview, not {kind!r}",
                )
            if not entry.used:
                raise ConfirmError("unconfirmed", "this operation was never confirmed")
            if entry.authorized:
                raise ConfirmError("consumed", "this token already authorised a send")
            if str(generation or "") != str(entry.params.get("generation", "")):
                raise ConfirmError(
                    "generation_mismatch",
                    "the adapter connection changed before the operation was sent",
                )
            if entry.expired(self._clock()):
                raise ConfirmError("expired", "confirm token expired before the send")
            authorized = ConfirmEntry(**{**entry.__dict__, "authorized": True})
            self._entries[token] = authorized
            return entry

    def _purge_locked(self) -> None:
        now = self._clock()
        for token, entry in list(self._entries.items()):
            if entry.expired(now):
                self._entries.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
