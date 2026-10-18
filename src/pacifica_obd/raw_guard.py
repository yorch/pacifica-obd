"""Tier classifier for a raw command surface (design §3.14).

The web Raw console (and, later, the CLI/TUI if they choose to adopt it) must
decide, **before anything is sent**, whether one command may go to the adapter:

* :data:`READ` — a command that cannot meaningfully change vehicle state; sent
  with no confirmation.
* :data:`CONFIRM` — a command that is not a known read and not a known hazard;
  allowed only with a single-use confirm token, a typed acknowledgement naming
  the exact bytes, and a server in write mode.
* :data:`DENY` — never sendable, in any mode, ever.

This module is **pure and dependency-free** so the CLI, the TUI and the web layer
can share one rule. It is the only place the tiers are defined; the
``/api/raw/allowlist`` endpoint serialises these constants so the UI and the
tests read the same source of truth.

Scope: the web Raw console enforces this classification on every request. The
CLI's ``raw`` command is a deliberate expert escape hatch and does **not** consult
it (adopting it there, with an explicit ``--force`` for the deny tier, is future
work); the invariant below is a property of this classifier and its web caller,
not of the whole project.

Parsing is *reject-first*: a raw endpoint that can smuggle a second command is
the main bypass, so a command containing a carriage return, a newline, a
semicolon or a backtick, or that is not exactly one hex request or one ``AT…``
command, is refused outright. Case is normalised, then the command is classified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "READ",
    "CONFIRM",
    "DENY",
    "Decision",
    "RawCommandError",
    "MAX_COMMAND_LENGTH",
    "REJECT_CHARACTERS",
    "classify",
    "normalise",
    "allowlist",
    "OBD_READ_MODES",
    "UDS_READ_SERVICES",
    "AT_READ_EXACT",
    "AT_READ_ARG",
    "OBD_DENY_MODES",
    "UDS_DENY_SERVICES",
    "AT_DENY_EXACT",
    "AT_FLOW_CONTROL_PREFIXES",
    "AT_DENY_PREFIXES",
]

READ = "read"
CONFIRM = "confirm"
DENY = "deny"

#: A bounded command: long enough for every real read, far too short to hide a
#: second command. Enforced before classification.
MAX_COMMAND_LENGTH = 64

#: Characters that could separate or smuggle a second command. Refused outright.
REJECT_CHARACTERS = ("\r", "\n", ";", "`")

_REJECT_REASON = {
    "\r": "a raw command must be a single line (carriage return is not allowed)",
    "\n": "a raw command must be a single line (newline is not allowed)",
    ";": "a raw command cannot contain ';' (a smuggled second command is refused)",
    "`": "a raw command cannot contain a backtick",
}

#: The only AT commands that may be followed by one hex argument.
AT_ARG_TAKING = frozenset({"ATSH", "ATCRA", "ATTP"})

_COMMAND_TOKEN = re.compile(r"AT[0-9A-Z]*")
_HEX_TOKEN = re.compile(r"[0-9A-Fa-f]+")
_AT_ARG = re.compile(r"[0-9A-Fa-f]{1,8}")
_CANONICAL = re.compile(r"[0-9A-Z]+")

# -- tier tables (the allowlist endpoint serialises exactly these) ---------- #
OBD_READ_MODES = {
    "01": "current powertrain data (live PIDs)",
    "03": "stored emissions DTCs",
    "07": "pending emissions DTCs",
    "09": "vehicle information (VIN, calibration)",
    "0A": "permanent emissions DTCs",
}

UDS_READ_SERVICES = {
    "10": "diagnostic session control",
    "19": "read DTC information",
    "22": "read data by identifier",
    "23": "read memory by address",
    "24": "read scaling information",
    "3E": "tester present",
}

#: AT commands this tool itself uses on a read/encoding path. ``ATSH``/``ATCRA``
#: address the adapter; ``ATTP`` selects a protocol *temporarily* (never ``ATSP``).
AT_READ_EXACT = frozenset({
    "ATI", "ATRV", "ATDP", "ATDPN", "ATAR", "ATS1", "ATS0", "ATH1", "ATH0",
    "ATE0", "ATE1", "ATL1", "ATL0", "ATCAF1", "ATD",
})
AT_READ_ARG = ("ATSH", "ATCRA", "ATTP")

#: ``code -> (reason, note, pointer)``. A pointer names the screen that *does*
#: expose the operation (the UI turns it into a link); ``None`` means "nowhere".
OBD_DENY_MODES = {
    "04": ("clearing DTCs (mode 04) erases stored codes, freeze-frame data and "
           "readiness monitors", None, None),
}

UDS_DENY_SERVICES = {
    "11": ("ECU reset (0x11) restarts a module", None, None),
    "14": ("clear diagnostic information (0x14) erases stored DTCs", None, None),
    "27": ("security access (0x27) is not sent from the raw console",
           "Use Security 0x27 — it previews the key and holds the seed under a lease.",
           "security"),
    "28": ("communication control (0x28) can silence a module", None, None),
    "2E": ("write data by identifier (0x2E) is a write",
           "Use Guarded writes.", "writes"),
    "2F": ("input/output control (0x2F) is not exposed in the browser", None, None),
    "31": ("routine control (0x31) can activate a routine",
           "Use Guarded writes.", "writes"),
    "34": ("request download (0x34) is not exposed in the browser", None, None),
    "35": ("request upload (0x35) is not exposed in the browser", None, None),
    "36": ("transfer data (0x36) is not exposed in the browser", None, None),
    "37": ("request transfer exit (0x37) is not exposed in the browser", None, None),
    "3D": ("write memory by address (0x3D) is not exposed in the browser", None, None),
    "85": ("control DTC setting (0x85) can hide faults", None, None),
}

#: Manual ISO-TP flow control switches the STN to an indexed frame format that
#: breaks multi-frame parsing, so a read that worked then reports "no response".
AT_FLOW_CONTROL_PREFIXES = ("ATFCSM", "ATFCSH", "ATFCSD")

#: Resets, monitor modes and EEPROM/protocol writes. Matched as **prefixes**: the
#: ELM forms take arguments (``ATWM<hex>``, ``ATSW<nn>``), so an exact match would
#: let ``ATWM112233`` slip through to the confirm tier. ``ATSP`` persists to the
#: adapter's EEPROM, which AGENTS.md explicitly forbids.
AT_DENY_EXACT = {
    "ATZ": "a full adapter reset",
    "ATWS": "an adapter warm start",
    "ATRST": "an adapter reset",
    "ATMA": "monitor-all streams every frame with no way to stop it from here",
    "ATWM": "a wake-up message write",
    "ATSW": "a wake/sleep interval write",
    "ATPPS": "a pin-state write",
    # With auto-formatting off the adapter sends the hex bytes literally, so the
    # first data byte becomes the ISO-TP PCI and "0104000000000000" would deliver
    # service 04 (clear DTCs) while classifying as a mode-01 read. The only reason
    # to turn it off is to hand-craft frames, which is the one thing this console
    # exists to prevent; the route re-asserts ATCAF1 before every hex request as
    # well, in case something else left framing off.
    "ATCAF0": ("disabling CAN auto-formatting lets hand-built frames carry any "
               "service, including the writes this console refuses"),
}

AT_DENY_PREFIXES = {
    "ATSP": "writes the protocol to the adapter EEPROM (use ATTP to try one temporarily)",
    # CAN extended addressing moves the first data byte out of the payload and into
    # the address position, so "22F190" would no longer deliver service 0x22 — and
    # ATCAF1 does not undo it. Nothing in a diagnostics console needs it.
    "ATCEA": ("CAN extended addressing shifts the first data byte into the address "
              "position, so a request would not deliver the service it classifies as"),
}


class RawCommandError(ValueError):
    """A command that cannot be parsed as one bounded request."""


@dataclass(frozen=True)
class Decision:
    """The classification of one command. ``tier`` is the decision that matters."""

    tier: str
    reason: str
    note: str | None = None
    pointer: str | None = None
    command: str = ""
    kind: str = "invalid"            # "hex" | "at" | "invalid"
    service: str | None = None
    label: str | None = None

    @property
    def valid(self) -> bool:
        """False when the command could not even be parsed (a deny)."""
        return self.kind != "invalid"

    @property
    def sendable_now(self) -> bool:
        """True only for the read tier: sendable with no confirmation."""
        return self.valid and self.tier == READ

    def as_dict(self) -> dict:
        return {
            "tier": self.tier,
            "reason": self.reason,
            "note": self.note,
            "pointer": self.pointer,
            "command": self.command,
            "kind": self.kind,
            "service": self.service,
            "label": self.label,
            "valid": self.valid,
        }


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _parse(command) -> tuple[str, str, str | None]:
    """Return ``(kind, canonical_command, service)`` or raise RawCommandError.

    ``kind`` is ``"hex"`` or ``"at"``; ``service`` is the first hex byte or the
    AT command name. ``canonical_command`` is the exact uppercase bytes to send.
    """
    if command is None:
        raise RawCommandError("an empty command cannot be sent")
    text = str(command)
    for char in REJECT_CHARACTERS:
        if char in text:
            raise RawCommandError(_REJECT_REASON[char])
    stripped = text.strip()
    if not stripped:
        raise RawCommandError("an empty command cannot be sent")
    if len(stripped) > MAX_COMMAND_LENGTH:
        raise RawCommandError(
            f"command is too long (max {MAX_COMMAND_LENGTH} characters)"
        )

    parts = stripped.split()
    if parts[0][:2].upper() == "AT":
        name = parts[0].upper()
        if len(parts) == 1:
            argument = None
        elif len(parts) == 2 and name in AT_ARG_TAKING:
            if not _AT_ARG.fullmatch(parts[1]):
                raise RawCommandError(
                    f"{name} takes one hex argument, e.g. '{name} 7E0'"
                )
            argument = parts[1].upper()
        else:
            raise RawCommandError("only one command may be sent per submit")
        if not _COMMAND_TOKEN.fullmatch(name):
            raise RawCommandError(f"not an AT command: {name!r}")
        return "at", name + (argument or ""), name

    for token in parts:
        if not _HEX_TOKEN.fullmatch(token):
            raise RawCommandError(
                "a raw request is one command only: hex bytes or a single AT command"
            )
    digits = "".join(parts).upper()
    if len(digits) < 2:
        raise RawCommandError("a hex request needs at least one byte")
    if len(digits) % 2:
        raise RawCommandError("hex bytes must be an even number of digits")
    return "hex", digits, digits[:2]


def normalise(command) -> str:
    """The canonical uppercase command (``"22 f1 90"`` -> ``"22F190"``)."""
    return _parse(command)[1]


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def classify(command) -> Decision:
    """Classify one command into a tier. Never raises; parse failures are denied."""
    try:
        kind, compact, service = _parse(command)
    except RawCommandError as exc:
        return Decision(DENY, str(exc), command="", kind="invalid")
    if not _CANONICAL.fullmatch(compact):
        # Defence in depth: reject anything the parser could not canonicalise
        # (unicode look-alikes, control characters, stray punctuation).
        return Decision(DENY, "the command contains characters that are not allowed",
                        command="", kind="invalid")
    if kind == "at":
        return _classify_at(compact, service or compact)
    return _classify_hex(compact, service or "")


def _classify_hex(compact: str, service: str) -> Decision:
    denied = OBD_DENY_MODES.get(service)
    if denied:
        reason, note, pointer = denied
        return Decision(DENY, reason, note=note, pointer=pointer, command=compact,
                        kind="hex", service=service)
    denied = UDS_DENY_SERVICES.get(service)
    if denied:
        reason, note, pointer = denied
        return Decision(DENY, reason, note=note, pointer=pointer, command=compact,
                        kind="hex", service=service)
    if service == "10":
        # Session control is a read *only* for the sessions a read needs. 0x02 is
        # the programming session (flashing) — never from here — and anything else
        # is not something to enter without saying so.
        sub = compact[2:4] if len(compact) >= 4 else ""
        label = UDS_READ_SERVICES["10"]
        if sub == "02":
            return Decision(DENY, "diagnostic session 0x02 is the programming session; "
                                  "it is not entered from the raw console",
                            command=compact, kind="hex", service=service)
        if sub in ("01", "03"):
            return Decision(READ, f"UDS service 0x10: {label} (session 0x{sub})",
                            command=compact, kind="hex", service=service, label=label)
        return Decision(CONFIRM, "session control with a subfunction other than "
                                 "0x01/0x03 needs confirmation",
                        command=compact, kind="hex", service=service)
    if service in OBD_READ_MODES:
        label = OBD_READ_MODES[service]
        return Decision(READ, f"OBD-II mode {service}: {label}", command=compact,
                        kind="hex", service=service, label=label)
    if service in UDS_READ_SERVICES:
        label = UDS_READ_SERVICES[service]
        return Decision(READ, f"UDS service 0x{service}: {label}",
                        command=compact, kind="hex", service=service, label=label)
    return Decision(CONFIRM, f"service 0x{service} is not on the read allowlist; "
                             "confirm the exact bytes to send it",
                    command=compact, kind="hex", service=service)


def _classify_at(compact: str, name: str) -> Decision:
    for prefix in AT_FLOW_CONTROL_PREFIXES:
        if compact.startswith(prefix):
            return Decision(
                DENY,
                f"{prefix}… sets manual ISO-TP flow control, which switches the "
                "STN to an indexed frame format and breaks parsing",
                command=compact, kind="at", service=name)
    for prefix, reason in AT_DENY_PREFIXES.items():
        if compact.startswith(prefix):
            return Decision(DENY, f"{prefix}… {reason}", command=compact,
                            kind="at", service=name)
    for command, reason in AT_DENY_EXACT.items():
        if compact.startswith(command):
            return Decision(DENY, f"{command}… {reason}", command=compact,
                            kind="at", service=name)
    if compact in AT_READ_EXACT:
        return Decision(READ, f"{compact} is on the read allowlist",
                        command=compact, kind="at", service=name)
    for cmd in AT_READ_ARG:
        if compact == cmd:
            return Decision(READ, f"{cmd} addresses the adapter and is on the read allowlist",
                            command=compact, kind="at", service=name)
        if compact.startswith(cmd) and _HEX_TOKEN.fullmatch(compact[len(cmd):]):
            return Decision(READ, f"{cmd} is on the read allowlist", command=compact,
                            kind="at", service=name)
    return Decision(CONFIRM, f"{name} is not on the read allowlist; confirm the "
                             "exact bytes to send it",
                    command=compact, kind="at", service=name)


# --------------------------------------------------------------------------- #
# the tiers as data (one source of truth for the UI and the tests)
# --------------------------------------------------------------------------- #
def allowlist() -> dict:
    """The read/confirm/deny tables as JSON-able data for ``GET /api/raw/allowlist``."""
    return {
        "max_length": MAX_COMMAND_LENGTH,
        "reject": {
            "characters": ["\\r", "\\n", ";", "`"],
            "rule": ("one bounded command per submit: hex bytes or a single AT "
                     "command; a smuggled second command is refused"),
        },
        "read": {
            "obd_modes": [{"code": code, "label": OBD_READ_MODES[code]}
                          for code in sorted(OBD_READ_MODES)],
            "uds_services": [{"code": code, "label": UDS_READ_SERVICES[code]}
                             for code in sorted(UDS_READ_SERVICES)],
            "at_exact": sorted(AT_READ_EXACT),
            "at_argument": list(AT_READ_ARG),
        },
        "confirm": {
            "rule": ("any other service id or AT command: needs a single-use "
                     "confirm token, a typed acknowledgement equal to the exact "
                     "bytes, and a server started with --allow-writes"),
        },
        "deny": {
            "obd_modes": [{"code": code, "reason": OBD_DENY_MODES[code][0]}
                          for code in sorted(OBD_DENY_MODES)],
            "uds_services": [{"code": code, "reason": UDS_DENY_SERVICES[code][0],
                              "note": UDS_DENY_SERVICES[code][1],
                              "pointer": UDS_DENY_SERVICES[code][2]}
                             for code in sorted(UDS_DENY_SERVICES)],
            "at_exact": [{"command": command, "reason": AT_DENY_EXACT[command]}
                         for command in sorted(AT_DENY_EXACT)],
            "at_prefixes": [{"prefix": prefix, "reason": AT_DENY_PREFIXES[prefix]}
                            for prefix in sorted(AT_DENY_PREFIXES)]
            + [{"prefix": prefix,
                "reason": "sets manual ISO-TP flow control, which breaks parsing"}
               for prefix in AT_FLOW_CONTROL_PREFIXES],
        },
    }
