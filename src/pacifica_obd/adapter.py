"""Device layer for pacifica-obd.

Owns the serial adapter: discovery, opening, bus selection, and protocol
snapshot/restore. It raises typed errors and never prints or exits, so both the
CLI and the TUI (or any other front end) can use it.
"""

from __future__ import annotations

import re

import obd

from pacifica_obd.elm import Elm327, Elm327Error
from pacifica_obd.uds import BUS_PROTOCOL


class AdapterError(RuntimeError):
    """Raised when the adapter is missing, rejected a command, or failed."""


_USB_SERIAL_HINTS = ("usbserial", "usbmodem", "vlinker", "ftdi", "wchusbserial", "slab", "serial")


def find_port(explicit: str | None = None) -> str | None:
    """Return the USB-serial adapter port, or None (never a random console port)."""
    if explicit:
        return explicit
    for port in obd.scan_serial():
        low = port.lower()
        if any(hint in low for hint in _USB_SERIAL_HINTS):
            return port
    return None


def must_ok(elm, command: str) -> None:
    """Run a configuration command and require an 'OK' acknowledgement."""
    response = elm.cmd(command)
    if not any("OK" in line.upper() for line in response):
        raise AdapterError(f"{command!r} rejected by adapter: {response!r}")


def current_protocol(elm):
    """Snapshot the adapter protocol as ('auto',None) | ('at',n) | ('st',n) | None.

    Never raises: a snapshot failure must not stop a diagnostic command.
    """
    try:
        strings = elm.cmd("STPRS")
        numbers = elm.cmd("STPR")
    except Exception:  # noqa: BLE001 - Elm327Error / serial errors
        return None
    strings_line = (strings[0] if strings else "").upper()
    if "AUTO" in strings_line:
        return ("auto", None)
    number_line = (numbers[0] if numbers else "").upper()
    token = number_line.split()[0] if number_line else ""
    if not re.fullmatch(r"[0-9A-Fa-f]{1,2}", token):
        return None
    return ("at" if "[AT]" in number_line else "st", token)


def restore_protocol(elm, previous) -> None:
    """Restore a snapshot without writing adapter EEPROM."""
    if not previous:
        return
    kind, token = previous
    try:
        if kind == "auto":
            elm.cmd("ATTP0")          # try auto, non-saving
        elif kind == "at":
            elm.cmd("ATTP " + token)  # AT namespace, non-saving
        elif kind == "st":
            elm.cmd("STP " + token)   # ST preset (e.g. 33 / 53)
    except Exception:  # noqa: BLE001
        pass


def configure_bus(elm, bus: str, timeout_ms: int) -> int:
    """Select a CAN bus preset and configure framing; returns the ST preset."""
    protocol = BUS_PROTOCOL[bus]
    must_ok(elm, f"STP {protocol}")
    must_ok(elm, "ATH1")
    must_ok(elm, "ATCAF1")
    must_ok(elm, "ATAL")
    must_ok(elm, f"STPTO {timeout_ms}")
    return protocol


def configure_flow_control(elm, header: str, data: str = "3000") -> None:
    """Send ISO-TP flow control for a specific address pair (best effort).

    Without this, multi-frame replies from non-standard pairs (most `-0x280`
    modules and all CAN-IHS modules) never complete and look like "no response".
    `header` is the address the flow-control frame is sent to (the responder).
    """
    for command in ("ATFCSM1", "ATFCSH" + header, "ATFCSD" + data, "ATFCSM1"):
        try:
            elm.cmd(command)
        except Exception:  # noqa: BLE001 - flow control is best-effort
            return


def reset_flow_control(elm) -> None:
    try:
        elm.cmd("ATFCSM0")  # back to automatic flow control
    except Exception:  # noqa: BLE001
        pass


class ElmAdapter:
    """Context-managed owner of the raw adapter with protocol restoration."""

    def __init__(self, port: str | None = None, baudrate: int = 115200,
                 timeout: float = 2.0, autoconnect: bool = True):
        resolved = find_port(port)
        if not resolved:
            raise AdapterError("no serial port found (plug in the vLinker FS)")
        self.port = resolved
        self._elm: Elm327 | None = None
        self._previous = None
        if autoconnect:
            self.open(baudrate=baudrate, timeout=timeout)

    def open(self, baudrate: int = 115200, timeout: float = 2.0) -> None:
        try:
            self._elm = Elm327(self.port, baudrate, timeout=timeout)
        except (Elm327Error, ValueError, OSError) as exc:
            raise AdapterError(str(exc)) from exc
        self._previous = current_protocol(self._elm)

    @property
    def connected(self) -> bool:
        return self._elm is not None

    def cmd(self, command: str, wait: float | None = None) -> list[str]:
        if self._elm is None:
            raise AdapterError("adapter is not open")
        return self._elm.cmd(command, wait=wait)

    def configure(self, bus: str, timeout_ms: int) -> int:
        return configure_bus(self, bus, timeout_ms)

    def close(self) -> None:
        if self._elm is None:
            return
        elm = self._elm
        try:
            restore_protocol(elm, self._previous)
        finally:
            try:
                elm.close()
            finally:
                self._elm = None

    def __enter__(self) -> "ElmAdapter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
