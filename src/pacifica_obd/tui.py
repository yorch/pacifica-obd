"""Interactive terminal UI (Textual) for pacifica-obd.

A keyboard- and mouse-driven front end over the same helpers the CLI uses:
generic OBD-II reads (live PIDs, readiness, freeze frame, VIN) and
manufacturer-specific UDS work (module discovery, all-module DTCs, DID reads)
through the raw ELM/STN transport in :mod:`pacifica_obd.elm`.

Design notes
------------
* The serial transport is synchronous and blocking, so every operation runs on
  a Textual **thread worker**; results are handed back to the UI thread with
  ``App.call_from_thread``. Nothing blocking ever runs on the event loop.
* A single non-blocking lock means only one operation owns the adapter at a
  time; a second request is refused with a visible message instead of
  interleaving two conversations on the same CAN bus.
* Lifecycle: the protocol is snapshotted with ``current_protocol()`` when the
  port opens and restored with ``restore_protocol()`` before ``close()`` on
  disconnect / app exit.
* Exceptions never reach the UI as a traceback: worker bodies are wrapped and
  reported as log lines plus a notification.

Launch with ``pacifica-obd tui`` (or ``python -c 'from pacifica_obd.tui import run; run()'``).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial

from rich.table import Table
from rich.text import Text
from serial.tools import list_ports
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
    Switch,
)

from pacifica_obd.cli import (
    DEFAULT_BAUD,
    DEFAULT_WATCH,
    _BUS_PROTOCOL,
    _classify_dtc_response,
    _did_ascii,
    _did_value,
    _enter_session,
    _FREEZE_PIDS,
    _INFO_DIDS,
    _parse_dtcs,
    _parse_uds_frame,
    _read_did,
    configure_bus,
    current_protocol,
    find_port,
    restore_protocol,
)
from pacifica_obd.elm import Elm327, Elm327Error

__all__ = ["run", "PacificaTui"]


class _Refused(Exception):
    """An operation could not start: shutting down, no adapter, or adapter busy.

    Raised by ``PacificaTui._exclusive`` so a worker can never accidentally run
    its body without owning the adapter.
    """


# Per-request CAN timeout (STPTO) and the derived read wait, mirroring the CLI.
DEFAULT_TIMEOUT_MS = 100
MAX_LOG_LINES = 4000

_BUS_LABELS = {
    "can-c": "CAN-C (HS 500k)",
    "can-ihs": "CAN-IHS (MS 125k)",
    "both": "both buses",
}

# Protocol snapshot shape returned by cli.current_protocol().
Snapshot = tuple[str, str | None] | None

# --------------------------------------------------------------------------- #
# generic OBD-II PID decoding (raw mode 01, no python-OBD needed)
# --------------------------------------------------------------------------- #
# name -> (request, payload bytes required, decoder, unit). Payload is what
# follows the "41 <pid>" echo of the reply.
Pid = tuple[str, int, Callable[[bytes], float], str]


def _u8(data: bytes) -> float:
    return float(data[0])


def _temp(data: bytes) -> float:
    return float(data[0] - 40)


def _percent(data: bytes) -> float:
    return data[0] * 100.0 / 255.0


def _rpm(data: bytes) -> float:
    return ((data[0] << 8) | data[1]) / 4.0


def _maf(data: bytes) -> float:
    return ((data[0] << 8) | data[1]) / 100.0


_PIDS: dict[str, Pid] = {
    "RPM": ("010C", 2, _rpm, "rpm"),
    "SPEED": ("010D", 1, _u8, "km/h"),
    "COOLANT_TEMP": ("0105", 1, _temp, "°C"),
    "ENGINE_LOAD": ("0104", 1, _percent, "%"),
    "THROTTLE_POS": ("0111", 1, _percent, "%"),
    "INTAKE_TEMP": ("010F", 1, _temp, "°C"),
    "MAF": ("0110", 2, _maf, "g/s"),
    "FUEL_LEVEL": ("012F", 1, _percent, "%"),
}
# ATRV is an adapter command, not a PID; handled separately in the poll loop.
_VOLTAGE_KEY = "ELM_VOLTAGE"
_PID_NAMES = [*_PIDS, _VOLTAGE_KEY]

# Readiness (mode 01 PID 01) bit names, copied from python-OBD's obd/codes.py so
# the layout matches `obd.decoders.status` exactly (see _decode_readiness).
_BASE_TESTS = ["MISFIRE_MONITORING", "FUEL_SYSTEM_MONITORING", "COMPONENT_MONITORING"]
_SPARK_TESTS: list[str | None] = [
    "CATALYST_MONITORING",
    "HEATED_CATALYST_MONITORING",
    "EVAPORATIVE_SYSTEM_MONITORING",
    "SECONDARY_AIR_SYSTEM_MONITORING",
    None,
    "OXYGEN_SENSOR_MONITORING",
    "OXYGEN_SENSOR_HEATER_MONITORING",
    "EGR_VVT_SYSTEM_MONITORING",
]
_COMPRESSION_TESTS: list[str | None] = [
    "NMHC_CATALYST_MONITORING",
    "NOX_SCR_AFTERTREATMENT_MONITORING",
    None,
    "BOOST_PRESSURE_MONITORING",
    None,
    "EXHAUST_GAS_SENSOR_MONITORING",
    "PM_FILTER_MONITORING",
    "EGR_VVT_SYSTEM_MONITORING",
]
_MONITOR_LABELS = {
    "MISFIRE_MONITORING": "Misfire",
    "FUEL_SYSTEM_MONITORING": "Fuel system",
    "COMPONENT_MONITORING": "Components",
    "CATALYST_MONITORING": "Catalyst",
    "HEATED_CATALYST_MONITORING": "Heated catalyst",
    "EVAPORATIVE_SYSTEM_MONITORING": "Evaporative system",
    "SECONDARY_AIR_SYSTEM_MONITORING": "Secondary air",
    "OXYGEN_SENSOR_MONITORING": "Oxygen sensor",
    "OXYGEN_SENSOR_HEATER_MONITORING": "Oxygen sensor heater",
    "EGR_VVT_SYSTEM_MONITORING": "EGR/VVT",
    "NMHC_CATALYST_MONITORING": "NMHC catalyst",
    "NOX_SCR_AFTERTREATMENT_MONITORING": "NOx/SCR",
    "BOOST_PRESSURE_MONITORING": "Boost pressure",
    "EXHAUST_GAS_SENSOR_MONITORING": "Exhaust gas sensor",
    "PM_FILTER_MONITORING": "PM filter",
}


def _bit(data: bytes, index: int) -> bool:
    """MSB-first bit read over a payload, as used by mode 01 PID 01."""
    byte = index // 8
    if byte >= len(data):
        return False
    return bool(data[byte] & (0x80 >> (index % 8)))


def _decode_readiness(data: bytes) -> dict:
    """Decode a 4-byte mode 01 PID 01 payload (SAE J1979)."""
    compression = _bit(data, 12)
    monitors: list[tuple[str, bool, bool]] = []
    for i, name in enumerate(reversed(_BASE_TESTS)):
        monitors.append((name, _bit(data, 13 + i), not _bit(data, 9 + i)))
    table = _COMPRESSION_TESTS if compression else _SPARK_TESTS
    for i, name in enumerate(reversed(table)):
        if name is None:  # reserved bit
            continue
        monitors.append((name, _bit(data, 16 + i), not _bit(data, 24 + i)))
    return {
        "mil": _bit(data, 0),
        "dtc_count": data[0] & 0x7F,
        "ignition": "compression" if compression else "spark",
        "monitors": monitors,
    }


def _decode_obd_dtc(data: bytes) -> str | None:
    """Two-byte OBD DTC (e.g. from mode 01 PID 02) -> 'P0300'."""
    if data[0] == 0 and data[1] == 0:
        return None
    letter = "PCBU"[(data[0] >> 6) & 0x03]
    return f"{letter}{(data[0] >> 4) & 0x03}{data[0] & 0x0F:X}{data[1]:02X}"


def _decode_vin(uds: str) -> str | None:
    """Pull the VIN out of a mode 09 02 ('4902…') payload."""
    if not uds.startswith("4902"):
        return None
    body = uds[4:]
    if not body or len(body) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", body):
        return None
    raw = bytes.fromhex(body)
    if len(raw) >= 18 and raw[0] == 0x01:  # drop the ISO 14229 data-item counter
        raw = raw[1:]
    text = "".join(chr(byte) for byte in raw if 0x20 <= byte <= 0x7E)
    text = re.sub(r"[^0-9A-Za-z]", "", text)
    return text[:17] if len(text) >= 17 else None


def _parse_voltage(lines: list[str]) -> float | None:
    """ATRV answers '12.4' (some clones append a unit)."""
    for line in lines:
        match = re.search(r"\d+(?:\.\d+)?", line)
        if match:
            return float(match.group(0))
    return None


def _payload_after(lines: list[str], prefix: str) -> bytes | None:
    """Return the hex payload following `prefix` in a single UDS/OBD reply."""
    _, uds = _parse_uds_frame(lines)
    if uds is None or not uds.startswith(prefix):
        return None
    body = uds[len(prefix):]
    if not body or len(body) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", body):
        return None
    return bytes.fromhex(body)


def _protocol_label(snapshot: Snapshot) -> str:
    """Render a current_protocol() snapshot for the status bar."""
    if not snapshot:
        return "—"
    kind, token = snapshot
    if kind == "auto":
        return "auto"
    return f"{kind.upper()} {token}"


def _probe_wait(timeout_ms: int = DEFAULT_TIMEOUT_MS) -> float:
    """Read wait used by the CLI for UDS probes/DID reads."""
    return max(0.25, timeout_ms / 1000 + 0.15)


def _buses_for(bus: str) -> list[str]:
    if bus == "both":
        return ["can-c", "can-ihs"]
    return [bus if bus in _BUS_PROTOCOL else "can-c"]


# --------------------------------------------------------------------------- #
# adapter plumbing
# --------------------------------------------------------------------------- #
def _prepare_bus(
    elm: Elm327,
    bus: str,
    warn: Callable[[str], None] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> int:
    """Pin the STN preset for `bus` and clear any leftover receive filter.

    ``configure_bus`` (reused from the CLI) leaves the CAN receive address
    filter alone; a previous session read leaves it narrowed to one ECU, which
    would silently hide every other module. Sending ``ATCRA`` with no argument
    puts the adapter back in "receive everything" mode.

    Rejections are reported through `warn` (the caller marshals them to the UI)
    rather than ignored, because a stale filter silently changes the results.
    """
    protocol = configure_bus(elm, bus, timeout_ms)
    for command in ("ATCRA", "ATSH 7E0"):
        if not _acknowledged(elm.cmd(command)):
            _warn(warn, f"{command!r} was not acknowledged; results may be incomplete")
    return protocol


def _acknowledged(lines: list[str]) -> bool:
    """True if any response line is the adapter's 'OK'."""
    return any("OK" in line.upper() for line in lines)


