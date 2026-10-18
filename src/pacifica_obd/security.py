"""Shared UDS ``0x27`` SecurityAccess policy for every front end.

Owner decision (``docs/web-ui.md`` §9.2): **request one seed, hold it under a
short lease bound to (module, level), and send the key against that held seed.**
Do **not** request a second seed by default: on some ECUs a second ``27 <level>``
advances an internal bad-attempt counter, which is exactly the shortcut that can
lock a module. The TUI-style "re-read and compare" is available as an explicit
opt-in (``strict_seed_reread``) for modules whose seed is known to be stable.

This module is deliberately free of I/O: the CLI, the TUI and the web service can
all use it. The web layer owns the token that names a lease; the lease itself
never contains the key.

Vocabulary (matching the rest of the app, never a parallel one):

``ok``                 the lease is valid and the key may be sent
``expired``            the 30 s TTL elapsed (or the token is unknown)
``consumed``           this lease already authorised one send
``mismatch``           a different module / level / bus than the lease names
``generation_mismatch`` the adapter connection changed since preview
``seed_changed``       strict mode re-read a different seed; nothing may be sent
``strict_unavailable`` strict mode asked for a re-read but supplied no reader
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, replace

__all__ = [
    "DEFAULT_TTL_S",
    "KEY_SERVICE_OFFSET",
    "SecurityLease",
    "SendKeyDecision",
    "SecurityLeaseStore",
    "SendRefused",
    "normalize_module",
    "normalize_seed",
    "key_request",
]

#: Owner decision §9.2: 30 seconds, long enough to read a modal, short enough
#: that a stale tab cannot authorise a key minutes later.
DEFAULT_TTL_S = 30.0

#: ``27 <level>`` asks for a seed; the key goes to ``level + 1``.
KEY_SERVICE_OFFSET = 1


def normalize_module(module) -> str:
    """``0x7e0`` / ``7e0`` / ``2016`` -> ``"7E0"`` (11-bit request id)."""
    if isinstance(module, int):
        value = module
    else:
        text = str(module).strip().upper()
        value = int(text[2:] if text.startswith("0X") else text, 16)
    if not 0 <= value <= 0x7FF:
        raise ValueError(f"CAN ID out of 11-bit range: {module!r}")
    return "%03X" % value


def normalize_seed(seed) -> str:
    """A seed as uppercase even-length hex (``0xA1B2`` / ``b'\xa1\xb2'`` / ``"A1B2"``)."""
    if isinstance(seed, (bytes, bytearray)):
        return bytes(seed).hex().upper()
    if isinstance(seed, int):
        if seed < 0:
            raise ValueError("a seed cannot be negative")
        width = max(2, (seed.bit_length() + 7) // 8 * 2)
        return ("%0*X" % (width, seed))
    text = str(seed).strip().upper().removeprefix("0X")
    if not text or len(text) % 2 or any(char not in "0123456789ABCDEF" for char in text):
        raise ValueError(f"not an even-length hex seed: {seed!r}")
    return text


def key_request(level: int, key_hex: str) -> str:
    """The exact wire request that sends a key for ``level`` (``27 <lvl+1> <key>``)."""
    level = int(level)
    key_hex = normalize_seed(key_hex)
    return "27%02X%s" % (level + KEY_SERVICE_OFFSET, key_hex)


@dataclass(frozen=True)
class SecurityLease:
    """A held seed, bound to one module/level/bus and one adapter generation."""

    token: str
    module: str
    level: int
    bus: str | None
    seed: str
    generation: str
    created: float
    ttl_s: float
    used: bool = False
    #: The challenge epoch for (module, level, bus) when this lease was issued.
    #: A newer seed request bumps it and supersedes this lease.
    epoch: int = 0
    #: The exact key previewed for this seed (in memory only; never logged).
    key_hex: str | None = None
    algorithm: str | None = None
    params: dict | None = None
    #: Set by :meth:`SecurityLeaseStore.authorize`, at the moment of transmit.
    authorized: bool = False

    def expired(self, now: float) -> bool:
        return now - self.created >= self.ttl_s


class SendRefused(RuntimeError):
    """The final pre-transmit authorisation refused. Nothing was sent."""


@dataclass(frozen=True)
class SendKeyDecision:
    """The one place that decides whether a reviewed key may go on the wire."""

    send: bool
    status: str
    reason: str
    seed: str | None = None
    re_read: bool = False
    lease: SecurityLease | None = None

    def __bool__(self) -> bool:  # allows ``if decision:``
        return self.send


class SecurityLeaseStore:
    """In-process, thread-safe lease store (never persisted, never logged).

    A lease is single-use. ``consume`` marks it used *before* returning a positive
    decision, so two concurrent confirms cannot both send; the loser sees
    ``consumed``. Expiry is checked on every access and a small purge keeps the
    dict from growing without a bound.
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S, *, clock=time.monotonic):
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, SecurityLease] = {}
        self._epochs: dict[tuple, int] = {}

    @staticmethod
    def _key(module, level: int, bus) -> tuple:
        return (normalize_module(module), int(level), str(bus) if bus else "")

    # -- inspection --------------------------------------------------------
    def __len__(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._leases)

    def peek(self, token: str) -> SecurityLease | None:
        """The lease for ``token`` if it is still valid — no consumption."""
        with self._lock:
            self._purge_locked()
            lease = self._leases.get(token or "")
            return lease if lease is not None and not lease.used else None

    def lookup(self, token: str) -> SecurityLease | None:
        """The lease for ``token`` even if it has been consumed (for a clear error)."""
        with self._lock:
            self._purge_locked()
            return self._leases.get(token or "")

    def get(self, token: str) -> SecurityLease | None:
        """Alias kept for callers that read the lease before a confirm modal."""
        return self.peek(token)

    # -- lifecycle ---------------------------------------------------------
    def issue(self, module, level: int, seed, *, bus: str | None = None,
              generation: str = "", ttl_s: float | None = None,
              key_hex: str | None = None, algorithm: str | None = None,
              params: dict | None = None) -> SecurityLease:
        """Hold a seed for a short window and return its unguessable lease."""
        level = int(level)
        if level % 2 == 0 or not 0x01 <= level <= 0x7D:
            raise ValueError("security level must be an odd value in 0x01..0x7D")
        key = self._key(module, level, bus)
        with self._lock:
            self._purge_locked()
            epoch = self._epochs.get(key, 0) + 1
            self._epochs[key] = epoch
        lease = SecurityLease(
            token=secrets.token_urlsafe(32),
            module=normalize_module(module),
            level=level,
            bus=(str(bus) if bus else None),
            seed=normalize_seed(seed),
            generation=str(generation or ""),
            created=self._clock(),
            ttl_s=float(self.ttl_s if ttl_s is None else ttl_s),
            epoch=epoch,
            key_hex=(normalize_seed(key_hex) if key_hex else None),
            algorithm=algorithm,
            params=dict(params or {}),
        )
        with self._lock:
            self._purge_locked()
            self._leases[lease.token] = lease
        return lease

    def invalidate(self, module, level: int, bus: str | None = None) -> None:
        """Supersede every live lease for this target (a new seed was requested)."""
        key = self._key(module, level, bus)
        with self._lock:
            self._epochs[key] = self._epochs.get(key, 0) + 1

    def consume(self, token: str, *, module, level: int,
                bus: str | None = None, generation: str = "",
                strict_seed_reread: bool = False, read_seed=None) -> SendKeyDecision:
        """Validate a lease and decide whether the held key may be sent.

        ``read_seed`` is an optional callable returning the *current* seed. It is
        called **only** when ``strict_seed_reread`` is true; in the default mode
        this function never touches the vehicle, which is the whole point of the
        held-seed policy. A positive decision marks the lease consumed.
        """
        level = int(level)
        want_module = normalize_module(module)
        want_bus = str(bus) if bus else None
        with self._lock:
            self._purge_locked()
            lease = self._leases.get(token or "")
            if lease is None:
                return SendKeyDecision(False, "expired", "no valid lease for this token")
            if lease.used:
                return SendKeyDecision(False, "consumed",
                                       "this lease already authorised one send", lease=lease)
            key = self._key(lease.module, lease.level, lease.bus)
            if lease.epoch != self._epochs.get(key, 0):
                return SendKeyDecision(
                    False, "superseded",
                    "a newer seed request for this module/level replaced this preview",
                    lease=lease,
                )
            if lease.module != want_module or lease.level != level or lease.bus != want_bus:
                return SendKeyDecision(
                    False, "mismatch",
                    "the lease is for module %s level 0x%02X on %s, not %s / 0x%02X / %s"
                    % (lease.module, lease.level, lease.bus or "?", want_module, level,
                       want_bus or "?"),
                    lease=lease,
                )
            if str(generation or "") != lease.generation:
                return SendKeyDecision(
                    False, "generation_mismatch",
                    "the adapter connection changed since this key was previewed",
                    lease=lease,
                )
            # Single use: burn it before any vehicle I/O.
            lease = replace(lease, used=True)
            self._leases[token] = lease

        if not strict_seed_reread:
            # DEFAULT POLICY: the held seed is authoritative; never re-request.
            return SendKeyDecision(True, "ok", "held seed accepted", seed=lease.seed,
                                   re_read=False, lease=lease)

        if read_seed is None:
            return SendKeyDecision(False, "strict_unavailable",
                                   "strict seed re-read was requested but no reader was supplied",
                                   lease=lease)
        try:
            fresh = normalize_seed(read_seed())
        except Exception as exc:  # noqa: BLE001 - a failed re-read must send nothing
            return SendKeyDecision(False, "seed_changed",
                                   f"could not re-read the seed: {exc}", lease=lease)
        if fresh != lease.seed:
            return SendKeyDecision(
                False, "seed_changed",
                "the ECU presented a different seed since the review; no key was sent",
                seed=fresh, re_read=True, lease=lease,
            )
        return SendKeyDecision(True, "ok", "re-read seed matches the review", seed=fresh,
                               re_read=True, lease=lease)

    # -- internals ---------------------------------------------------------
    def authorize(self, token: str, *, module, level: int, bus: str | None = None,
                  generation: str = "") -> SendKeyDecision:
        """The **final** gate: re-validate immediately before the key is sent.

        ``consume`` burns the lease and returns a positive decision, but the
        caller then configures the bus and opens a diagnostic session — seconds
        during which the lease can expire or be superseded by a newer seed
        request. Without this check a positive decision would survive expiry and
        the key would go out anyway (independent adversarial review finding).
        A successful authorisation is itself single-use.
        """
        level = int(level)
        want_module = normalize_module(module)
        want_bus = str(bus) if bus else None
        with self._lock:
            self._purge_locked()  # drops anything past its TTL
            lease = self._leases.get(token or "")
            if lease is None:
                return SendKeyDecision(False, "expired",
                                       "the held seed expired before the key was sent")
            if (lease.module != want_module or lease.level != level
                    or lease.bus != want_bus):
                return SendKeyDecision(False, "mismatch",
                                       "the held seed is for a different target",
                                       lease=lease)
            if str(generation or "") != lease.generation:
                return SendKeyDecision(
                    False, "generation_mismatch",
                    "the adapter connection changed before the key was sent", lease=lease)
            lease_key = self._key(lease.module, lease.level, lease.bus)
            if lease.epoch != self._epochs.get(lease_key, 0):
                return SendKeyDecision(
                    False, "superseded",
                    "a newer seed request replaced this preview", lease=lease)
            if not lease.used:
                return SendKeyDecision(False, "not_consumed",
                                       "this seed was never confirmed", lease=lease)
            if lease.authorized:
                return SendKeyDecision(False, "consumed",
                                       "this seed already authorised a send", lease=lease)
            if lease.expired(self._clock()):
                return SendKeyDecision(False, "expired",
                                       "the held seed expired before the key was sent",
                                       lease=lease)
            authorized = replace(lease, authorized=True)
            self._leases[token] = authorized
            return SendKeyDecision(True, "ok", "authorised at transmit",
                                   seed=lease.seed, lease=authorized)

    def _purge_locked(self) -> None:
        now = self._clock()
        for token, lease in list(self._leases.items()):
            if lease.expired(now):
                self._leases.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._leases.clear()
            self._epochs.clear()
