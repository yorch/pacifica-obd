"""Guards for what this tool may send on a *read* path.

python-OBD's ``OBD.supported_commands`` is seeded from ``commands.base_commands()``
and only ever added to, so on any real connection it contains commands that are
**not reads** — notably ``CLEAR_DTC`` (mode ``04``), which erases stored codes,
freeze-frame data and readiness monitors. Enumerating ``supported_commands`` and
querying each entry (a natural way to write a "live data" view) therefore sends
mode ``04`` and destroys the very evidence the tool exists to read.

Only mode ``01`` (current powertrain data) is a read, so this module uses a
positive allowlist rather than a blocklist. AGENTS.md: *"Never clear DTCs or
write data without an explicit user request and a confirmation path."*

Shared by the CLI, the TUI and the web service so the rule cannot drift.
"""

from __future__ import annotations

__all__ = ["REFUSED_COMMANDS", "command_text", "is_read_only_command"]

# Defence in depth: even if one of these ever reported mode 01, never send it.
REFUSED_COMMANDS = frozenset({
    "CLEAR_DTC", "RESET_DTC", "CLEAR_EMISSIONS", "CLEAR_FREEZE_FRAME",
})


def command_text(command) -> str:
    """The ASCII command string of a python-OBD command (``"010C"``)."""
    raw = getattr(command, "command", b"")
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


def is_read_only_command(command) -> bool:
    """True for a command this tool may send on a read path (mode 01 only)."""
    name = str(getattr(command, "name", "") or "").upper()
    if name in REFUSED_COMMANDS:
        return False
    return command_text(command).startswith("01")