def _warn(warn: Callable[[str], None] | None, message: str) -> None:
    if warn is not None:
        warn(message)


def _probe_modules(
    elm: Elm327,
    start: int,
    end: int,
    probe: str,
    wait: float,
    cancel: threading.Event,
    warn: Callable[[str], None] | None = None,
) -> Iterator[dict]:
    """Cancellable probe of UDS request IDs, mirroring ``cli._probe_ids``.

    The CLI's generator cannot be interrupted and only yields responders, so a
    Stop press while scanning an empty range would otherwise have no effect.
    Parsing still goes through the shared ``_parse_uds_frame``.

    A rejected ``ATSH`` skips that ID instead of querying the previous module:
    with the old header still in place its reply would be attributed to the
    wrong module.
    """
    service = int(probe[:2], 16)
    for rid in range(start, end + 1):
        if cancel.is_set():
            return
        if not _acknowledged(elm.cmd("ATSH %03X" % rid)):
            _warn(warn, f"ATSH {rid:03X} was not acknowledged; skipping that ID")
            continue
        header, uds = _parse_uds_frame(elm.cmd(probe, wait=wait))
        if header is None:
            continue
        if uds.startswith("%02X" % (service + 0x40)):
            kind = "positive"
        elif uds.startswith("7F"):
            kind = "negative"
        else:
            kind = "other"
        yield {
            "request_id": "%03X" % rid,
            "response_id": header,
            "kind": kind,
            "session_response": uds,
        }


def _list_serial_ports() -> list[str]:
    """Enumerate candidate serial devices *without* opening any of them.

    ``obd.scan_serial()`` probes every device with ``serial.Serial(...)``, which
    would reopen (and toggle control lines on) the adapter this app is already
    using. Enumeration here is purely passive.
    """
    try:
        devices = [info.device for info in list_ports.comports() if info.device]
    except Exception:  # noqa: BLE001 - enumeration is best effort
        return []
    names = set(devices)
    for device in list(names):  # prefer callout (cu.*) names over dial-in (tty.*)
        if "/dev/tty." in device:
            callout = device.replace("/dev/tty.", "/dev/cu.", 1)
            if callout in names:
                names.discard(device)

    def rank(device: str) -> tuple[int, str]:
        low = device.lower()
        preferred = any(key in low for key in ("usbserial", "vlinker", "ftdi", "usbmodem"))
        return (0 if preferred else 1, device)

    return sorted(names, key=rank)


def _release_handle(elm: Elm327, previous: Snapshot) -> None:
    """Restore the protocol snapshot and close the port. Never raises."""
    try:
        restore_protocol(elm, previous)
    except Exception:  # noqa: BLE001 - adapter may already be gone
        pass
    elm.close()


def _open_adapter(port: str) -> tuple[Elm327, Snapshot, str]:
    """Open the port, snapshot the protocol and read the adapter ID."""
    elm = Elm327(port, DEFAULT_BAUD, timeout=1.0)
    try:
        previous = current_protocol(elm)
        lines = elm.cmd("ATI")
    except Exception:
        elm.close()
        raise
    return elm, previous, (lines[0].strip() if lines else "")


# --------------------------------------------------------------------------- #
# input validation (the UI counterpart of the CLI's argparse types)
# --------------------------------------------------------------------------- #
def _parse_module_id(text: str) -> str | None:
    """Validate an 11-bit CAN request ID such as '7E0'; return 3-hex upper-case."""
    token = text.strip().upper().removeprefix("0X")
    if not re.fullmatch(r"[0-9A-F]{1,3}", token):
        return None
    value = int(token, 16)
    return "%03X" % value if value <= 0x7FF else None


def _parse_dids(text: str) -> list[int] | None:
    """Validate a comma/space separated list of 16-bit DIDs."""
    tokens = [
        t.strip().upper().removeprefix("0X")
        for t in re.split(r"[,\s]+", text.strip())
        if t.strip()
    ]
    if not tokens or any(not re.fullmatch(r"[0-9A-F]{4}", t) for t in tokens):
        return None
    return [int(t, 16) for t in tokens]


def _parse_range(text: str) -> tuple[int, int] | None:
    """Validate a 'START-END' hex request-ID range (both 11-bit, start <= end)."""
    match = re.fullmatch(r"\s*([0-9A-Fa-f]{1,3})\s*[-:]\s*([0-9A-Fa-f]{1,3})\s*", text)
    if not match:
        return None
    start, end = int(match.group(1), 16), int(match.group(2), 16)
    if start > end or end > 0x7FF:
        return None
    return start, end


def _parse_pids(text: str) -> list[str] | None:
    """Validate a comma separated list of known live-data PID names.

    Duplicates are dropped: the live table keys rows by PID, so a repeated name
    would fail to build the table.
    """
    names: list[str] = []
    for token in text.split(","):
        name = token.strip().upper()
        if not name:
            continue
        if name not in _PID_NAMES:
            return None
        if name not in names:
            names.append(name)
    return names or None


def _parse_interval(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if 0.2 <= value <= 10.0 else None


def _parse_raw_command(text: str) -> str | None:
    """Accept one printable AT/ST/OBD command and nothing that could be injected."""
    command = text.strip()
    if not command or len(command) > 64:
        return None
    return command if re.fullmatch(r"[0-9A-Za-z ._-]+", command) else None


# Vehicle services that write, clear or reset state, and adapter commands that
# persist to EEPROM. Both need an explicit yes/no before they go out.
_RAW_DANGEROUS_SERVICES = {
    "04": "mode 04 clears emissions DTCs and resets readiness monitors",
    "08": "OBD mode 08 controls on-board systems (actuators, evap test)",
    "11": "UDS 0x11 resets (reboots) the addressed module",
    "14": "UDS 0x14 clears stored DTCs",
    "27": "UDS 0x27 security access can unlock write routines",
    "28": "UDS 0x28 communication control can silence a module",
    "2E": "UDS 0x2E writes a data identifier",
    "2F": "UDS 0x2F writes an input/output control",
    "31": "UDS 0x31 starts a routine on the module",
    "34": "UDS 0x34 requests a download (firmware/calibration write)",
    "36": "UDS 0x36 transfers data to the module",
    "37": "UDS 0x37 ends a transfer (commits the data)",
    "38": "UDS 0x38 requests a file transfer",
    "3B": "UDS 0x3B writes to module memory",
    "3D": "UDS 0x3D writes to module memory",
    "85": "UDS 0x85 can disable DTC storage in the module",
}
_RAW_PERSISTENT_AT_PREFIXES = ("ATSP", "ATPP")


def _raw_risk(command: str) -> str | None:
    """Explain why `command` is not a plain read, or None if it is safe.

    ELM/ST commands tolerate spaces (``AT SP 6``), so compare the space-free
    form for those too, not just for hex service requests.
    """
    compact = command.upper().replace(" ", "")
    if re.fullmatch(r"[0-9A-F]+", compact) and len(compact) >= 2:
        return _RAW_DANGEROUS_SERVICES.get(compact[:2])
    if compact.startswith(_RAW_PERSISTENT_AT_PREFIXES):
        return "these adapter commands write the protocol choice to EEPROM"
    return None


# --------------------------------------------------------------------------- #
# modal screens
# --------------------------------------------------------------------------- #
class PortPicker(ModalScreen[str | None]):
    """Pick a serial port (or auto-detect). Dismisses with the path / '' / None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, ports: list[str], current: str | None) -> None:
        super().__init__()
        self._ports = ports
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Serial port", id="dialog-title")
            if not self._ports:
                yield Label("(no candidate ports found — is the adapter plugged in?)", classes="hint")
            items = [ListItem(Label("(auto-detect)"), id="pick-auto")]
            for index, port in enumerate(self._ports):
                suffix = "   ← current" if port == self._current else ""
                items.append(ListItem(Label(port + suffix), id=f"pick-{index}"))
            yield ListView(*items, id="port-list")
            yield Label("Enter selects · Esc cancels", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#port-list", ListView).focus()

    @on(ListView.Selected)
    def _picked(self, event: ListView.Selected) -> None:
        event.stop()
        # Item 0 is "(auto-detect)"; the rest map back to self._ports by index.
        self.dismiss("" if event.index == 0 else self._ports[event.index - 1])

    def action_cancel(self) -> None:
        self.dismiss(None)


class PromptDialog(ModalScreen[str | None]):
    """Single-line free-text prompt (used for raw adapter commands)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", value: str = "", hint: str = "") -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._value = value
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield Input(placeholder=self._placeholder, value=self._value, id="dialog-input")
            yield Label(self._hint, classes="hint")

    def on_mount(self) -> None:
        self.query_one("#dialog-input", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    """Yes/No gate for requests that change vehicle or adapter state."""

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, question: str, detail: str = "") -> None:
        super().__init__()
        self._question = question
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Confirm", id="dialog-title")
            yield Label(self._question, id="dialog-question")
            if self._detail:
                yield Label(self._detail, classes="hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Yes, send it", variant="error", id="dialog-yes")
                yield Button("No", variant="primary", id="dialog-no")

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "dialog-yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# --------------------------------------------------------------------------- #
# the app
# --------------------------------------------------------------------------- #
class PacificaTui(App[None]):
    """Textual front end for the pacifica-obd diagnostics helpers."""

    TITLE = "pacifica-obd"
    SUB_TITLE = "2021 Chrysler Pacifica · vLinker FS"

    CSS = """
    #body { height: 1fr; }
    #sidebar {
        width: 38;
        padding: 0 1;
        border-right: solid $panel;
        scrollbar-gutter: stable;
    }
    #main { width: 1fr; }
    .panel-title { color: $accent; text-style: bold; margin-top: 1; }
    .hint { color: $text-muted; text-style: italic; height: auto; }
    .action, #btn-stop, #btn-clear, #btn-disconnect { width: 100%; }
    #status { height: 1; padding: 0 1; background: $panel; }
    #log { min-width: 20; height: 1fr; }
    /* Live values grow at the bottom when a watch is running (the table sizes
       itself to its rows). Deliberately not a tab: a hidden RichLog has no width
       and would wrap output at min_width, and stored lines never reflow. */
    #table { height: auto; max-height: 12; margin-top: 1; }
    PortPicker, PromptDialog, ConfirmDialog { align: center middle; }
    #dialog {
        width: 76;
        max-width: 94%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #port-list { height: auto; max-height: 14; }
    #dialog-question { margin: 1 0; }
    #dialog-buttons { height: auto; align-horizontal: right; }
    #dialog-buttons Button { width: auto; min-width: 16; margin-left: 2; }
    """

    BINDINGS = [
        Binding("c", "connect", "Connect"),
        Binding("p", "ports", "Port"),
        Binding("ctrl+d", "disconnect", "Disconnect"),
        Binding("i", "info", "Info"),
        Binding("m", "modules", "Modules"),
        Binding("d", "dtcs", "DTCs"),
        Binding("n", "module_info", "Module info"),
        Binding("r", "read_did", "Read DID"),
        Binding("w", "watch", "Watch"),
        Binding("t", "readiness", "Readiness"),
        Binding("f", "freeze", "Freeze"),
        Binding("x", "raw", "Raw cmd"),
        Binding("s", "stop", "Stop"),
        Binding("ctrl+l", "clear_log", "Clear", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Adapter state: only mutated on the UI thread (see _forget/_adopt).
        self._elm: Elm327 | None = None
        self._port: str | None = None
        self._prev: Snapshot = None
        self._version = ""
        # Worker coordination.
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._busy: str | None = None
        self._closed = False
        self._shutdown_done = False
        # Live-watch state (UI thread only).
        self._watching = False
        self._samples = 0
        self._log_view: RichLog | None = None
        self._table: DataTable | None = None
        self._status_view: Static | None = None

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with VerticalScroll(id="sidebar"):
                yield Static("Connection", classes="panel-title")
                yield Button("Connect", id="btn-connect", variant="success", classes="action")
                yield Button("Choose port…", id="btn-port", classes="action")
                yield Button("Disconnect", id="btn-disconnect", classes="action")
                yield Static("Bus / scan range", classes="panel-title")
                yield Select(
                    [
                        (_BUS_LABELS["can-c"], "can-c"),
                        (_BUS_LABELS["can-ihs"], "can-ihs"),
                        ("Both buses", "both"),
                    ],
                    value="can-c",
                    allow_blank=False,
                    id="bus",
                )
                yield Input(value="700-7FF", placeholder="range 700-7FF", id="range")
                yield Label("DTC/scan range (hex)", classes="hint")
                yield Switch(value=False, id="identify")
                yield Label("Identify modules (VIN / part no.)", classes="hint")
                yield Static("Module / DIDs", classes="panel-title")
                yield Input(placeholder="module 7E0 (blank = scan)", id="module")
                yield Input(placeholder="DIDs F190,F187", id="dids")
                yield Static("Live data", classes="panel-title")
                yield Input(value=",".join(DEFAULT_WATCH), id="pids")
                yield Input(value="0.5", placeholder="interval seconds", id="interval")
                yield Static("Actions", classes="panel-title")
                yield Button("Info", id="btn-info", classes="action")
                yield Button("Modules", id="btn-modules", classes="action")
                yield Button("DTCs (all modules)", id="btn-dtcs", classes="action")
                yield Button("Module info (F186-F191)", id="btn-modinfo", classes="action")
                yield Button("Read DID", id="btn-readdid", classes="action")
                yield Button("Watch live PIDs", id="btn-watch", classes="action")
                yield Button("Readiness", id="btn-readiness", classes="action")
                yield Button("Freeze frame", id="btn-freeze", classes="action")
                yield Button("Raw command…", id="btn-raw", classes="action")
                yield Button("Stop / cancel", id="btn-stop", variant="warning")
                yield Button("Clear output", id="btn-clear")
            with Vertical(id="main"):
                yield RichLog(
                    id="log", markup=False, wrap=True, min_width=20, max_lines=MAX_LOG_LINES
                )
                yield DataTable(id="table", zebra_stripes=True)
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._log_view = self.query_one("#log", RichLog)
        self._table = self.query_one("#table", DataTable)
        self._status_view = self.query_one("#status", Static)
        self._write(Text("pacifica-obd TUI — press c to connect, then pick an action.", style="bold"))
        self._line(
            "Keys: c connect · p port · i info · m modules · d DTCs · n module info · "
            "r read DID · w watch · t readiness · f freeze · x raw · s stop",
            "dim",
        )
        self._refresh_status()

    async def on_unmount(self) -> None:
        # Runs when the terminal/app goes away without action_quit. Only flags
        # workers to stop here: the port is closed by run()'s finally, i.e. off
        # the event loop, so nothing blocking happens while the UI is winding
        # down and a worker waiting in call_from_thread() can still complete.
        self._begin_shutdown()

    # -- thread marshalling ------------------------------------------------
    def _post(self, callback: Callable[..., object], *args: object) -> object:
        """Run `callback` on the UI thread and return its result.

        Returns None once the app is shutting down (the UI is gone). Anything
        else is re-raised so a failing callback is reported by the worker
        instead of silently leaving the UI half-updated.
        """
        if self._closed:
            return None
        try:
            return self.call_from_thread(callback, *args)
        except RuntimeError:  # "App is not running" / loop closed during exit
            if self._closed:
                return None
            raise

    # -- output helpers (UI thread) ---------------------------------------
    def _write(self, renderable: object) -> None:
        if self._log_view is not None:
            self._log_view.write(renderable)

    def _line(self, message: str, style: str = "") -> None:
        self._write(Text(message, style=style))

    def _section(self, title: str) -> None:
        heading = Text()
        heading.append("\n" + title + " ", style="bold cyan")
        heading.append("─" * max(0, 58 - len(title)), style="cyan")
        self._write(heading)

    def _log_err(self, message: str) -> None:
        self._write(Text(message, style="bold red"))
        self.notify(message, severity="error", markup=False, timeout=8)

    def _log_warn(self, message: str) -> None:
        self._write(Text(message, style="yellow"))
        self.notify(message, severity="warning", markup=False, timeout=6)

    def _write_table(self, title: str, columns: list[str], rows: list[tuple[str, ...]]) -> None:
        if not rows:
            self._line(f"{title}: nothing to show.", "dim")
            return
        table = Table(title=title, title_justify="left", header_style="bold cyan")
        for column in columns:
            table.add_column(column)
        for row in rows:
            table.add_row(*row)
        self._write(table)

    # -- status bar --------------------------------------------------------
    def _refresh_status(self) -> None:
        if self._status_view is None:
            return
        if self._elm is None:
            state, style = "disconnected", "bold red"
        elif self._busy:
            state, style = f"busy: {self._busy}", "bold yellow"
        else:
            state, style = "connected", "bold green"
        bus = str(self.query_one("#bus", Select).value)
        if bus not in _BUS_LABELS:  # defensive: the Select should never be blank
            bus = "can-c"
        if bus == "both":
            bus_text = "both (STP 33/53)"
        else:
            bus_text = f"{bus} (STP {_BUS_PROTOCOL[bus]})"
        text = Text()
        text.append("port ", style="dim")
        text.append(self._port or "—", style="bold")
        text.append("   adapter ", style="dim")
        text.append(self._version or "—", style="bold")
        text.append("   bus ", style="dim")
        text.append(bus_text)
        text.append("   ")
        text.append(state, style=style)
        if self._watching:
            text.append("  ● live", style="bold magenta")
        self._status_view.update(text)

    def _set_busy(self, label: str | None) -> None:
        for widget in self.query(".action"):
            if isinstance(widget, Button):
                widget.disabled = label is not None
        self._refresh_status()

    # -- adapter handle hand-off (always called on the UI thread) ----------
    def _forget_adapter(self) -> tuple[Elm327, Snapshot] | None:
        """Detach the current handle and return it so the worker can close it."""
        old, previous = self._elm, self._prev
        self._elm = None
        self._port = None
        self._prev = None
        self._version = ""
        self._refresh_status()
        return (old, previous) if old is not None else None

    def _adopt_adapter(self, elm: Elm327, port: str, previous: Snapshot, version: str) -> bool:
        """Take ownership of a freshly opened handle. False => keep it on the worker."""
        if self._closed:
            return False
        self._elm = elm
        self._port = port
        self._prev = previous
        self._version = version or "unknown adapter"
        self._line(f"Connected to {port} ({self._version}).", "bold green")
        self._refresh_status()
        return True

    # -- worker plumbing ---------------------------------------------------
    @contextmanager
    def _exclusive(
        self, label: str, *, require_adapter: bool = True, wait: float = 0.0
    ) -> Iterator[Elm327 | None]:
        """Hold the adapter for one operation.

        Raises ``_Refused`` (before the body runs, so no lock is held) when the
        app is closing, no adapter is open, or another operation owns it. A
        positive `wait` gives a cancelled operation a moment to release the
        adapter first (used by Disconnect).
        """
        if self._closed:
            raise _Refused("app is shutting down")
        if require_adapter and self._elm is None:
            self._post(self._log_err, "Not connected — press c (or click Connect) first.")
            raise _Refused("no adapter")
        owned = self._lock.acquire(timeout=wait) if wait > 0 else self._lock.acquire(blocking=False)
        if not owned:
            self._post(
                self._log_warn,
                f"Adapter busy ({self._busy or 'another operation'}) — try again shortly.",
            )
            raise _Refused("busy")
        self._busy = label
        self._cancel.clear()
        self._post(self._set_busy, label)
        try:
            yield self._elm
        finally:
            try:
                self._busy = None
                self._post(self._set_busy, None)
            finally:
                self._lock.release()

    def _guarded(self, label: str, body: Callable[[Elm327], None]) -> None:
        """Worker entry point: hold the adapter, run `body`, report failures."""
        try:
            with self._exclusive(label) as elm:
                if elm is None:
                    return
                body(elm)
        except _Refused:
            return  # already explained to the user by _exclusive
        except Elm327Error as exc:
            self._post(self._log_err, f"{label}: {exc}")
        except Exception as exc:  # noqa: BLE001 - never let a driver error kill the UI
            self._post(self._log_err, f"{label}: {type(exc).__name__}: {exc}")

    def _warn(self, message: str) -> None:
        """Warning line for worker threads (marshalled to the UI thread)."""
        self._post(self._line, "warning: " + message, "yellow")

    def _start(self, label: str, body: Callable[[Elm327], None]) -> None:
        if self._closed:
            return
        self.run_worker(
            partial(self._guarded, label, body),
            name=label,
            description=label,
            group="adapter",
            thread=True,
            exit_on_error=False,
        )

    # -- actions: connection ----------------------------------------------
    def action_connect(self) -> None:
        """Open the adapter on the remembered port, or auto-detect one."""
        if self._elm is not None:
            self.notify(f"Already connected to {self._port}.", markup=False)
            return
        self._start_connect(self._port)

    def action_ports(self) -> None:
        """Choose a serial port from a picker."""
        self._start_port_scan()

    def action_disconnect(self) -> None:
        """Restore the protocol snapshot, close the port and stop any watch."""
        if self._elm is None:
            self.notify("Not connected.", severity="warning", markup=False)
            return
        self._cancel.set()  # ask a running scan/watch to wind down
        self.run_worker(
            self._worker_disconnect,
            name="Disconnect",
            group="adapter",
            thread=True,
            exit_on_error=False,
        )

    def _start_port_scan(self) -> None:
        if self._closed:
            return
        self.run_worker(
            self._worker_scan_ports,
            name="Ports",
            group="scan",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _worker_scan_ports(self) -> None:
        """List serial ports off the event loop, then open the picker.

        Uses passive enumeration: obd.scan_serial() would open (and pulse the
        control lines of) the very adapter this app may already own.
        """
        self._post(self._push_port_picker, _list_serial_ports())

    def _push_port_picker(self, ports: list[str]) -> None:
        if isinstance(self.screen, PortPicker):  # already open
            return
        self.push_screen(PortPicker(ports, self._port), self._port_chosen)

    def _port_chosen(self, selection: str | None) -> None:
        if selection is None:  # cancelled
            return
        self._start_connect(selection or None)

    def _start_connect(self, port: str | None) -> None:
        if self._closed:
            return
        self.run_worker(
            partial(self._worker_connect, port),
            name="Connect",
            description="Connect",
            group="adapter",
            thread=True,
            exit_on_error=False,
        )

    def _worker_connect(self, port: str | None) -> None:
        """Open the adapter and hand the handle to the UI thread."""
        try:
            with self._exclusive("Connect", require_adapter=False):
                stale = self._post(self._forget_adapter)
                if isinstance(stale, tuple):
                    self._post(self._line, "Closing the previous adapter handle…", "dim")
                    _release_handle(*stale)
                resolved = find_port(port)
                if resolved is None:
                    self._post(
                        self._log_err,
                        "No serial port found. Plug in the vLinker FS or choose one with p.",
                    )
                    return
                self._post(self._line, f"Opening {resolved} @ {DEFAULT_BAUD} baud…", "dim")
                elm, previous, version = _open_adapter(resolved)
                if self._post(self._adopt_adapter, elm, resolved, previous, version) is not True:
                    # The UI thread never took ownership (quit during connect):
                    # the handle is still ours, so close it here.
                    _release_handle(elm, previous)
        except _Refused:
            return  # busy / closing: _exclusive already told the user
        except Elm327Error as exc:
            self._post(self._log_err, f"Connect failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._post(self._log_err, f"Connect failed: {type(exc).__name__}: {exc}")

    def _worker_disconnect(self) -> None:
        try:
            # Give a just-cancelled watch/scan a moment to release the adapter so
            # a single Disconnect press is enough when something is running.
            with self._exclusive("Disconnect", require_adapter=False, wait=2.0):
                stale = self._post(self._forget_adapter)
                if not isinstance(stale, tuple):
                    self._post(self._line, "Not connected.", "dim")
                    return
                _release_handle(*stale)
                self._post(self._line, "Adapter closed; protocol snapshot restored.", "dim")
        except _Refused:
            return
        except Exception as exc:  # noqa: BLE001
            self._post(self._log_err, f"Disconnect: {type(exc).__name__}: {exc}")

    # -- actions: reads ----------------------------------------------------
    def action_info(self) -> None:
        """Adapter identity, protocol, voltage and VIN."""
        self._start("Info", self._do_info)

    def action_modules(self) -> None:
        """Probe a CAN bus for UDS-responding modules."""
        rng = _parse_range(self._input("range"))
        if rng is None:
            self._log_err("Scan range must look like 700-7FF (hex, 11-bit, start <= end).")
            return
        start, end = rng
        identify = self.query_one("#identify", Switch).value
        bus = self._bus()
        self._start("Modules", partial(self._do_modules, bus, start, end, identify))

    def action_dtcs(self) -> None:
        """Read DTCs (UDS 0x19 02) from every responding module."""
        bus = self._bus()
        ok, module = self._module_input(required=False)
        if not ok:
            return
        rng = _parse_range(self._input("range"))
        if module is None and rng is None:
            self._log_err("Scan range must look like 700-7FF (hex, 11-bit, start <= end).")
            return
        start, end = rng if rng is not None else (0x700, 0x7FF)
        self._start("DTCs", partial(self._do_dtcs, bus, module, start, end))

    def action_module_info(self) -> None:
        """Read the F186..F191 identification block for one module or a range."""
        bus = self._bus()
        ok, module = self._module_input(required=False)
        if not ok:
            return
        rng = _parse_range(self._input("range"))
        if module is None and rng is None:
            self._log_err("Scan range must look like 700-7FF (hex, 11-bit, start <= end).")
            return
        start, end = rng if rng is not None else (0x700, 0x7FF)
        self._start("Module info", partial(self._do_module_info, bus, module, start, end))

    def action_read_did(self) -> None:
        """Read one or more DIDs (UDS 0x22) from a module."""
        bus = self._bus()
        ok, module = self._module_input(required=True)
        if not ok or module is None:
            return
        dids = _parse_dids(self._input("dids"))
        if dids is None:
            self._log_err("DIDs must be 4 hex digits each, e.g. F190,F187.")
            return
        self._start("Read DID", partial(self._do_read_did, bus, module, dids))

    def action_readiness(self) -> None:
        """Readiness / I-M monitors (mode 01 PID 01)."""
        bus = self._buses_for_pcm()
        self._start("Readiness", partial(self._do_readiness, bus))

    def action_freeze(self) -> None:
        """Show the freeze-frame snapshot stored behind a DTC (mode 02)."""
        bus = self._buses_for_pcm()
        self._start("Freeze frame", partial(self._do_freeze, bus))

    def action_raw(self) -> None:
        """Send one raw AT/ST/OBD/UDS command to the adapter."""
        if isinstance(self.screen, PromptDialog):  # already open
            return
        self.push_screen(
            PromptDialog(
                "Raw adapter command",
                placeholder="ATSH 7E0",
                hint="Sent verbatim. Requests that change vehicle or adapter state ask for "
                "confirmation first.",
            ),
            self._raw_command,
        )

    def _raw_command(self, command: str | None) -> None:
        if not command:
            return
        parsed = _parse_raw_command(command)
        if parsed is None:
            self._log_err("Command must be printable ASCII, at most 64 characters.")
            return
        risk = _raw_risk(parsed)
        if risk is None:
            self._start("Raw", partial(self._do_raw, parsed))
            return
        # Destructive request (DTC clear, reset, write, EEPROM): require a yes/no.
        if isinstance(self.screen, ConfirmDialog):
            return
        self.push_screen(
            ConfirmDialog(
                f"Send {parsed} to the vehicle?",
                f"{risk}. This cannot be undone from the TUI and the Security Gateway may "
                "block it anyway.",
            ),
            lambda confirmed: self._raw_confirmed(parsed, bool(confirmed)),
        )

    def _raw_confirmed(self, command: str, confirmed: bool) -> None:
        if not confirmed:
            self._line(f"Raw command {command} cancelled.", "yellow")
            return
        self._line(f"Confirmed; sending {command}.", "yellow")
        self._start("Raw", partial(self._do_raw, command))

    def action_watch(self) -> None:
        """Start/stop live PID polling."""
        if self._watching:
            self._cancel.set()
            self.notify("Stopping the live watch…", markup=False)
            return
        pids = _parse_pids(self._input("pids"))
        if pids is None:
            self._log_err(f"PID list must be a subset of: {', '.join(_PID_NAMES)}.")
            return
        interval = _parse_interval(self._input("interval"))
        if interval is None:
            self._log_err("Interval must be a number between 0.2 and 10 seconds.")
            return
        self._start("Watch", partial(self._do_watch, pids, interval, self._buses_for_pcm()))

    def action_stop(self) -> None:
        """Ask the running operation to stop after its current request."""
        if self._busy is None:
            self.notify("Nothing is running.", severity="warning", markup=False)
            return
        self._cancel.set()
        self.notify(f"Stopping {self._busy} after the current request…", markup=False)

    def action_clear_log(self) -> None:
        if self._log_view is not None:
            self._log_view.clear()

    # -- action helpers (UI thread) ---------------------------------------
    def _input(self, widget_id: str) -> str:
        return self.query_one("#" + widget_id, Input).value

    def _bus(self) -> str:
        return str(self.query_one("#bus", Select).value)

    def _buses_for_pcm(self) -> str:
        """Readiness/freeze frame are mode 01/02 reads aimed at the PCM (CAN-C)."""
        bus = self._bus()
        return "can-c" if bus == "both" else bus

    def _module_input(self, *, required: bool) -> tuple[bool, str | None]:
        """Validate the Module field.

        Returns ``(ok, module)``; ``(True, None)`` means "blank, so scan the
        range". An *invalid* value returns ``(False, None)`` so callers abort
        instead of silently scanning every module.
        """
        text = self._input("module").strip()
        if not text:
            if required:
                self._log_err("Enter a module request ID (e.g. 7E0) in the Module field.")
                return False, None
            return True, None
        module = _parse_module_id(text)
        if module is None:
            self._log_err(f"Module {text!r} is not an 11-bit hex CAN ID (000-7FF).")
            return False, None
        return True, module

    # -- button dispatch ---------------------------------------------------
    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        handler = {
            "btn-connect": self.action_connect,
            "btn-port": self.action_ports,
            "btn-disconnect": self.action_disconnect,
            "btn-info": self.action_info,
            "btn-modules": self.action_modules,
            "btn-dtcs": self.action_dtcs,
            "btn-modinfo": self.action_module_info,
            "btn-readdid": self.action_read_did,
            "btn-watch": self.action_watch,
            "btn-readiness": self.action_readiness,
            "btn-freeze": self.action_freeze,
            "btn-raw": self.action_raw,
            "btn-stop": self.action_stop,
            "btn-clear": self.action_clear_log,
        }.get(event.button.id or "")
        if handler is not None:
            handler()

    @on(Select.Changed)
    def _bus_changed(self, event: Select.Changed) -> None:
        self._refresh_status()

    # ===================================================================== #
    # operation bodies — these run on a worker thread with the adapter lock
    # and must only touch the UI through self._post(...)
    # ===================================================================== #
    def _do_info(self, elm: Elm327) -> None:
        bus = self._buses_for_pcm()
        self._post(self._section, "Adapter / vehicle info")
        _prepare_bus(elm, bus, self._warn)
        wait = _probe_wait()
        adapter = (elm.cmd("ATI") or [""])[0].strip()
        voltage = _parse_voltage(elm.cmd("ATRV"))
        protocol = _protocol_label(current_protocol(elm))
        vin = None
        elm.cmd("ATSH 7E0")
        payload = _payload_after(elm.cmd("0902", wait=wait), "4902")
        if payload is not None:
            vin = _decode_vin("4902" + payload.hex().upper())
        rows = [
            ("Adapter", adapter or self._version or "—"),
            ("Protocol now", protocol),
            ("Voltage (ATRV)", f"{voltage:.1f} V" if voltage is not None else "—"),
            ("VIN (09 02)", vin or "not reported"),
        ]
        self._post(self._write_table, "Connection info", ["Item", "Value"], rows)

    def _do_modules(self, bus: str, start: int, end: int, identify: bool, elm: Elm327) -> None:
        wait = _probe_wait()
        self._post(self._section, f"Module discovery — {_bus_label(bus)}")
        rows: list[tuple[str, ...]] = []
        for name in _buses_for(bus):
            if self._cancel.is_set():
                self._post(self._line, "Cancelled.", "yellow")
                break
            _prepare_bus(elm, name, self._warn)
            self._post(self._line, f"Probing {_BUS_LABELS[name]} {start:03X}-{end:03X}…", "dim")
            for entry in _probe_modules(elm, start, end, "1003", wait, self._cancel, self._warn):
                if self._cancel.is_set():
                    self._post(self._line, "Cancelled.", "yellow")
                    break
                rid, response_id = entry["request_id"], entry["response_id"]
                self._post(self._line, f"  {rid} → {response_id} ({entry['kind']})", "dim")
                extra = ""
                if identify and entry["kind"] != "other":
                    elm.cmd("ATSH " + rid)
                    vin_record = _read_did(elm, 0xF190, wait, expect_header=response_id)
                    part_record = _read_did(elm, 0xF187, wait, expect_header=response_id)
                    extra = _did_ascii(vin_record) or _did_ascii(part_record) or ""
                rows.append((name, rid, response_id, entry["kind"], extra))
        self._post(
            self._write_table,
            f"Modules on {_bus_label(bus)}",
            ["Bus", "Request", "Response", "Type", "VIN / part"],
            rows,
        )

    def _do_dtcs(self, bus: str, module: str | None, start: int, end: int, elm: Elm327) -> None:
        wait = _probe_wait()
        self._post(self._section, f"All-module DTCs (UDS 0x19) — {_bus_label(bus)}")
        rows: list[tuple[str, ...]] = []
        total = 0
        modules = 0
        incomplete = 0
        for name in _buses_for(bus):
            if self._cancel.is_set():
                self._post(self._line, "Cancelled.", "yellow")
                break
            _prepare_bus(elm, name, self._warn)
            if module:
                targets = [{"request_id": module, "expect": None}]
            else:
                self._post(self._line, f"Probing {_BUS_LABELS[name]} {start:03X}-{end:03X}…", "dim")
                targets = []
                for entry in _probe_modules(
                    elm, start, end, "1003", wait, self._cancel, self._warn
                ):
                    if self._cancel.is_set():
                        break
                    targets.append(
                        {"request_id": entry["request_id"], "expect": entry["response_id"]}
                    )
                    self._post(self._line, f"  {entry['request_id']} responded", "dim")
            for target in targets:
                if self._cancel.is_set():
                    break
                rid = target["request_id"]
                expect = target["expect"]
                modules += 1
                if not _acknowledged(elm.cmd("ATSH " + rid)):
                    incomplete += 1
                    rows.append((name, rid, "—", "header rejected"))
                    continue
                elm.cmd("1003", wait=wait)  # extended session, as in the CLI
                header, uds = _parse_uds_frame(elm.cmd("1902FF", wait=wait))
                if header is None or uds is None:
                    incomplete += 1
                    rows.append((name, rid, "—", "no response"))
                    continue
                if expect is not None and header != expect:
                    # Someone else answered: do not attribute its DTCs to `rid`.
                    incomplete += 1
                    rows.append((name, rid, "—", f"wrong responder ({header})"))
                    continue
                status = _classify_dtc_response(uds)
                if status != "ok":
                    incomplete += 1
                    note = {
                        "negative": "negative response",
                        "malformed": "malformed response",
                        "unexpected": "unexpected response",
                    }.get(status, status)
                    rows.append((name, rid, "—", note))
                    continue
                dtcs = _parse_dtcs(uds)
                if not dtcs:
                    rows.append((name, rid, "—", "no DTCs"))
                    continue
                first = True
                for dtc in dtcs:
                    total += 1
                    rows.append(
                        (
                            name if first else "",
                            rid if first else "",
                            f"{dtc['code']}-{dtc['failure_type']}",
                            ", ".join(dtc["flags"]),
                        )
                    )
                    first = False
        self._post(
            self._write_table,
            "All-module DTCs (UDS 0x19)",
            ["Bus", "Module", "Code", "Flags"],
            rows,
        )
        summary = f"{total} DTC(s) across {modules} module(s)"
        if incomplete:
            summary += f"; {incomplete} did not answer cleanly"
        self._post(self._line, summary + ".", "bold red" if total else "green")

    def _do_module_info(
        self, bus: str, module: str | None, start: int, end: int, elm: Elm327
    ) -> None:
        wait = _probe_wait()
        self._post(self._section, f"Module identification (F186-F191) — {_bus_label(bus)}")
        rows: list[tuple[str, ...]] = []
        for name in _buses_for(bus):
            if self._cancel.is_set():
                self._post(self._line, "Cancelled.", "yellow")
                break
            _prepare_bus(elm, name, self._warn)
            if module:
                request_ids: list[tuple[str, str | None]] = [(module, None)]
            else:
                request_ids = []
                for entry in _probe_modules(
                    elm, start, end, "1003", wait, self._cancel, self._warn
                ):
                    if self._cancel.is_set():
                        break
                    request_ids.append((entry["request_id"], entry["response_id"]))
                    self._post(self._line, f"  {entry['request_id']} responded", "dim")
            for rid, probed_from in request_ids:
                if self._cancel.is_set():
                    break
                # Drop a filter left over from the previous module, and make sure
                # the adapter really did acknowledge the new header/filter: a
                # rejected ATCRA/ATSH silently returns another module's data.
                if not _acknowledged(elm.cmd("ATCRA")):
                    self._warn("'ATCRA' was not acknowledged; a stale ECU filter may mask modules")
                if not _acknowledged(elm.cmd("ATSH " + rid)):
                    self._warn(f"'ATSH {rid}' was not acknowledged; skipping this module")
                    continue
                learned = _enter_session(elm, rid, wait)
                self._post(self._line, f"{rid}: {'in session' if learned else 'no response'}", "dim")
                if probed_from and learned and learned != probed_from:
                    # The probe answered from `probed_from`; if the session
                    # answered from somewhere else, trust the earlier reply so a
                    # wrong ECU cannot masquerade as this module.
                    self._warn(f"{rid}: session answered from {learned}, expected {probed_from}")
                expect = probed_from or learned
                first = True
                for did in _INFO_DIDS:
                    if self._cancel.is_set():
                        break
                    record = _read_did(elm, did, wait, expect_header=expect)
                    rows.append(
                        (
                            name if first else "",
                            rid if first else "",
                            record.get("name") or record["did"],
                            _did_value(record),
                            _did_ascii(record),
                        )
                    )
                    first = False
        self._post(
            self._write_table,
            "Module identification",
            ["Bus", "Module", "Item", "Hex", "ASCII"],
            rows,
        )

    def _do_read_did(self, bus: str, module: str, dids: list[int], elm: Elm327) -> None:
        wait = _probe_wait()
        self._post(self._section, f"Read DID — module {module} on {_bus_label(bus)}")
        rows: list[tuple[str, ...]] = []
        for name in _buses_for(bus):
            if self._cancel.is_set():
                self._post(self._line, "Cancelled.", "yellow")
                break
            _prepare_bus(elm, name, self._warn)
            if not _acknowledged(elm.cmd("ATSH " + module)):
                self._warn(f"'ATSH {module}' was not acknowledged; skipped on this bus")
                continue
            expect = _enter_session(elm, module, wait)
            for did in dids:
                if self._cancel.is_set():
                    break
                record = _read_did(elm, did, wait, expect_header=expect)
                rows.append(
                    (
                        name,
                        record["did"],
                        record.get("name") or "",
                        _did_value(record),
                        _did_ascii(record),
                    )
                )
        self._post(
            self._write_table,
            f"DIDs from module {module}",
            ["Bus", "DID", "Name", "Hex", "ASCII"],
            rows,
        )

    def _do_readiness(self, bus: str, elm: Elm327) -> None:
        wait = _probe_wait()
        self._post(self._section, f"Readiness monitors — {_BUS_LABELS[bus]}")
        _prepare_bus(elm, bus, self._warn)
        payload = _payload_after(elm.cmd("0101", wait=wait), "4101")
        if payload is None or len(payload) < 4:
            self._post(self._line, "No readiness data (vehicle off or unsupported).", "yellow")
            return
        data = _decode_readiness(payload)
        available = [m for m in data["monitors"] if m[1]]
        incomplete = [m[0] for m in available if not m[2]]
        ready = (not data["mil"]) and data["dtc_count"] == 0 and not incomplete
        headline = Text()
        headline.append(f"ignition {data['ignition']}  ", style="dim")
        headline.append(
            "MIL ON  " if data["mil"] else "MIL off  ",
            style="bold red" if data["mil"] else "bold green",
        )
        headline.append(f"{data['dtc_count']} stored DTC(s)  ")
        headline.append(
            "READY" if ready else "NOT READY",
            style="bold green" if ready else "bold yellow",
        )
        self._post(self._write, headline)
        rows = [
            (_MONITOR_LABELS.get(name, name), "complete" if complete else "incomplete")
            for name, _, complete in available
        ]
        self._post(self._write_table, "Monitors", ["Monitor", "State"], rows)

    def _do_freeze(self, bus: str, elm: Elm327) -> None:
        wait = _probe_wait()
        self._post(self._section, f"Freeze frame (mode 02) — {_BUS_LABELS[bus]}")
        _prepare_bus(elm, bus, self._warn)
        payload = _payload_after(elm.cmd("0102", wait=wait), "4102")
        dtc = _decode_obd_dtc(payload) if payload is not None and len(payload) >= 2 else None
        if dtc is None:
            self._post(self._line, "No freeze-frame data stored (mode 01 PID 02 is empty).", "yellow")
            self._post(
                self._line, "A snapshot is only captured when an emissions fault sets.", "dim"
            )
            return
        self._post(self._line, f"Freeze-frame DTC: {dtc}", "bold red")
        rows: list[tuple[str, ...]] = []
        for pid, label, unit, decoder in _FREEZE_PIDS:
            if self._cancel.is_set():
                self._post(self._line, "Cancelled.", "yellow")
                break
            payload = _payload_after(elm.cmd("02%02X00" % pid, wait=wait), "42%02X" % pid)
            if payload is None or len(payload) < 2:
                continue
            try:
                value = decoder(payload[1:])  # drop the frame-number byte
            except (IndexError, TypeError, ZeroDivisionError):
                continue
            rows.append(("0x%02X" % pid, label, f"{value:g} {unit}"))
        self._post(
            self._write_table,
            f"Freeze frame behind {dtc} (frame 0)",
            ["PID", "Parameter", "Value"],
            rows,
        )

    def _do_raw(self, command: str, elm: Elm327) -> None:
        self._post(self._section, f"Raw command — {command}")
        lines = elm.cmd(command, wait=_probe_wait())
        if not lines:
            self._post(self._line, "(no response)", "dim")
            return
        for line in lines:
            self._post(self._line, line)

    def _do_watch(self, pids: list[str], interval: float, bus: str, elm: Elm327) -> None:
        wait = _probe_wait()
        self._post(self._watch_started, pids)
        self._post(
            self._line,
            f"Polling {', '.join(pids)} every {interval:g}s on {_BUS_LABELS[bus]} — "
            "press w or Stop to end.",
            "dim",
        )
        try:
            _prepare_bus(elm, bus, self._warn)
            while not self._cancel.is_set():
                started = time.monotonic()
                values: dict[str, tuple[str, str]] = {}
                for pid in pids:
                    if self._cancel.is_set():
                        break
                    if pid == _VOLTAGE_KEY:
                        volts = _parse_voltage(elm.cmd("ATRV"))
                        values[pid] = (f"{volts:.1f}" if volts is not None else "—", "V")
                        continue
                    request, needed, decoder, unit = _PIDS[pid]
                    payload = _payload_after(elm.cmd(request, wait=wait), "41" + request[2:])
                    if payload is None or len(payload) < needed:
                        values[pid] = ("—", unit)
                        continue
                    try:
                        values[pid] = (f"{decoder(payload):g}", unit)
                    except (IndexError, ValueError, ZeroDivisionError):
                        values[pid] = ("err", unit)
                self._post(self._watch_sample, values)
                deadline = started + interval
                while not self._cancel.is_set() and time.monotonic() < deadline:
                    time.sleep(0.05)  # keep Stop responsive
        finally:
            self._post(self._watch_finished)

    # -- live-watch UI callbacks (UI thread) -------------------------------
    def _watch_started(self, pids: list[str]) -> None:
        self._watching = True
        self._samples = 0
        table = self._table
        if table is not None:
            table.clear(columns=True)
            table.add_columns(("PID", "pid"), ("Value", "value"), ("Unit", "unit"))
            for pid in pids:
                table.add_row(pid, "…", "", key=pid)
        self.query_one("#btn-watch", Button).label = "Stop live watch"
        self._refresh_status()

    def _watch_sample(self, values: dict[str, tuple[str, str]]) -> None:
        table = self._table
        if table is not None:
            for pid, (value, unit) in values.items():
                try:
                    table.update_cell(pid, "value", value)
                    table.update_cell(pid, "unit", unit)
                except Exception:  # noqa: BLE001 - row was rebuilt under us
                    pass
        self._samples += 1
        self._refresh_status()

    def _watch_finished(self) -> None:
        self._watching = False
        self.query_one("#btn-watch", Button).label = "Watch live PIDs"
        self._refresh_status()
        self._line(f"Live watch stopped after {self._samples} sample(s).", "dim")

    # -- lifecycle ---------------------------------------------------------
    def _begin_shutdown(self) -> None:
        """Tell every worker to stop. Non-blocking, safe on the event loop.

        Sets the flags that make ``_exclusive`` refuse new work and that make
        ``_post`` a no-op, so a worker blocked in ``call_from_thread`` is never
        left waiting on a UI that has already gone away.
        """
        self._closed = True
        self._cancel.set()

    def shutdown(self) -> None:
        """Restore the protocol snapshot and close the port.

        Called from ``run()``'s ``finally`` (after the event loop has stopped)
        and idempotent. The snapshot is only restored while this thread owns the
        adapter; if a worker refuses to give up the lock we close the port
        instead of issuing commands on a bus we do not own.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._begin_shutdown()
        owned = self._lock.acquire(timeout=3.0)
        elm, previous = self._elm, self._prev
        self._elm, self._port, self._prev, self._version = None, None, None, ""
        if elm is None:
            if owned:
                self._lock.release()
            return
        try:
            if owned:
                _release_handle(elm, previous)
            else:  # pragma: no cover - a wedged worker; closing unblocks its read
                elm.close()
        finally:
            if owned:
                self._lock.release()


def _bus_label(bus: str) -> str:
    return _BUS_LABELS.get(bus, bus)


def run() -> None:
    """Launch the interactive TUI."""
    app = PacificaTui()
    try:
        app.run()
    finally:
        # Runs on normal exit, Ctrl-C and unhandled exceptions alike.
        app.shutdown()


if __name__ == "__main__":
    run()
