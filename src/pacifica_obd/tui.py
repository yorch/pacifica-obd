"""Interactive terminal UI (Textual) for pacifica-obd.

A keyboard- and mouse-driven front end for the whole ``pacifica-obd`` command
surface:

* adapter reads — ports, info, VIN, generic DTCs (modes 03/07/0A), live
  supported-PID dump, watch, readiness, freeze frame, scan, raw commands,
  module discovery, all-module UDS DTCs (0x19), ``read-did`` (0x22) and the
  F186-F191 identification block;
* long-running adapter work — :func:`capture` (modules + DTCs + ID block) and
  :func:`monitor` (UDS DIDs polled into the local database);
* offline local-database views — ``vehicles``, ``history``, ``show``,
  ``module-map`` and the pure-Python ``seedkey-brute`` recovery;
* guarded writes — ``security`` (0x27 seed/key), ``write-did`` (0x2E) and
  ``routine`` (0x31). These never auto-send: a dialog states the exact request
  and the Security-Gateway/irreversibility warning, and only an explicit "yes"
  reaches the adapter.

Design notes
------------
* The serial transport is synchronous and blocking, so every operation runs on
  a Textual **thread worker**; results are handed back to the UI thread with
  ``App.call_from_thread``. Nothing blocking ever runs on the event loop.
* A single non-blocking lock means only one operation owns the adapter at a
  time; a second request is refused with a visible message instead of
  interleaving two conversations on the same CAN bus. Offline operations take
  the same lock so the busy state stays consistent (they just ignore the
  handle).
* Lifecycle: the protocol is snapshotted with ``current_protocol()`` when the
  port opens and restored with ``restore_protocol()`` before ``close()`` on
  disconnect / app exit. That is deliberately the raw transport rather than
  :class:`pacifica_obd.adapter.ElmAdapter` (the CLI's one-shot wrapper): the TUI
  must be able to close a port *without* commanding a bus another worker still
  owns.
* Exceptions never reach the UI as a traceback: worker bodies are wrapped and
  reported as log lines plus a notification.
* The TUI stays on the raw transport for the generic OBD commands the CLI runs
  through python-OBD (``dtc``, ``live``, ``scan``): handing the port back and
  forth would close/reopen it, and ``cli.connect()`` is free to ``sys.exit``.

Protocol/parsing logic is never re-implemented here: the UDS layer
(:mod:`pacifica_obd.uds`), the adapter helpers (:mod:`pacifica_obd.adapter`),
the optional external ECU database reader (:mod:`pacifica_obd.database`), the
seed/key tools (:mod:`pacifica_obd.seedkey`) and the store (:mod:`pacifica_obd.store`)
do the work; this module only drives them and renders the results.

Launch with ``pacifica-obd tui`` (or ``python -c 'from pacifica_obd.tui import run; run()'``).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from obd.codes import DTC as _DTC_TEXT
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

from pacifica_obd.adapter import (
    AdapterError,
    configure_bus,
    current_protocol,
    find_port,
    must_ok,
    reset_flow_control,
    restore_protocol,
)
from pacifica_obd.cli import (
    DEFAULT_BAUD,
    DEFAULT_WATCH,
    _FREEZE_PIDS,
    _WRITE_WARNING,
)
from pacifica_obd.database import EcuDatabase, default_db
from pacifica_obd.elm import Elm327, Elm327Error
from pacifica_obd.profiles import (
    addresses as module_addresses,
    looks_like_vin,
    response_id,
    year_from_vin,
)
from pacifica_obd.seedkey import SeedKeyError, load_provider
from pacifica_obd.store import Store, logging_enabled
from pacifica_obd.uds import (
    BUS_PROTOCOL as _BUS_PROTOCOL,
    INFO_DIDS as _INFO_DIDS,
    classify_dtc_response as _classify_dtc_response,
    did_ascii as _did_ascii,
    did_value as _did_value,
    enter_session as _uds_enter_session,
    parse_dtcs as _parse_dtcs,
    parse_uds_frame as _parse_uds_frame,
    provider_context,
    read_did as _read_did,
    routine_control,
    security_access,
    write_did as uds_write_did,
)

__all__ = ["run", "PacificaTui"]


class _Refused(Exception):
    """An operation could not start: shutting down, no adapter, or adapter busy.

    Raised by ``PacificaTui._exclusive`` so a worker can never accidentally run
    its body without owning the adapter.
    """


class _SearchAborted(Exception):
    """Raised by the seed/key search wrapper when cancelled or out of budget."""


# Bound on the (uncancellable, in-library) seed/key search so a slow plugin
# cannot hold the adapter lock forever; Stop also trips it via the cancel event.
BRUTE_BUDGET_SECONDS = 30.0


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
# name -> (mode-01 PID code, payload bytes required, decoder, unit). The payload
# is everything after the "41 <pid>" echo of the reply.
Pid = tuple[int, int, Callable[[bytes], float], str]


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
    "RPM": (0x0C, 2, _rpm, "rpm"),
    "SPEED": (0x0D, 1, _u8, "km/h"),
    "COOLANT_TEMP": (0x05, 1, _temp, "°C"),
    "ENGINE_LOAD": (0x04, 1, _percent, "%"),
    "THROTTLE_POS": (0x11, 1, _percent, "%"),
    "INTAKE_TEMP": (0x0F, 1, _temp, "°C"),
    "MAF": (0x10, 2, _maf, "g/s"),
    "FUEL_LEVEL": (0x2F, 1, _percent, "%"),
}
# ATRV is an adapter command, not a PID; handled separately in the poll loop.
_VOLTAGE_KEY = "ELM_VOLTAGE"
_PID_NAMES = [*_PIDS, _VOLTAGE_KEY]
# PID code -> decoder, so the "live" dump can decode any PID we know how to.
_PID_BY_CODE: dict[int, tuple[str, int, Callable[[bytes], float], str]] = {
    code: (name, needed, decoder, unit) for name, (code, needed, decoder, unit) in _PIDS.items()
}

# Generic (non-UDS) trouble-code services and their 0x40 replies.
_GENERIC_DTC_MODES = (
    ("03", "Stored (mode 03)"),
    ("07", "Pending (mode 07)"),
    ("0A", "Permanent (mode 0A)"),
)

# A 4-byte seed/key secret is 2**32 candidates: minutes-to-hours of uncancellable
# Python in one thread. The TUI caps it (and says so); the CLI can still do it.
MAX_BRUTE_WIDTH = 3

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


def _decode_generic_dtcs(payload: bytes) -> list[str]:
    """Decode a mode 03/07/0A payload: a count byte then two bytes per code.

    Mirrors python-OBD's ``dtc`` decoder, which drops the mode byte *and* the
    reported DTC count before pairing the remaining bytes (``43 01 03 00`` is one
    code, P0300 — not P0103).
    """
    codes: list[str] = []
    data = payload[1:]  # payload[0] is the number of codes that follow
    for index in range(1, len(data), 2):  # pair up, ignoring an odd trailing byte
        code = _decode_obd_dtc(data[index - 1:index + 1])
        if code is not None:  # 00 00 padding
            codes.append(code)
    return codes


def _dtc_description(code: str) -> str:
    """Human description from python-OBD's code table (same text as the CLI)."""
    return str(_DTC_TEXT.get(code, ""))


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


def _read_vin(elm, wait: float) -> str | None:
    """Read the VIN with generic mode 09 02 (works without a UDS session)."""
    elm.cmd("ATSH 7E0")
    payload = _payload_after(elm.cmd("0902", wait=wait), "4902")
    if payload is None:
        return None
    return _decode_vin("4902" + payload.hex().upper())


def _read_uds_vin(
    elm,
    wait: float,
    cancel: threading.Event,
    warn: Callable[[str], None] | None = None,
) -> str | None:
    """VIN over UDS F190 from the known module addresses (mirrors the CLI).

    Some vehicles (e.g. the Pacifica Hybrid) do not serve mode 09 02 at all but
    do return F190 from their powertrain/body modules.
    """
    for bus in ("can-c", "can-ihs"):
        if cancel.is_set():
            return None
        try:
            _prepare_bus(elm, bus, warn)
        except (AdapterError, Elm327Error):
            continue
        for _name, request, response in module_addresses(bus, powertrain="phev"):
            if cancel.is_set():
                return None
            rid = "%03X" % request
            if not _address(elm, rid, warn):
                continue
            learned = _enter_session(elm, rid, wait)
            expect = ("%03X" % response if response else None) or learned
            record = _read_did(elm, 0xF190, wait, expect_header=expect)
            if record["status"] == "ok" and looks_like_vin(record.get("ascii")):
                return record["ascii"]
    return None


# Recovery runs in a child interpreter: a plugin is arbitrary code and the
# library's search loop cannot be interrupted from another thread, so the only
# reliable Stop/budget guarantee is to terminate the process.
_BRUTE_CHILD = r"""
import json, sys
from pacifica_obd.seedkey import find_secrets, load_algorithm, parse_pairs

algorithm_path, pairs_text, width = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    algorithm = load_algorithm(algorithm_path)
    pairs = parse_pairs(pairs_text, width=width)
    matches = find_secrets(algorithm, pairs, width=width)
except BaseException as exc:  # a broken plugin must not look like "no match"
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
    raise SystemExit(0)
print(json.dumps({"matches": matches}))
"""


def _run_brute_subprocess(
    algorithm_path: str, pairs_text: str, width: int, cancel: threading.Event, budget: float
) -> dict:
    """Run seed/key recovery in a killable child process.

    Returns ``{"matches": [...]}`` or ``{"error": "..."}``; raises
    ``_SearchAborted`` when Stop or the budget ends it first.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _BRUTE_CHILD, algorithm_path, pairs_text, str(width)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + budget

    def stop() -> None:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
            proc.kill()
            proc.wait(timeout=2.0)

    while True:
        if cancel.is_set() or time.monotonic() > deadline:
            stop()
            raise _SearchAborted()
        try:
            out, err = proc.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            continue
    text = (out or "").strip()
    if proc.returncode != 0:
        detail = (err or text or "the recovery process failed").strip().splitlines()
        return {"error": detail[-1] if detail else "the recovery process failed"}
    try:
        return json.loads(text.splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (err or text or "unreadable result from the recovery process").strip()}


def _enter_session(elm, rid: str | None = None, wait: float = 0.5) -> str | None:
    """``uds.enter_session``; ISO-TP flow control stays in automatic mode.

    Reply correlation is handled by the receive filter (``ATCRA``), never by
    manual flow control — see AGENTS.md.
    """
    return _uds_enter_session(elm, rid, wait)


def _known_response_id(request_id: str) -> str | None:
    """Response ID for a request ID from the profile tables, if we know one.

    Used to attribute replies to the module we asked: without an expectation a
    stray frame from another ECU could be recorded against the wrong module.
    """
    try:
        return "%03X" % response_id(int(request_id, 16))
    except ValueError:
        return None


def _supported_pids(elm, wait: float, cancel: threading.Event) -> list[int]:
    """Decode the mode-01 supported-PID bitmaps (01 00 / 20 / 40 / 60)."""
    found: list[int] = []
    base = 0x00
    for _block in range(4):
        if cancel.is_set():
            break
        payload = _payload_after(elm.cmd("01%02X" % base, wait=wait), "41%02X" % base)
        if payload is None or len(payload) < 4:
            break
        for index, byte in enumerate(payload[:4]):
            for bit in range(8):
                if byte & (0x80 >> bit):
                    found.append(base + index * 8 + bit + 1)
        next_base = base + 0x20
        if next_base not in found:  # the continuation bit was clear
            break
        base = next_base
    return found


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
    """Pin the STN preset for `bus`, aim requests at the PCM and clear filters.

    ``configure_bus`` (reused from the CLI) leaves the CAN receive address
    filter alone; a previous session read leaves it narrowed to one ECU, which
    would silently hide every other module, so clear it here.

    The request header is required to be acknowledged: if a stale header from an
    earlier raw command survived, mode 01/02 reads would go to another ECU and we
    would present that module's data as the PCM's. The filter clear is only
    warned about, because a surviving filter shows up as missing modules rather
    than as someone else's data.

    Automatic ISO-TP flow control is restored here too: UDS reads set it manually
    per responder, and the CLI can leave it set (it exits), but a long-lived TUI
    would otherwise send later mode 01/09 flow-control frames to a stale address.
    """
    protocol = configure_bus(elm, bus, timeout_ms)
    reset_flow_control(elm)
    must_ok(elm, "ATSH 7E0")
    if not _acknowledged(elm.cmd("ATCRA")):
        _warn(warn, "'ATCRA' was not acknowledged; a stale ECU filter may hide modules")
    return protocol


def _acknowledged(lines: list[str]) -> bool:
    """True if any response line is the adapter's 'OK'."""
    return any("OK" in line.upper() for line in lines)


def _warn(warn: Callable[[str], None] | None, message: str) -> None:
    if warn is not None:
        warn(message)


def _probe_modules_multi(
    elm: Elm327,
    start: int,
    end: int,
    probes: Sequence[str],
    wait: float,
    cancel: threading.Event,
    warn: Callable[[str], None] | None = None,
    receive_id: Callable[[int], int] | None = response_id,
) -> Iterator[dict]:
    """Cancellable multi-service probe, mirroring ``uds.probe_ids_multi``.

    Many modules reject ``10 03`` but answer ``22 F190`` (and vice versa), so the
    CLI tries several services; this is the cancellable equivalent (the library
    generator cannot be interrupted and only yields responders, so a Stop press
    while scanning an empty range would otherwise have no effect). ``receive_id``
    applies the same receive-address fix as the CLI, without which the adapter
    silently drops ``-0x280`` and CAN-IHS answers. An ID whose ``ATSH`` is
    rejected is skipped rather than misattributed to the previous module.
    """
    known: set[int] = set()
    ignored: set[int] = set()
    for probe in probes:
        service = int(probe[:2], 16)
        for rid in range(start, end + 1):
            if cancel.is_set():
                return
            if rid in known:
                continue
            if not _acknowledged(elm.cmd("ATSH %03X" % rid)):
                _warn(warn, f"ATSH {rid:03X} was not acknowledged; skipping that ID")
                known.add(rid)
                continue
            mapped: str | None = None
            if receive_id is not None:
                try:
                    mapped = "%03X" % receive_id(rid)
                    elm.cmd("ATCRA " + mapped)
                except Exception:  # noqa: BLE001 - no mapping (e.g. broadcast)
                    elm.cmd("ATAR")
            header, uds = _parse_uds_frame(elm.cmd(probe, wait=wait))
            if header is None:
                continue
            if mapped is not None and header != mapped:
                # Someone else answered. Yielding this pair would outrank the
                # known mapping downstream, so ignore it and let the next probe
                # service try the same ID.
                if rid not in ignored:
                    ignored.add(rid)
                    _warn(warn, f"{rid:03X} answered from {header}, expected {mapped}; ignoring")
                continue
            if uds.startswith("%02X" % (service + 0x40)):
                kind = "positive"
            elif uds.startswith("7F"):
                kind = "negative"
            else:
                kind = "other"
            known.add(rid)
            yield {
                "request_id": "%03X" % rid,
                "response_id": header,
                "kind": kind,
                "probe": probe,
                "session_response": uds,
            }


def _address(elm: Elm327, rid: str, warn: Callable[[str], None] | None = None) -> bool:
    """Aim the adapter at one module: set the RX filter, then set the header.

    Non-standard modules answer at an address other than ``request + 8``, and the
    adapter needs the receive filter (``ATCRA``) pointed at the responder so it can
    reassemble (and flow-control) multi-frame replies. When the profile knows the
    responder we filter to it; otherwise the filter is cleared to accept any reply.
    ISO-TP flow control is deliberately left in the adapter's automatic mode:
    forcing it into user-defined mode switches the STN to an indexed frame format
    that breaks multi-frame parsing.

    Returns False if the adapter rejected the header, in which case the caller
    must skip the module — the previous one would still be selected.
    """
    mapped = _known_response_id(rid)
    reply = elm.cmd("ATCRA " + mapped) if mapped else elm.cmd("ATCRA")
    if not _acknowledged(reply):
        _warn(warn, "'ATCRA' was not acknowledged; replies may be missed")
    if not _acknowledged(elm.cmd("ATSH " + rid)):
        _warn(warn, f"'ATSH {rid}' was not acknowledged; skipping this module")
        return False
    return True


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


def _open_writable_store() -> Store | None:
    """Writable log database, or None when logging is off/unavailable."""
    if not logging_enabled():
        return None
    try:
        store = Store()
    except Exception:  # noqa: BLE001 - persistence must never break a command
        return None
    return store if store.available else None


def _open_readonly_store() -> Store | None:
    """Read-only database for the offline views; None when there is none."""
    try:
        store = Store(readonly=True)
    except Exception:  # noqa: BLE001
        return None
    return store if store.available else None


def _write_warning() -> Text:
    """The CLI's SGW/irreversibility warning, as renderable text."""
    return Text.from_markup(_WRITE_WARNING)


def _addressed_session(
    elm, bus: str, module: str, wait: float, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> tuple[str, str]:
    """Configure the bus, address `module` and require an extended session.

    Deliberately *not* ``cli._addr_and_session``: that helper leaves the receive
    filter untouched (a filter left by an earlier read would hide this module's
    session reply) and forces ISO-TP flow control into user-defined mode, which
    switches the STN to an indexed frame format that breaks multi-frame parsing
    (see AGENTS.md). The receive filter is pointed at the responder instead, and
    the ECU must answer ``5003`` — without that, a write could land on whatever
    ECU was selected by the previous command.
    """
    configure_bus(elm, bus, timeout_ms)
    reset_flow_control(elm)  # stay in the adapter's automatic mode
    rid = _parse_module_id(module) or module
    if not _address(elm, rid):
        raise AdapterError(f"could not address module {rid}; refusing")
    expected = _known_response_id(rid)
    if expected is None:
        # A guarded write has to be verifiable: without a physical response
        # address a stray ECU's 5003 could authorise it (0x7DF is broadcast and
        # cannot answer at all).
        raise AdapterError(
            f"no known response address for module {rid}; refusing this operation"
        )
    header, uds = _parse_uds_frame(elm.cmd("1003", wait=wait), prefer_header=expected)
    if header is None or not (uds or "").startswith("5003"):
        raise AdapterError(f"module {rid} did not enter an extended session; refusing")
    if expected is not None and header != expected:
        # A stray ECU answered: its session (and later its seed/ack) must not be
        # treated as this module's, or a key could be sent to the wrong address.
        raise AdapterError(f"module {rid} answered from {header}, expected {expected}; refusing")
    return rid, header


# --------------------------------------------------------------------------- #
# input validation (the UI counterpart of the CLI's argparse types)
# --------------------------------------------------------------------------- #
def _ecu_for_module(module_hex: str) -> str | None:
    """Map a CAN request id to the dataset's ECU name (``742`` -> ``IPC_PN``)."""
    from pacifica_obd import pacifica_data

    try:
        want = int(str(module_hex), 16)
    except (TypeError, ValueError):
        return None
    for row in pacifica_data.modules():
        request = row.get("request")
        if request is None:
            continue
        try:
            if int(str(request), 16) == want:
                return row.get("ecu")
        except ValueError:
            continue
    return None


def _builtin_provider(module_hex: str, level: int,
                      device_id: int | None = None,
                      variant_id: int | None = None):
    """``(provider, info)`` from the shipped selector; ``provider`` is None on refusal.

    A refusal is returned rather than raised so the caller can show the candidate
    list and send nothing — a guessed key can lock a module.
    """
    from pacifica_obd import fca_keys

    ecu = _ecu_for_module(module_hex)
    if not ecu:
        return None, {
            "status": "unmapped",
            "reason": f"{module_hex} is not in the bundled dataset",
            "candidates": [], "module": module_hex, "level": level, "ids": {},
        }
    info = fca_keys.explain(ecu, level, device_id=device_id, variant_id=variant_id)
    if info["status"] != "resolved":
        return None, info
    return fca_keys.provider_for(ecu, level, device_id=device_id, variant_id=variant_id), info


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


def _parse_path(text: str) -> str | None:
    """Expand a user-supplied file path; None when blank."""
    value = text.strip()
    return str(Path(value).expanduser()) if value else None


def _parse_hex_payload(text: str, *, allow_empty: bool = False) -> str | None:
    """Validate an even-length hex byte string."""
    value = (text or "").strip().upper().replace(" ", "")
    if not value:
        return "" if allow_empty else None
    if len(value) % 2 or not re.fullmatch(r"[0-9A-F]+", value):
        return None
    return value


def _parse_vin_field(text: str) -> str | None:
    """Normalise a VIN (any non-blank text; the caller may warn if it looks odd)."""
    value = text.strip().upper()
    return value or None


# Vehicle services that write, clear or reset state, and adapter commands that
# persist to EEPROM. Both need an explicit yes/no before they go out.
_RAW_DANGEROUS_SERVICES = {
    "04": "mode 04 clears emissions DTCs and resets readiness monitors",
    "08": "OBD mode 08 controls on-board systems (actuators, evap test)",
    "11": "UDS 0x11 resets (reboots) the addressed module",
    "14": "UDS 0x14 clears stored DTCs",
    "27": "UDS 0x27 security access can unlock write routines",
    "28": "UDS 0x28 communication control can silence a module",
    "2C": "UDS 0x2C dynamically defines a data identifier",
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


def _raw_risk(command: str, framing_off: bool = False) -> str | None:
    """Explain why `command` is not a plain read, or None if it is safe.

    ELM/ST commands tolerate spaces (``AT SP 6``), so compare the space-free
    form for those too. With auto-formatting off (``ATCAF0``) the bytes the user
    types are a raw CAN frame whose first byte is the ISO-TP PCI, so a write
    service such as ``11`` (ECU reset) hides at offset 2 of what looks like a
    harmless mode-02 read.
    """
    compact = command.upper().replace(" ", "")
    if re.fullmatch(r"[0-9A-F]+", compact) and len(compact) >= 2:
        services = [compact[0:2]]
        if len(compact) >= 4 and compact[0] in "01":  # single frame / framed payload
            services.append(compact[2:4])
        if len(compact) >= 6 and compact[0] == "1":  # ISO-TP first frame: 2 length bytes
            services.append(compact[4:6])
        for service in services:
            reason = _RAW_DANGEROUS_SERVICES.get(service)
            if reason is not None:
                return reason
        if framing_off:
            return "CAN auto-formatting is off, so these bytes are a raw CAN frame"
        return None
    if compact.startswith(_RAW_PERSISTENT_AT_PREFIXES):
        return "these adapter commands write the protocol choice to EEPROM"
    if compact.startswith("ATCAF0"):
        return "this turns off CAN auto-formatting, so later hex commands become raw frames"
    return None


# --------------------------------------------------------------------------- #
# modal screens
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Field:
    """One entry in a :class:`FormDialog`."""

    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    kind: str = "text"  # "text" | "switch" | "select"
    options: tuple[tuple[str, str], ...] = ()
    hint: str = ""


class FormDialog(ModalScreen[dict | None]):
    """Collect parameters for the parameterised actions.

    ``validate`` receives the raw values and returns an error string (shown
    in the dialog, which stays open) or None to accept.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Submit", show=False),
    ]

    def __init__(
        self,
        title: str,
        fields: Sequence[Field],
        hint: str = "",
        validate: Callable[[dict], str | None] | None = None,
        submit_label: str = "Submit",
    ) -> None:
        super().__init__()
        self._title = title
        self._fields = list(fields)
        self._hint = hint
        self._validate = validate
        self._submit_label = submit_label

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label(self._title, id="dialog-title")
            for spec in self._fields:
                if spec.kind == "switch":
                    with Horizontal(classes="field-row"):
                        yield Switch(value=spec.value == "1", id=f"field-{spec.name}")
                        yield Label(spec.label, classes="field-label")
                else:
                    yield Label(spec.label, classes="field-label")
                    if spec.kind == "select":
                        yield Select(
                            list(spec.options),
                            value=spec.value,
                            allow_blank=False,
                            compact=True,
                            id=f"field-{spec.name}",
                        )
                    else:
                        yield Input(
                            value=spec.value,
                            placeholder=spec.placeholder,
                            compact=True,
                            id=f"field-{spec.name}",
                        )
                if spec.hint:
                    yield Label(spec.hint, classes="hint")
            yield Label("", id="dialog-error")
            if self._hint:
                yield Label(self._hint, classes="hint")
            with Horizontal(id="dialog-buttons"):
                yield Button(self._submit_label, variant="primary", id="dialog-submit")
                yield Button("Cancel", id="dialog-cancel")

    def on_mount(self) -> None:
        for widget in self.query(Input):
            widget.focus()
            break

    def values(self) -> dict:
        values: dict = {}
        for spec in self._fields:
            widget = self.query_one(f"#field-{spec.name}")
            if isinstance(widget, Switch):
                values[spec.name] = bool(widget.value)
            elif isinstance(widget, Select):
                values[spec.name] = str(widget.value)
            else:
                values[spec.name] = str(widget.value)
        return values

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "dialog-submit":
            self.action_submit()
        else:
            self.dismiss(None)

    def action_submit(self) -> None:
        values = self.values()
        if self._validate is not None:
            error = self._validate(values)
            if error:
                # A Text (not markup) keeps user input out of the markup parser.
                self.query_one("#dialog-error", Label).update(Text(error, style="bold red"))
                return
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Every action and its key, built from the app's own bindings."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close", show=False)]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("pacifica-obd — actions and keys", id="dialog-title")
            yield Static(self._bindings_table(), id="help-table")
            yield Label(
                "Reads are always safe. The writes (security key, 0x2E, 0x31) ask for "
                "confirmation and show the exact request first; the Security Gateway on "
                "2018+ vehicles usually blocks them without a bypass.",
                classes="hint",
            )
            yield Label("Esc closes · Ctrl+P opens the command palette", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#dialog", VerticalScroll).focus()

    def _bindings_table(self) -> Table:
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column("key", style="bold cyan", no_wrap=True)
        table.add_column("action")
        seen: set[str] = set()
        for binding in PacificaTui.BINDINGS:
            if not isinstance(binding, Binding) or not binding.description:
                continue
            if binding.action in seen:
                continue
            seen.add(binding.action)
            table.add_row(binding.key, binding.description)
        return table

    def action_close(self) -> None:
        self.dismiss(None)


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
                yield Label("(no candidate ports; is the adapter plugged in?)", classes="hint")
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
    /* Compact action rows: the sidebar carries the whole command surface. */
    Button.narrow {
        width: 100%;
        height: 1;
        min-height: 1;
        border: none;
        margin: 0;
        padding: 0 1;
    }
    #btn-stop, #btn-clear { width: 100%; }
    #status { height: 1; padding: 0 1; background: $panel; }
    #log { min-width: 20; height: 1fr; }
    /* Live values grow at the bottom when a watch/monitor runs (the table sizes
       itself to its rows). Deliberately not a tab: a hidden RichLog has no width
       and would wrap output at min_width, and stored lines never reflow. */
    #table { height: auto; max-height: 12; margin-top: 1; }
    PortPicker, PromptDialog, ConfirmDialog, FormDialog, HelpScreen {
        align: center middle;
    }
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
    #dialog-error { height: auto; }
    .field-label { color: $text-muted; margin-top: 1; height: auto; }
    .field-row { height: auto; }
    #dialog-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #dialog-buttons Button { width: auto; min-width: 16; margin-left: 2; }
    #help-table { height: auto; }
    """

    BINDINGS = [
        # connection
        Binding("c", "connect", "Connect"),
        Binding("p", "ports", "Choose port"),
        Binding("ctrl+d", "disconnect", "Disconnect", show=False),
        # adapter reads
        Binding("i", "info", "Info"),
        Binding("v", "vin", "VIN"),
        Binding("l", "live", "Live PIDs"),
        Binding("g", "generic_dtc", "OBD DTCs"),
        Binding("m", "modules", "Modules"),
        Binding("d", "dtcs", "UDS DTCs"),
        Binding("n", "module_info", "Module info", show=False),
        Binding("r", "read_did", "Read DID"),
        Binding("t", "readiness", "Readiness", show=False),
        Binding("f", "freeze", "Freeze frame", show=False),
        Binding("a", "scan", "Scan"),
        Binding("x", "raw", "Raw command"),
        # live data + long running
        Binding("w", "watch", "Watch PIDs"),
        Binding("o", "monitor", "Monitor DIDs", show=False),
        Binding("C", "capture", "Capture", show=False),
        # offline / local database
        Binding("M", "module_map", "Module map", show=False),
        Binding("V", "vehicles", "Vehicles", show=False),
        Binding("H", "history", "History", show=False),
        Binding("S", "show", "Show VIN", show=False),
        Binding("b", "seedkey", "Seed→key brute", show=False),
        # guarded writes
        Binding("e", "security", "Security 0x27", show=False),
        Binding("W", "write_did", "Write DID 0x2E", show=False),
        Binding("R", "routine", "Routine 0x31", show=False),
        # housekeeping
        Binding("s", "stop", "Stop"),
        Binding("ctrl+l", "clear_log", "Clear", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("f1", "help", "Help", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Adapter state: only mutated on the UI thread (see _forget/_adopt).
        self._elm: Elm327 | None = None
        self._port: str | None = None
        self._prev: Snapshot = None
        self._version = ""
        # True once a raw ATCAF0 has turned automatic CAN formatting off, which
        # makes later hex commands raw frames (see _raw_risk).
        self._raw_framing_off = False
        # Worker coordination.
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._busy: str | None = None
        self._closed = False
        self._shutdown_done = False
        # Live table state (UI thread only).
        self._live = False
        self._live_label = ""
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
                yield Button("Connect", id="btn-connect", variant="success", classes="narrow")
                yield Button("Choose port…", id="btn-port", classes="narrow")
                yield Button("Disconnect", id="btn-disconnect", classes="narrow")
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
                yield Static("Module / DIDs / vehicle", classes="panel-title")
                yield Input(placeholder="module 7E0 (blank = scan)", id="module")
                yield Input(placeholder="DIDs F190,F187", id="dids")
                yield Input(placeholder="VIN (show / tag stored data)", id="vin")
                yield Static("Live data", classes="panel-title")
                yield Input(value=",".join(DEFAULT_WATCH), id="pids")
                yield Input(value="0.5", placeholder="interval seconds", id="interval")
                yield Static("Reads", classes="panel-title")
                yield Button("Info", id="btn-info", classes="narrow")
                yield Button("VIN", id="btn-vin", classes="narrow")
                yield Button("Live PIDs (all supported)", id="btn-live", classes="narrow")
                yield Button("OBD DTCs (03/07/0A)", id="btn-generic-dtc", classes="narrow")
                yield Button("Modules (discovery)", id="btn-modules", classes="narrow")
                yield Button("UDS DTCs (all modules)", id="btn-dtcs", classes="narrow")
                yield Button("Module info (F186-F191)", id="btn-modinfo", classes="narrow")
                yield Button("Read DID", id="btn-readdid", classes="narrow")
                yield Button("Readiness", id="btn-readiness", classes="narrow")
                yield Button("Freeze frame", id="btn-freeze", classes="narrow")
                yield Button("Scan (VIN + DTCs + PIDs)", id="btn-scan", classes="narrow")
                yield Button("Raw command…", id="btn-raw", classes="narrow")
                yield Static("Live / long-running", classes="panel-title")
                yield Button("Watch live PIDs", id="btn-watch", classes="narrow")
                yield Button("Monitor UDS DIDs…", id="btn-monitor", classes="narrow")
                yield Button("Capture snapshot…", id="btn-capture", classes="narrow")
                yield Static("Local database / offline", classes="panel-title")
                yield Button("Module map", id="btn-modmap", classes="narrow")
                yield Button("Vehicles", id="btn-vehicles", classes="narrow")
                yield Button("History", id="btn-history", classes="narrow")
                yield Button("Show stored VIN", id="btn-show", classes="narrow")
                yield Button("Seed→key brute…", id="btn-seedkey", classes="narrow")
                yield Static("Guarded writes", classes="panel-title")
                yield Button("Security access 0x27…", id="btn-security", variant="error",
                             classes="narrow")
                yield Button("Write DID 0x2E…", id="btn-write-did", variant="error",
                             classes="narrow")
                yield Button("Routine 0x31…", id="btn-routine", variant="error", classes="narrow")
                yield Static("—", classes="panel-title")
                yield Button("Stop / cancel", id="btn-stop", variant="warning")
                yield Button("Clear output", id="btn-clear")
                yield Button("Help / keys", id="btn-help", classes="narrow")
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
        self._write(
            Text("pacifica-obd TUI — press c to connect, then pick an action.", style="bold")
        )
        self._line(
            "Keys: c connect · p port · i info · v VIN · l live · g OBD DTCs · m modules · "
            "d UDS DTCs · r read DID · w watch · o monitor · C capture · x raw · ? help",
            "dim",
        )
        self._line(
            "Offline (no adapter): M module map · V vehicles · H history · S show · "
            "b seed→key.  Guarded writes: e security · W write DID · R routine.",
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
        try:
            bus = str(self.query_one("#bus", Select).value)
        except Exception:  # noqa: BLE001 - the DOM is gone during teardown
            return
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
        if self._live:
            text.append("  ● live", style="bold magenta")
        self._status_view.update(text)

    def _set_busy(self, label: str | None) -> None:
        try:
            # Only the narrow action buttons: Stop must stay clickable, and the
            # status bar keeps showing what is running.
            widgets = list(self.query("#sidebar .narrow"))
        except Exception:  # noqa: BLE001 - the DOM is gone during teardown
            return
        for widget in widgets:
            if isinstance(widget, Button):
                widget.disabled = label is not None
        self._refresh_status()

    # -- adapter handle hand-off (always called on the UI thread) ----------
    def _forget_adapter(self) -> tuple[Elm327, Snapshot] | None:
        """Detach the current handle and return it so the worker can close it.

        Deliberately does no rendering: a drawing failure here would lose the
        handle (shutdown would no longer see it either), leaking an open port.
        """
        old, previous = self._elm, self._prev
        self._elm = None
        self._port = None
        self._prev = None
        self._version = ""
        self._raw_framing_off = False
        return (old, previous) if old is not None else None

    def _adopt_adapter(self, elm: Elm327, port: str, previous: Snapshot, version: str) -> bool:
        """Take ownership of a freshly opened handle. False => keep it on the worker."""
        if self._closed:
            return False
        self._elm = elm
        self._port = port
        self._prev = previous
        self._version = version or "unknown adapter"
        self._raw_framing_off = False
        try:
            self._line(f"Connected to {port} ({self._version}).", "bold green")
            self._refresh_status()
        except Exception:  # noqa: BLE001 - never lose an adopted handle to a render error
            pass
        return True

    def _post_quiet(self, callback: Callable[..., object], *args: object) -> None:
        """Cosmetic UI update from a worker: never let it abort the caller.

        Used for status/button repaints, which must not be able to interrupt a
        cleanup path (closing the port) or leave the busy flag stuck.
        """
        try:
            self._post(callback, *args)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

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
        try:
            # Clear a stale Stop, then re-check under the lock: a shutdown that
            # began while we were waiting must not be undone, or a polling worker
            # would never see the cancellation it depends on.
            self._cancel.clear()
            if self._closed:
                raise _Refused("app is shutting down")
            self._busy = label
            self._post_quiet(self._set_busy, label)
            try:
                yield self._elm
            finally:
                self._busy = None
                self._post_quiet(self._set_busy, None)
        finally:
            self._lock.release()

    def _guarded(
        self, label: str, body: Callable[[Elm327 | None], None], require_adapter: bool = True
    ) -> None:
        """Worker entry point: hold the adapter, run `body`, report failures.

        Offline operations pass ``require_adapter=False`` and ignore the handle;
        they still run under the same lock so the busy state stays truthful.
        """
        try:
            with self._exclusive(label, require_adapter=require_adapter) as elm:
                body(elm)
        except _Refused:
            return  # already explained to the user by _exclusive
        except AdapterError as exc:
            self._post(self._log_err, f"{label}: {exc}")
        except Elm327Error as exc:
            self._post(self._log_err, f"{label}: {exc}")
        except Exception as exc:  # noqa: BLE001 - never let a driver error kill the UI
            self._post(self._log_err, f"{label}: {type(exc).__name__}: {exc}")

    def _warn(self, message: str) -> None:
        """Warning line for worker threads (marshalled to the UI thread)."""
        self._post(self._line, "warning: " + message, "yellow")

    def _start(
        self, label: str, body: Callable[[Elm327 | None], None], *, require_adapter: bool = True
    ) -> None:
        if self._closed:
            return
        self.run_worker(
            partial(self._guarded, label, body, require_adapter),
            name=label,
            description=label,
            group="adapter",
            thread=True,
            exit_on_error=False,
        )

    def _start_offline(self, label: str, body: Callable[[], None]) -> None:
        """Run work that needs no adapter (local DB, plugin files)."""
        self._start(label, lambda _elm: body(), require_adapter=False)

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
                self._post_quiet(self._refresh_status)
                if isinstance(stale, tuple):
                    # Release before rendering anything: a drawing failure here
                    # must never skip the close.
                    _release_handle(*stale)
                    self._post(self._line, "Closed the previous adapter handle.", "dim")
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
                self._post_quiet(self._refresh_status)
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
        self._start("Info", partial(self._do_info, self._single_bus()))

    def action_vin(self) -> None:
        """Read the VIN (mode 09 02)."""
        self._start("VIN", partial(self._do_vin, self._single_bus()))

    def action_live(self) -> None:
        """Dump the current value of every supported OBD-II PID."""
        self._start("Live", partial(self._do_live, self._single_bus()))

    def action_generic_dtc(self) -> None:
        """Stored/pending/permanent codes via plain OBD-II (modes 03/07/0A)."""
        self._start("OBD DTCs", partial(self._do_generic_dtc, self._single_bus()))

    def action_scan(self) -> None:
        """One-shot VIN + stored DTCs + supported-PID count."""
        self._start("Scan", partial(self._do_scan, self._single_bus()))

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
        bus = self._single_bus()
        self._start("Readiness", partial(self._do_readiness, bus))

    def action_freeze(self) -> None:
        """Show the freeze-frame snapshot stored behind a DTC (mode 02)."""
        bus = self._single_bus()
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
        risk = _raw_risk(parsed, self._raw_framing_off)
        if risk is None:
            self._dispatch_raw(parsed)
            return
        # Destructive request (DTC clear, reset, write, EEPROM, framing change):
        # require an explicit yes/no first.
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
        self._dispatch_raw(command)

    def _dispatch_raw(self, command: str) -> None:
        """Send a raw command; the worker updates the framing flag from the reply."""
        self._start("Raw", partial(self._do_raw, command))

    def _set_framing_off(self, framing_off: bool) -> None:
        """Record whether the adapter is in raw-CAN mode (UI thread)."""
        self._raw_framing_off = framing_off

    def action_watch(self) -> None:
        """Start/stop live PID polling."""
        if self._live:
            self._cancel.set()
            self.notify("Stopping the live view…", markup=False)
            return
        pids = _parse_pids(self._input("pids"))
        if pids is None:
            self._log_err(f"PID list must be a subset of: {', '.join(_PID_NAMES)}.")
            return
        interval = _parse_interval(self._input("interval"))
        if interval is None:
            self._log_err("Interval must be a number between 0.2 and 10 seconds.")
            return
        self._start("Watch", partial(self._do_watch, pids, interval, self._single_bus()))

    def action_monitor(self) -> None:
        """Start/stop polling UDS DIDs on modules (stored as samples)."""
        if self._live:
            self._cancel.set()
            self.notify("Stopping the monitor…", markup=False)
            return
        ok, module = self._module_input(required=False)
        if not ok:
            return
        dids = _parse_dids(self._input("dids"))
        if dids is None:
            self._log_err("Monitor DIDs must be 4 hex digits each, e.g. F190,F187.")
            return
        interval = _parse_interval(self._input("interval"))
        if interval is None:
            self._log_err("Interval must be a number between 0.2 and 10 seconds.")
            return
        rng = _parse_range(self._input("range"))
        if module is None and rng is None:
            self._log_err("Scan range must look like 700-7FF (hex, 11-bit, start <= end).")
            return
        start, end = rng if rng is not None else (0x700, 0x7FF)
        vin = _parse_vin_field(self._input("vin"))
        self._start(
            "Monitor",
            partial(self._do_monitor, self._single_bus(), module, dids, interval, start, end, vin),
        )

    def action_capture(self) -> None:
        """Full snapshot (modules + DTCs + ID block) stored in the local DB."""
        bus = self._bus()
        rng = _parse_range(self._input("range"))
        if rng is None:
            self._log_err("Scan range must look like 700-7FF (hex, 11-bit, start <= end).")
            return
        start, end = rng
        vin = _parse_vin_field(self._input("vin"))
        if vin is not None and not looks_like_vin(vin):
            self._log_warn(f"{vin!r} does not look like a 17-character VIN; tagging anyway.")
        self._start("Capture", partial(self._do_capture, bus, start, end, vin))

    # -- offline / local database -----------------------------------------
    def action_module_map(self) -> None:
        """Known module addresses (built-in profiles + optional ECU database)."""
        self.push_screen(
            PromptDialog(
                "External ECU database path (optional)",
                # default_db() only builds a Path: no filesystem or SQLite work
                # happens on the event loop just to show the placeholder.
                placeholder=str(default_db() or ""),
                hint="Blank or missing file: the built-in profile map is still shown.",
            ),
            self._module_map_path,
        )

    def _module_map_path(self, path: str | None) -> None:
        parsed = _parse_path(path) if path else None
        self._start_offline("Module map", partial(self._do_module_map, parsed))

    def action_vehicles(self) -> None:
        """Vehicles with stored records (no adapter needed)."""
        self._start_offline("Vehicles", self._do_vehicles)

    def action_history(self) -> None:
        """Stored vehicles and recent sessions (no adapter needed)."""
        self._start_offline("History", self._do_history)

    def action_show(self) -> None:
        """Everything stored for the Vehicle VIN field (no adapter needed)."""
        vin = _parse_vin_field(self._input("vin"))
        if vin is None:
            self._log_err("Enter the VIN to show in the Vehicle / VIN field.")
            return
        self._start_offline("Show", partial(self._do_show, vin))

    def action_seedkey(self) -> None:
        """Recover a short per-ECU secret from observed seed/key pairs."""
        self._open_form(
            FormDialog(
                "Seed→key recovery",
                [
                    Field("pairs", "Seed:key pairs (hex)", placeholder="1234:ABCD,5678:EF01",
                          hint="Comma-separated observations of the same ECU."),
                    Field("algorithm", "Algorithm file", placeholder="~/my_algorithm.py",
                          hint="Python file exposing algorithm(seed, secret) -> key."),
                    Field("width", "Secret width (bytes)", value="2",
                          hint=f"1..{MAX_BRUTE_WIDTH}; width 3 takes a few seconds. "
                               "Use the CLI for width 4 (it can run for hours)."),
                ],
                validate=self._validate_seedkey,
            ),
            self._seedkey_run,
        )

    def _validate_seedkey(self, values: dict) -> str | None:
        pairs = str(values.get("pairs", "")).strip()
        if not pairs:
            return "Give at least one SEED:KEY observation."
        algorithm = _parse_path(str(values.get("algorithm", "")))
        if algorithm is None:
            return "Point at a Python file exposing algorithm(seed, secret) -> key."
        try:
            width = int(str(values.get("width", "2")).strip() or "2")
        except ValueError:
            return "Width must be a number (bytes)."
        if not 1 <= width <= MAX_BRUTE_WIDTH:
            return (
                f"Width must be 1..{MAX_BRUTE_WIDTH} in the TUI. A 4-byte secret is "
                f"2**32 candidates: run 'pacifica-obd seedkey-brute --width 4' instead."
            )
        return None

    def _seedkey_run(self, values: dict | None) -> None:
        if not values:
            return
        self._start_offline(
            "Seed→key",
            partial(
                self._do_seedkey,
                str(values.get("pairs", "")),
                _parse_path(str(values.get("algorithm", ""))) or "",
                int(str(values.get("width", "2")).strip() or "2"),
            ),
        )

    # -- guarded writes ----------------------------------------------------
    def action_security(self) -> None:
        """UDS 0x27: dump a seed, optionally send a key from a provider file."""
        self._open_form(
            FormDialog(
                "Security access (UDS 0x27)",
                [
                    Field("module", "Module request ID", placeholder="7E0",
                          hint="The ECU must answer an extended session or we refuse."),
                    Field("level", "Security level (odd)", value="1"),
                    Field("provider", "Key provider file (optional)",
                          placeholder="~/my_provider.py",
                          hint="Python file exposing key_for(seed, **ctx) -> int. Leave blank "
                               "to use the built-in PowerNet selector."),
                    Field("device-id", "Device ID (optional)",
                          hint="A module that pins several device IDs needs this (see --explain)."),
                    Field("variant", "Variant ID (optional, hex or dec)",
                          hint="Disambiguates modules the selector keys on the variant."),
                    Field("unlock", "Send the key (unlock)", value="0", kind="switch"),
                ],
                hint="Dumping a seed is a read. Sending a key is a write and will ask again.",
                validate=self._validate_security,
                submit_label="Continue",
            ),
            self._security_review,
        )

    def _validate_security(self, values: dict) -> str | None:
        if _parse_module_id(str(values.get("module", ""))) is None:
            return "Module must be an 11-bit hex request ID (e.g. 7E0)."
        try:
            level = int(str(values.get("level", "1")).strip() or "1")
        except ValueError:
            return "Level must be a number."
        if not 1 <= level <= 0x7D or level % 2 == 0:
            return "Level must be an odd number in 1..125 (0x7D)."
        ids = self._security_ids(values)
        if ids is None:
            return "Device ID and variant must be numbers (or blank)."
        if not bool(values.get("unlock")):
            return None
        if _parse_path(str(values.get("provider", ""))) is not None:
            return None  # an explicit provider always wins
        module = _parse_module_id(str(values.get("module", ""))) or ""
        provider, info = _builtin_provider(module, level, **ids)
        if provider is not None:
            return None
        if info.get("candidates"):
            names = ", ".join(c["algorithm"] for c in info["candidates"][:3])
            return (f"Built-in selector {info['status']}: supply a device ID / variant, "
                    f"or a provider file. Candidates: {names}")
        return f"Built-in selector {info['status']}: {info.get('reason', 'no algorithm')}."

    @staticmethod
    def _security_ids(values: dict) -> dict | None:
        """Parse the optional device ID / variant fields (None = invalid)."""
        out: dict = {"device_id": None, "variant_id": None}
        for field, key in (("device-id", "device_id"), ("variant", "variant_id")):
            text = str(values.get(field, "")).strip()
            if not text:
                continue
            try:
                out[key] = int(text, 0)
            except ValueError:
                return None
        return out

    def _security_review(self, values: dict | None) -> None:
        if not values:
            return
        # The bus is captured here, on the UI thread, so the confirmation and the
        # request it describes cannot disagree.
        bus = self._single_bus()
        module = _parse_module_id(str(values.get("module", ""))) or ""
        level = int(str(values.get("level", "1")).strip() or "1")
        provider = _parse_path(str(values.get("provider", "")))
        ids = self._security_ids(values) or {"device_id": None, "variant_id": None}
        if not bool(values.get("unlock")):
            self._start("Security", partial(self._do_security_seed, bus, module, level))
            return
        self._start(
            "Security",
            partial(self._do_security_key_request, bus, module, level, provider, **ids),
        )

    def _do_security_seed(self, bus: str, module: str, level: int, elm: Elm327) -> None:
        """Read the security seed (0x27) without sending any key."""
        wait = _probe_wait()
        self._post(
            self._section,
            f"Security access 0x27 level {level:02X} — {module} on {_bus_label(bus)}",
        )
        rid, resp = _addressed_session(elm, bus, module, wait)
        result = security_access(elm, level, seedkey=None, wait=wait, expect_header=resp)
        self._post(
            self._write_table,
            "Security seed",
            ["Item", "Value"],
            [
                ("Module", rid),
                ("Bus", _bus_label(bus)),
                ("Level", "0x%02X" % level),
                ("Status", result["status"]),
                ("Seed", result.get("seed") or "—"),
                ("Raw", result.get("raw") or "—"),
            ],
        )
        if result.get("unlocked"):
            self._post(self._line, "Already unlocked (the ECU returned a zero seed).", "green")
        elif not result.get("seed"):
            self._post(self._line, "No usable seed; nothing was sent.", "yellow")
        else:
            self._post(
                self._line,
                "Seed dumped only; no key provider was used, so the ECU stays locked.",
                "dim",
            )

    def _do_security_key_request(
        self, bus: str, module: str, level: int, provider_path: str | None, elm: Elm327,
        device_id: int | None = None, variant_id: int | None = None,
    ) -> None:
        """Read the seed and compute the key, then hand the exact request to the UI."""
        wait = _probe_wait()
        self._post(
            self._section,
            f"Security access 0x27 level {level:02X} — {module} on {_bus_label(bus)}",
        )
        seedkey = None
        if provider_path is not None:
            try:
                seedkey = load_provider(provider_path)
            except SeedKeyError as exc:
                self._post(self._log_err, str(exc))
                return
        else:
            seedkey, info = _builtin_provider(module, level, device_id=device_id,
                                              variant_id=variant_id)
            if seedkey is None:
                self._post(
                    self._line,
                    f"Built-in selector {info['status']}: "
                    f"{info.get('reason', 'no single algorithm')}",
                    "yellow",
                )
                for cand in info.get("candidates", []):
                    sels = ", ".join(f"{s['field']}={s['value']}" for s in cand["selectors"])
                    self._post(self._line, f"{cand['algorithm']}  ({sels})", "dim")
                self._post(
                    self._line,
                    "Refusing: no single algorithm could be chosen, so no key was sent.",
                    "yellow",
                )
                return
            self._post(self._line, f"Built-in algorithm: {info['algorithm']}", "dim")
        rid, resp = _addressed_session(elm, bus, module, wait)
        result = security_access(elm, level, seedkey=None, wait=wait, expect_header=resp)
        seed = result.get("seed")
        self._post(
            self._write_table,
            "Security seed",
            ["Item", "Value"],
            [
                ("Module", rid),
                ("Bus", _bus_label(bus)),
                ("Level", "0x%02X" % level),
                ("Status", result["status"]),
                ("Seed", seed or "—"),
            ],
        )
        if result.get("unlocked"):
            self._post(self._line, "Already unlocked (the ECU returned a zero seed).", "green")
            return
        if not seed or len(seed) % 2 or result["status"] not in ("ok", "no_key_provider"):
            # "no_key_provider" is normal here: the seed was requested without one.
            self._post(
                self._line,
                f"No usable seed ({result['status']}); the key was not sent.",
                "yellow",
            )
            return
        if seedkey is None:
            return
        width = len(seed) // 2
        try:
            ctx = provider_context(seedkey, level, width)
            key = seedkey(int(seed, 16), **ctx) if ctx else seedkey(int(seed, 16))
        except Exception as exc:  # noqa: BLE001 - a broken provider must never send a key
            self._post(self._log_err, f"Key provider failed: {exc}")
            return
        # The algorithm states its own key width (an 8-byte-seed module may want
        # a 4-byte key); fall back to the seed width when it does not.
        key_bytes = getattr(seedkey, "key_width", 0) or width
        if type(key) is not int or key < 0 or key >= (1 << (8 * key_bytes)):
            self._post(
                self._log_err, "The provider returned a key of the wrong width; nothing sent."
            )
            return
        self._post(self._security_confirm_key, bus, module, level, seed,
                   "%0*X" % (2 * key_bytes, key))

    def _security_confirm_key(
        self, bus: str, module: str, level: int, seed: str, key_hex: str
    ) -> None:
        """Show the exact key request (UI thread) and ask before sending it."""
        request = "27%02X%s" % (level + 1, key_hex)
        self._write(_write_warning())
        self._line(
            f"Request under review: 0x27 level {level + 1:02X} key {key_hex} for seed {seed} "
            f"on module {module} ({_bus_label(bus)}) — wire: {request}",
            "yellow",
        )
        self._open_confirm(
            f"Send {request} to module {module} on {_bus_label(bus)}?",
            "A wrong key can lock the ECU; the Security Gateway usually blocks writes on "
            "2018+ vehicles. The seed is read again first, and the key is only sent if it "
            "is unchanged.",
            lambda yes: self._start(
                "Security",
                partial(self._do_security_send_key, bus, module, level, seed, key_hex),
            )
            if yes
            else self._line("Security unlock cancelled; the ECU stays locked.", "yellow"),
        )

    def _do_security_send_key(
        self, bus: str, module: str, level: int, seed: str, key_hex: str, elm: Elm327
    ) -> None:
        """Send the reviewed key — only if the ECU still presents the same seed."""
        wait = _probe_wait()
        self._post(
            self._section,
            f"Security unlock 0x27 level {level:02X} — {module} on {_bus_label(bus)}",
        )

        def reviewed_key(current_seed: int, **_ctx) -> int | None:
            # Send the key that was shown to the user, and only for the seed they
            # saw: None makes the library stop without sending anything, so a
            # changed seed invalidates the confirmation instead of sending a key.
            if "%0*X" % (len(seed), current_seed) != seed:
                return None
            return int(key_hex, 16)

        # Send the reviewed key at the width it was reviewed at.
        reviewed_key.key_width = len(key_hex) // 2

        rid, resp = _addressed_session(elm, bus, module, wait)
        result = security_access(elm, level, seedkey=reviewed_key, wait=wait, expect_header=resp)
        self._post(
            self._write_table,
            "Security unlock result",
            ["Item", "Value"],
            [
                ("Module", rid),
                ("Level", "0x%02X" % level),
                ("Status", result["status"]),
                ("Seed now", result.get("seed") or "—"),
                ("Unlocked", "yes" if result.get("unlocked") else "no"),
                ("Raw", result.get("raw") or "—"),
            ],
        )
        if result["unlocked"]:
            self._post(self._line, "Security access granted.", "bold green")
        elif result["status"] == "no_key_provider":
            self._post(
                self._line,
                "The seed changed since the review, so no key was sent; run it again.",
                "yellow",
            )

    def action_write_did(self) -> None:
        """UDS 0x2E WriteDataByIdentifier (guarded)."""
        self._open_form(
            FormDialog(
                "Write data identifier (UDS 0x2E)",
                [
                    Field("module", "Module request ID", placeholder="7E0",
                          hint="The ECU must answer an extended session or we refuse."),
                    Field("did", "DID (4 hex)", placeholder="F190"),
                    Field("data", "Data (even-length hex)", placeholder="01"),
                ],
                hint="Writes module configuration. Irreversible from this tool.",
                validate=self._validate_write_did,
                submit_label="Review",
            ),
            self._write_did_confirm,
        )

    def _validate_write_did(self, values: dict) -> str | None:
        if _parse_module_id(str(values.get("module", ""))) is None:
            return "Module must be an 11-bit hex request ID (e.g. 7E0)."
        if _parse_dids(str(values.get("did", ""))) is None:
            return "DID must be 4 hex digits, e.g. F190."
        if _parse_hex_payload(str(values.get("data", ""))) is None:
            return "Data must be non-empty, even-length hex, e.g. 01."
        return None

    def _write_did_confirm(self, values: dict | None) -> None:
        if not values:
            return
        bus = self._single_bus()
        module = _parse_module_id(str(values.get("module", ""))) or ""
        did = _parse_dids(str(values.get("did", ""))) or [0]
        data = _parse_hex_payload(str(values.get("data", ""))) or ""
        request = f"2E{did[0]:04X}{data}"
        self._warn_write_preview(
            f"0x2E {did[0]:04X} = {data} on module {module} ({_bus_label(bus)})", request
        )
        self._open_confirm(
            f"Send {request} to module {module} on {_bus_label(bus)}?",
            "A write can change module configuration. The Security Gateway will usually "
            "block it without a bypass, and this tool cannot undo it.",
            lambda yes: self._start(
                "Write DID", partial(self._do_write_did, bus, module, did[0], data)
            )
            if yes
            else self._line("Write DID cancelled.", "yellow"),
        )

    def action_routine(self) -> None:
        """UDS 0x31 RoutineControl (guarded)."""
        self._open_form(
            FormDialog(
                "Routine control (UDS 0x31)",
                [
                    Field("module", "Module request ID", placeholder="7E0",
                          hint="The ECU must answer an extended session or we refuse."),
                    Field("rid", "Routine ID (4 hex)", placeholder="0203"),
                    Field("sub", "Sub-function", value="1", kind="select",
                          options=(("1 start", "1"), ("2 stop", "2"), ("3 request results", "3"))),
                    Field("data", "Data (optional hex)", placeholder="",
                          hint="Blank is fine; some routines take no data."),
                ],
                hint="Routines can activate actuators or calibrations.",
                validate=self._validate_routine,
                submit_label="Review",
            ),
            self._routine_confirm,
        )

    def _validate_routine(self, values: dict) -> str | None:
        if _parse_module_id(str(values.get("module", ""))) is None:
            return "Module must be an 11-bit hex request ID (e.g. 7E0)."
        if _parse_dids(str(values.get("rid", ""))) is None:
            return "Routine ID must be 4 hex digits, e.g. 0203."
        if _parse_hex_payload(str(values.get("data", "")), allow_empty=True) is None:
            return "Data must be even-length hex, or blank."
        return None

    def _routine_confirm(self, values: dict | None) -> None:
        if not values:
            return
        bus = self._single_bus()
        module = _parse_module_id(str(values.get("module", ""))) or ""
        rid = (_parse_dids(str(values.get("rid", ""))) or [0])[0]
        sub = int(str(values.get("sub", "1")))
        data = _parse_hex_payload(str(values.get("data", "")), allow_empty=True) or ""
        request = f"31{sub:02X}{rid:04X}{data}"
        self._warn_write_preview(
            f"0x31 sub {sub} routine {rid:04X} data '{data}' on module {module} "
            f"({_bus_label(bus)})",
            request,
        )
        self._open_confirm(
            f"Send {request} to module {module} on {_bus_label(bus)}?",
            "A routine can activate actuators or change calibrations. The Security Gateway "
            "will usually block it without a bypass, and this tool cannot undo it.",
            lambda yes: self._start(
                "Routine", partial(self._do_routine, bus, module, rid, sub, data)
            )
            if yes
            else self._line("Routine cancelled.", "yellow"),
        )

    def _warn_write_preview(self, description: str, request: str) -> None:
        """Log the CLI's write warning plus the exact request under review."""
        self._write(_write_warning())
        self._line(f"Request under review: {description} — wire: {request}", "yellow")

    def action_help(self) -> None:
        """Show every action and key."""
        if isinstance(self.screen, HelpScreen):
            return
        self.push_screen(HelpScreen())

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

    def _single_bus(self) -> str:
        """Bus for single-ECU work; "both" collapses to CAN-C (the PCM bus)."""
        bus = self._bus()
        return "can-c" if bus == "both" else bus

    def _open_form(
        self, dialog: FormDialog, callback: Callable[[dict | None], None]
    ) -> None:
        """Push a form once (a stray second key press must not stack dialogs)."""
        if isinstance(self.screen, FormDialog):
            return
        self.push_screen(dialog, callback)

    def _open_confirm(
        self, question: str, detail: str, callback: Callable[[bool], None]
    ) -> None:
        """Push a confirmation once."""
        if isinstance(self.screen, ConfirmDialog):
            return
        self.push_screen(ConfirmDialog(question, detail), callback)

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
            "btn-vin": self.action_vin,
            "btn-live": self.action_live,
            "btn-generic-dtc": self.action_generic_dtc,
            "btn-modules": self.action_modules,
            "btn-dtcs": self.action_dtcs,
            "btn-modinfo": self.action_module_info,
            "btn-readdid": self.action_read_did,
            "btn-readiness": self.action_readiness,
            "btn-freeze": self.action_freeze,
            "btn-scan": self.action_scan,
            "btn-raw": self.action_raw,
            "btn-watch": self.action_watch,
            "btn-monitor": self.action_monitor,
            "btn-capture": self.action_capture,
            "btn-modmap": self.action_module_map,
            "btn-vehicles": self.action_vehicles,
            "btn-history": self.action_history,
            "btn-show": self.action_show,
            "btn-seedkey": self.action_seedkey,
            "btn-security": self.action_security,
            "btn-write-did": self.action_write_did,
            "btn-routine": self.action_routine,
            "btn-stop": self.action_stop,
            "btn-clear": self.action_clear_log,
            "btn-help": self.action_help,
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
    def _do_info(self, bus: str, elm: Elm327) -> None:
        self._post(self._section, "Adapter / vehicle info")
        _prepare_bus(elm, bus, self._warn)
        wait = _probe_wait()
        adapter = (elm.cmd("ATI") or [""])[0].strip()
        voltage = _parse_voltage(elm.cmd("ATRV"))
        protocol = _protocol_label(current_protocol(elm))
        vin = _read_vin(elm, wait)
        rows = [
            ("Adapter", adapter or self._version or "—"),
            ("Protocol now", protocol),
            ("Voltage (ATRV)", f"{voltage:.1f} V" if voltage is not None else "—"),
            ("VIN (09 02)", vin or "not reported"),
        ]
        self._post(self._write_table, "Connection info", ["Item", "Value"], rows)

    def _do_vin(self, bus: str, elm: Elm327) -> None:
        self._post(self._section, f"VIN — {_bus_label(bus)}")
        _prepare_bus(elm, bus, self._warn)
        wait = _probe_wait()
        vin = _read_vin(elm, wait)
        source = "mode 09 02"
        if not looks_like_vin(vin):
            self._post(
                self._line,
                "Mode 09 02 gave no usable VIN; trying UDS F190 on the known modules…",
                "dim",
            )
            uds_vin = _read_uds_vin(elm, wait, self._cancel, self._warn)
            if uds_vin:
                vin, source = uds_vin, "UDS F190"
        if vin is None:
            self._post(
                self._line,
                "No VIN returned (mode 09 02 unserved and no UDS F190 reply).",
                "yellow",
            )
            return
        self._post(self._write, Text(f"VIN: {vin}  ({source})", style="bold green"))
        self._post(
            self._line,
            f"{len(vin)} characters; looks like a VIN: {looks_like_vin(vin)}",
            "dim",
        )

    def _do_generic_dtc(self, bus: str, elm: Elm327) -> None:
        """Stored/pending/permanent codes with plain OBD-II services."""
        wait = _probe_wait()
        self._post(self._section, f"Generic OBD-II DTCs — {_bus_label(bus)}")
        _prepare_bus(elm, bus, self._warn)
        rows: list[tuple[str, ...]] = []
        total = 0
        for mode, label in _GENERIC_DTC_MODES:
            if self._cancel.is_set():
                self._post(self._line, "Cancelled.", "yellow")
                break
            payload = _payload_after(elm.cmd(mode, wait=wait), "%02X" % (int(mode, 16) + 0x40))
            if payload is None:
                rows.append((label, "—", "no response"))
                continue
            codes = _decode_generic_dtcs(payload)
            if not codes:
                rows.append((label, "—", "none"))
                continue
            for code in codes:
                total += 1
                rows.append((label, code, _dtc_description(code)))
        self._post(
            self._write_table,
            "Generic DTCs (modes 03/07/0A)",
            ["Service", "Code", "Description"],
            rows,
        )
        self._post(self._line, f"{total} code(s) reported.", "bold red" if total else "green")

    def _do_live(self, bus: str, elm: Elm327) -> None:
        """Dump the current value of every PID the vehicle says it supports."""
        wait = _probe_wait()
        self._post(self._section, f"Live values — all supported PIDs ({_bus_label(bus)})")
        _prepare_bus(elm, bus, self._warn)
        supported = _supported_pids(elm, wait, self._cancel)
        self._post(self._line, f"{len(supported)} supported PID(s); querying…", "dim")
        rows: list[tuple[str, ...]] = []
        voltage = _parse_voltage(elm.cmd("ATRV"))
        if voltage is not None:
            rows.append(("ELM_VOLTAGE", "ATRV", f"{voltage:.1f}", "V"))
        for pid in supported:
            if self._cancel.is_set():
                self._post(self._line, "Cancelled.", "yellow")
                break
            request = "01%02X" % pid
            payload = _payload_after(elm.cmd(request, wait=wait), "41%02X" % pid)
            known = _PID_BY_CODE.get(pid)
            if payload is None:
                rows.append(("%02X" % pid, request, "no response", ""))
                continue
            if known is None:
                rows.append(("%02X" % pid, request, payload.hex().upper(), ""))
                continue
            name, needed, decoder, unit = known
            if len(payload) < needed:
                rows.append((name, request, payload.hex().upper(), unit))
                continue
            try:
                rows.append((name, request, f"{decoder(payload):g}", unit))
            except (IndexError, ValueError, ZeroDivisionError):
                rows.append((name, request, payload.hex().upper(), unit))
        self._post(self._write_table, "Live PID dump", ["PID", "Request", "Value", "Unit"], rows)

    def _do_scan(self, bus: str, elm: Elm327) -> None:
        """One-shot VIN + stored DTCs + supported-PID count (the CLI's scan)."""
        wait = _probe_wait()
        self._post(self._section, f"Scan — {_bus_label(bus)}")
        _prepare_bus(elm, bus, self._warn)
        vin = _read_vin(elm, wait)
        payload = _payload_after(elm.cmd("03", wait=wait), "43")
        codes = _decode_generic_dtcs(payload) if payload is not None else []
        supported = _supported_pids(elm, wait, self._cancel)
        rows = [
            ("VIN", vin or "—"),
            ("Stored DTCs (mode 03)", str(len(codes))),
            ("Supported OBD-II PIDs", str(len(supported))),
        ]
        self._post(self._write_table, "Scan summary", ["Item", "Value"], rows)
        if codes:
            self._post(
                self._write_table,
                "Stored codes",
                ["Code", "Description"],
                [(code, _dtc_description(code)) for code in codes],
            )

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
            for entry in _probe_modules_multi(
                    elm, start, end, ("1003", "22F190"), wait, self._cancel, self._warn
                ):
                if self._cancel.is_set():
                    self._post(self._line, "Cancelled.", "yellow")
                    break
                rid, response_id = entry["request_id"], entry["response_id"]
                self._post(self._line, f"  {rid} → {response_id} ({entry['kind']})", "dim")
                extra = ""
                if identify and entry["kind"] != "other":
                    _address(elm, rid, self._warn)
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
                for entry in _probe_modules_multi(
                    elm, start, end, ("1003", "22F190"), wait, self._cancel,
                    self._warn,
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
                if not _address(elm, rid, self._warn):
                    incomplete += 1
                    rows.append((name, rid, "—", "header rejected"))
                    continue
                learned = _enter_session(elm, rid, wait)  # extended session, as in the CLI
                # Prefer the independent profile mapping: a stray reply to the
                # session request must not decide who this module is.
                expect = expect or _known_response_id(rid) or learned
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
                for entry in _probe_modules_multi(
                    elm, start, end, ("1003", "22F190"), wait, self._cancel,
                    self._warn,
                ):
                    if self._cancel.is_set():
                        break
                    request_ids.append((entry["request_id"], entry["response_id"]))
                    self._post(self._line, f"  {entry['request_id']} responded", "dim")
            for rid, probed_from in request_ids:
                if self._cancel.is_set():
                    break
                if not _address(elm, rid, self._warn):
                    continue
                learned = _enter_session(elm, rid, wait)
                # The probe/table response ID is the independent expectation; what
                # the session claimed is only a fallback, so an unrelated ECU's
                # reply cannot be displayed under this module.
                expect = probed_from or _known_response_id(rid) or learned
                self._post(
                    self._line, f"{rid}: {'in session' if learned else 'no response'}", "dim"
                )
                if probed_from and learned and learned != probed_from:
                    self._warn(f"{rid}: session answered from {learned}, expected {probed_from}")
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
            if not _address(elm, module, self._warn):
                continue
            learned = _enter_session(elm, module, wait)
            # The profile mapping is independent knowledge and outranks a stray
            # reply to the session request.
            expect = _known_response_id(module) or learned
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
            self._post(
                self._line, "No freeze-frame data stored (mode 01 PID 02 is empty).", "yellow"
            )
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
        # A leftover manual flow-control header from a UDS read would silently
        # change what a raw request does, so start from automatic flow control.
        reset_flow_control(elm)
        lines = elm.cmd(command, wait=_probe_wait())
        # Track raw-CAN framing from what the adapter actually did: only an
        # acknowledged ATCAF1 restores auto-formatting, and a rejected or silent
        # ATCAF0 is assumed to have taken effect (the safe assumption).
        compact = command.upper().replace(" ", "")
        if compact == "ATCAF0":
            self._post(self._set_framing_off, True)
        elif compact == "ATCAF1":
            if _acknowledged(lines):
                self._post(self._set_framing_off, False)
            else:
                self._post(
                    self._line, "ATCAF1 was not acknowledged; still assuming raw framing.", "yellow"
                )
        if not lines:
            self._post(self._line, "(no response)", "dim")
            return
        for line in lines:
            self._post(self._line, line)

    def _do_watch(self, pids: list[str], interval: float, bus: str, elm: Elm327) -> None:
        wait = _probe_wait()
        self._post(
            self._live_start,
            "Watch",
            [("PID", "pid"), ("Value", "value"), ("Unit", "unit")],
            {pid: (pid, "…", "") for pid in pids},
        )
        self._post(
            self._line,
            f"Polling {', '.join(pids)} every {interval:g}s on {_BUS_LABELS[bus]} — "
            "press w or Stop to end.",
            "dim",
        )
        try:
            _prepare_bus(elm, bus, self._warn)
            while not self._cancel.is_set() and not self._closed:
                started = time.monotonic()
                values: dict[str, dict[str, str]] = {}
                for pid in pids:
                    if self._cancel.is_set() or self._closed:
                        break
                    if pid == _VOLTAGE_KEY:
                        volts = _parse_voltage(elm.cmd("ATRV"))
                        values[pid] = {"value": f"{volts:.1f}" if volts is not None else "—"}
                        continue
                    code, needed, decoder, unit = _PIDS[pid]
                    request = "01%02X" % code
                    payload = _payload_after(elm.cmd(request, wait=wait), "41%02X" % code)
                    if payload is None or len(payload) < needed:
                        values[pid] = {"value": "—", "unit": unit}
                        continue
                    try:
                        values[pid] = {"value": f"{decoder(payload):g}", "unit": unit}
                    except (IndexError, ValueError, ZeroDivisionError):
                        values[pid] = {"value": "err", "unit": unit}
                self._post(self._live_sample, values)
                deadline = started + interval
                while (
                    not self._cancel.is_set()
                    and not self._closed
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)  # keep Stop responsive
        finally:
            self._post(self._live_finished, "Watch")

    def _do_monitor(
        self,
        bus: str,
        module: str | None,
        dids: list[int],
        interval: float,
        start: int,
        end: int,
        vin: str | None,
        elm: Elm327,
    ) -> None:
        """Poll UDS DIDs on one or many modules, storing every sample."""
        wait = _probe_wait()
        store = _open_writable_store()
        session = (
            store.start_session("monitor", {"bus": bus, "dids": len(dids)}, self._port)
            if store is not None
            else None
        )
        self._post(self._section, f"Monitor UDS DIDs — {_bus_label(bus)}")
        if store is None:
            self._post(
                self._line,
                "Local logging is off/unavailable: samples will not be stored.",
                "yellow",
            )
        status = "ok"
        try:
            _prepare_bus(elm, bus, self._warn)
            if module is not None:
                # An explicit module still gets its independently known response
                # ID where the profile tables have one, so a stray reply from
                # another ECU cannot be stored under this module.
                pairs = [(module, _known_response_id(module))]
            else:
                self._post(self._line, f"Probing {_bus_label(bus)} {start:03X}-{end:03X}…", "dim")
                pairs = [
                    (entry["request_id"], entry["response_id"])
                    for entry in _probe_modules_multi(
                        elm, start, end, ("1003", "22F190"), wait, self._cancel,
                        self._warn,
                    )
                ]
            if not pairs:
                self._post(self._line, "No modules responded.", "yellow")
                return
            rows = {
                f"{rid}:{did:04X}": (rid, "%04X" % did, "…") for rid, _ in pairs for did in dids
            }
            self._post(
                self._live_start,
                "Monitor",
                [("Module", "module"), ("DID", "did"), ("Value", "value")],
                rows,
            )
            self._post(
                self._line,
                f"Polling {len(pairs)} module(s) x {len(dids)} DID(s) every {interval:g}s — "
                "press o or Stop to end.",
                "dim",
            )
            while not self._cancel.is_set() and not self._closed:
                started = time.monotonic()
                values: dict[str, dict[str, str]] = {}
                for rid, probed_from in pairs:
                    if self._cancel.is_set() or self._closed:
                        break
                    if not _address(elm, rid, self._warn):
                        for did in dids:
                            values[f"{rid}:{did:04X}"] = {"value": "header rejected"}
                        continue
                    learned = _enter_session(elm, rid, wait)  # sessions expire
                    expect = probed_from or _known_response_id(rid) or learned
                    if expect is None:
                        # Without a known responder we cannot attribute an answer.
                        for did in dids:
                            values[f"{rid}:{did:04X}"] = {"value": "no session"}
                        continue
                    for did in dids:
                        if self._cancel.is_set() or self._closed:
                            break
                        record = _read_did(elm, did, wait, expect_header=expect)
                        shown = record.get("hex") or _did_value(record)
                        values[f"{rid}:{did:04X}"] = {"value": shown}
                        if store is not None:
                            number = None
                            data_hex = record.get("hex")
                            if record["status"] == "ok" and data_hex and len(data_hex) <= 4:
                                try:
                                    number = int(data_hex, 16)
                                except ValueError:
                                    number = None
                            store.add_sample(
                                session, vin, bus, rid, record["did"], record.get("name"), number,
                                record.get("ascii") or record.get("hex"), None, record["status"],
                                record.get("raw"),
                            )
                self._post(self._live_sample, values)
                deadline = started + interval
                while (
                    not self._cancel.is_set()
                    and not self._closed
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
        except Exception:
            status = "error"
            raise
        finally:
            if status == "ok" and self._cancel.is_set():
                # Discovery/probing can return early on cancel; without this the
                # session would be recorded as a clean run.
                status = "cancelled"
            self._post(self._live_finished, "Monitor")
            if store is not None:
                if vin:
                    store.upsert_vehicle(vin, year=year_from_vin(vin))
                store.finish_session(session, status, vin)
                self._post(
                    self._line, f"Samples stored in {store.db_file} ({status}).", "dim"
                )
                store.close()

    # -- operation bodies: capture, offline views, guarded writes ----------
    def _do_capture(self, bus: str, start: int, end: int, vin: str | None, elm: Elm327) -> None:
        """Full snapshot: module discovery + DTCs + ID block, stored locally."""
        wait = _probe_wait()
        store = _open_writable_store()
        session = (
            store.start_session("capture", {"bus": bus, "start": start, "end": end}, self._port)
            if store is not None
            else None
        )
        self._post(self._section, f"Capture — {_bus_label(bus)}")
        if store is None:
            self._post(
                self._line, "Local logging is off/unavailable: nothing will be stored.", "yellow"
            )
        modules = dtc_count = readings = 0
        discovered = False
        status = "ok"
        try:
            for name in _buses_for(bus):
                if self._cancel.is_set():
                    self._post(self._line, "Cancelled.", "yellow")
                    break
                _prepare_bus(elm, name, self._warn)
                self._post(
                    self._line, f"Capturing {_BUS_LABELS[name]} {start:03X}-{end:03X}…", "dim"
                )
                for entry in _probe_modules_multi(
                    elm, start, end, ("1003", "22F190"), wait, self._cancel,
                    self._warn,
                ):
                    if self._cancel.is_set():
                        break
                    rid = entry["request_id"]
                    expect = entry["response_id"]
                    modules += 1
                    if not _address(elm, rid, self._warn):
                        continue
                    _enter_session(elm, rid, wait)  # enter extended session
                    header, uds = _parse_uds_frame(elm.cmd("1902FF", wait=wait))
                    dtcs: list[dict] = []
                    if header is None or uds is None:
                        dtc_status = "no_response"
                    elif header != expect:
                        dtc_status = f"wrong responder ({header})"
                    else:
                        dtc_status = _classify_dtc_response(uds)
                        if dtc_status == "ok":
                            dtcs = _parse_dtcs(uds)
                    dtc_count += len(dtcs)
                    if store is not None:
                        store.add_dtcs(session, vin, name, rid, dtcs, uds)
                        # Record how the read went, so an unavailable module is
                        # not stored as a clean "no DTCs" module.
                        store.event(
                            session, "dtc-read", rid,
                            {"status": dtc_status, "count": len(dtcs)},
                        )
                    module_vin = None
                    part = None
                    for did in _INFO_DIDS:
                        if self._cancel.is_set():
                            break
                        record = _read_did(elm, did, wait, expect_header=expect)
                        readings += 1
                        if (
                            did == 0xF190
                            and record["status"] == "ok"
                            and looks_like_vin(record.get("ascii"))
                        ):
                            module_vin = record["ascii"]
                            if vin is None:
                                vin = module_vin
                                discovered = True
                                if store is not None:
                                    store.upsert_vehicle(vin, year=year_from_vin(vin))
                                    store.backfill_vin(session, vin)
                            elif module_vin != vin:
                                self._post(
                                    self._line,
                                    f"VIN mismatch: {rid} reports {module_vin}, keeping {vin}.",
                                    "yellow",
                                )
                        elif did == 0xF187 and record["status"] == "ok":
                            part = record.get("ascii") or record.get("hex")
                        if store is not None:
                            store.add_reading(
                                session, vin, name, rid, record["did"], record.get("name"),
                                record.get("hex"), record.get("ascii"), record["status"],
                                record.get("raw"), category="module-info",
                            )
                    if store is not None:
                        store.add_module(
                            session,
                            vin,
                            name,
                            {
                                "request_id": rid,
                                "response_id": expect,
                                "kind": entry.get("kind"),
                                "vin": module_vin,
                                "part_number": part,
                            },
                        )
                    detail = (
                        f"  {rid}: {len(dtcs)} DTC(s)"
                        if dtc_status == "ok"
                        else f"  {rid}: DTCs {dtc_status}"
                    )
                    if module_vin:
                        detail += f", VIN {module_vin}"
                    if part:
                        detail += f", part {part}"
                    self._post(self._line, detail, "dim")
        except Exception:
            status = "error"
            raise
        finally:
            if status == "ok" and self._cancel.is_set():
                status = "cancelled"  # a Stop press must not look like a clean run
            if store is not None:
                if vin:
                    store.upsert_vehicle(vin, year=year_from_vin(vin))
                store.finish_session(session, status, vin)
                store.close()  # one connection per run, not one per capture
        if discovered:
            self._post(self._adopt_discovered_vin, vin)
        self._post(
            self._write_table,
            "Capture summary",
            ["Item", "Value"],
            [
                ("VIN", vin or "—"),
                ("Modules", str(modules)),
                ("DTCs", str(dtc_count)),
                ("ID readings", str(readings)),
                ("Session", status),
                ("Stored", str(store.db_file) if store is not None else "not stored"),
            ],
        )

    def _adopt_discovered_vin(self, vin: str | None) -> None:
        """Put a VIN discovered by capture into the Vehicle field (UI thread)."""
        if vin:
            self.query_one("#vin", Input).value = vin

    def _do_module_map(self, ecu_db: str | None) -> None:
        """Built-in module addresses plus an optional external ECU database map."""
        self._post(self._section, "Module map")
        rows: list[tuple[str, ...]] = []
        for bus in ("can-c", "can-ihs"):
            for name, request, response in module_addresses(bus):
                rows.append((bus, name, "%03X" % request, "%03X" % response, "profile"))
        db = EcuDatabase(path=ecu_db)
        try:
            if db.available:
                for row in db.pn_modules():
                    rows.append(
                        (
                            row.get("bus") or "",
                            row.get("name") or row.get("ecu") or "",
                            ("%03X" % row["request"]) if row.get("request") is not None else "",
                            ("%03X" % row["response"]) if row.get("response") is not None else "",
                            "ecu-db",
                        )
                    )
                self._post(self._line, f"External ECU database: {db.path}", "dim")
            else:
                self._post(
                    self._line,
                    "No external ECU database; showing the built-in map only "
                    "(set PACIFICA_OBD_ECU_DB or give a path).",
                    "dim",
                )
        finally:
            db.close()
        self._post(
            self._write_table,
            "Known modules",
            ["Bus", "Module", "Request", "Response", "Source"],
            rows,
        )

    def _do_vehicles(self) -> None:
        """Vehicles with stored records (read-only, no adapter)."""
        self._post(self._section, "Stored vehicles")
        store = _open_readonly_store()
        if store is None:
            self._post(self._line, "No local database found.", "yellow")
            return
        try:
            vehicles = store.vehicles()
            self._post(self._line, f"Database: {store.db_file}", "dim")
        finally:
            store.close()
        rows = [
            (
                vehicle.get("vin") or "",
                vehicle.get("make") or "",
                vehicle.get("model") or "",
                str(vehicle.get("year") or ""),
                vehicle.get("powertrain") or "",
                vehicle.get("last_seen") or "",
            )
            for vehicle in vehicles
        ]
        self._post(
            self._write_table,
            "Vehicles",
            ["VIN", "Make", "Model", "Year", "Powertrain", "Last seen"],
            rows,
        )

    def _do_history(self) -> None:
        """Stored vehicles and recent sessions (read-only, no adapter)."""
        self._post(self._section, "History")
        store = _open_readonly_store()
        if store is None:
            self._post(self._line, "No local database found.", "yellow")
            return
        try:
            vehicles = store.vehicles()
            sessions = store.sessions(limit=25)
            self._post(self._line, f"Database: {store.db_file}", "dim")
        finally:
            store.close()
        self._post(
            self._write_table,
            "Vehicles",
            ["VIN", "Make", "Model", "Year", "Last seen"],
            [
                (
                    vehicle.get("vin") or "",
                    vehicle.get("make") or "",
                    vehicle.get("model") or "",
                    str(vehicle.get("year") or ""),
                    vehicle.get("last_seen") or "",
                )
                for vehicle in vehicles
            ],
        )
        self._post(
            self._write_table,
            "Recent sessions (25)",
            ["#", "Started", "Command", "Status", "VIN"],
            [
                (
                    str(session.get("id") or ""),
                    session.get("started_at") or "",
                    session.get("command") or "",
                    session.get("status") or "",
                    session.get("vin") or "",
                )
                for session in sessions
            ],
        )

    def _do_show(self, vin: str) -> None:
        """Everything stored for one VIN (read-only, no adapter)."""
        self._post(self._section, f"Stored data for {vin}")
        store = _open_readonly_store()
        if store is None:
            self._post(self._line, "No local database found.", "yellow")
            return
        try:
            summary = store.vehicle_summary(vin)
        finally:
            store.close()
        if not any(summary.get(key) for key in ("modules", "dtcs", "readings", "samples")):
            self._post(self._line, f"Nothing stored for {vin}.", "yellow")
            return
        self._post(
            self._write,
            Text(
                f"{len(summary['modules'])} module rows · {len(summary['dtcs'])} DTC rows · "
                f"{len(summary['readings'])} readings · {len(summary['samples'])} samples",
                style="bold",
            ),
        )
        self._post(
            self._write_table,
            "Stored DTCs",
            ["When", "Module", "Code", "Failure", "Status"],
            [
                (
                    row.get("ts") or "",
                    row.get("module") or "",
                    row.get("code") or "",
                    row.get("failure_type") or "",
                    row.get("status") or "",
                )
                for row in summary["dtcs"]
            ],
        )
        self._post(
            self._write_table,
            "Stored modules",
            ["When", "Bus", "Request", "Response", "Kind", "VIN", "Part"],
            [
                (
                    row.get("ts") or "",
                    row.get("bus") or "",
                    row.get("request_id") or "",
                    row.get("response_id") or "",
                    row.get("kind") or "",
                    row.get("vin_read") or "",
                    row.get("part_number") or "",
                )
                for row in summary["modules"][:50]
            ],
        )
        self._post(
            self._write_table,
            "Stored readings",
            ["When", "Module", "DID", "Name", "ASCII", "Hex"],
            [
                (
                    row.get("ts") or "",
                    row.get("module") or "",
                    row.get("did") or "",
                    row.get("name") or "",
                    row.get("value_ascii") or "",
                    row.get("value_hex") or "",
                )
                for row in summary["readings"][:100]
            ],
        )
        samples = summary["samples"][:50]
        self._post(
            self._write_table,
            f"Stored samples (latest {len(samples)} of {len(summary['samples'])})",
            ["When", "Module", "DID", "Value", "Status"],
            [
                (
                    row.get("ts") or "",
                    row.get("module") or "",
                    row.get("did") or "",
                    row.get("value_text")
                    or ("" if row.get("value_num") is None else str(row.get("value_num"))),
                    row.get("status") or "",
                )
                for row in samples
            ],
        )

    def _do_seedkey(self, pairs_text: str, algorithm_path: str, width: int) -> None:
        """Recover the per-ECU secret consistent with the observed pairs."""
        self._post(self._section, "Seed→key recovery")
        self._post(
            self._line,
            f"Searching {width}-byte secrets consistent with the pairs "
            f"(up to {1 << (8 * width):,} candidates, {BRUTE_BUDGET_SECONDS:g}s budget). "
            "Stop cancels it.",
            "dim",
        )
        try:
            result = _run_brute_subprocess(
                algorithm_path, pairs_text, width, self._cancel, BRUTE_BUDGET_SECONDS
            )
        except _SearchAborted:
            self._post(
                self._line,
                "Search stopped (Stop pressed or the budget ran out). Narrow the width, "
                "use a faster algorithm, or run it in the CLI.",
                "yellow",
            )
            return
        if "error" in result:
            self._post(self._log_err, f"Recovery failed: {result['error']}")
            return
        matches = result.get("matches") or []
        if not matches:
            self._post(self._line, "No secret consistent with those pairs.", "yellow")
            return
        if len(matches) > 1:
            self._post(
                self._line,
                "Ambiguous: several secrets fit; supply more observations (distinct seeds).",
                "yellow",
            )
            return
        secret = "%0*X" % (width * 2, int(matches[0]))
        self._post(
            self._write, Text(f"Recovered {width}-byte secret: {secret}", style="bold green")
        )
        self._post(
            self._line,
            "That is a candidate, not a proof: matching pairs do not confirm the algorithm family.",
            "dim",
        )

    def _do_write_did(self, bus: str, module: str, did: int, data: str, elm: Elm327) -> None:
        """UDS 0x2E WriteDataByIdentifier (already confirmed by the caller)."""
        wait = _probe_wait()
        self._post(
            self._section, f"Write DID 0x2E {did:04X} — {module} on {_bus_label(bus)}"
        )
        self._post(self._write, _write_warning())
        rid, resp = _addressed_session(elm, bus, module, wait)
        result = uds_write_did(elm, did, data, wait=wait, expect_header=resp)
        self._post(
            self._write_table,
            "Write DID result",
            ["Item", "Value"],
            [
                ("Module", rid),
                ("Bus", _bus_label(bus)),
                ("DID", result["did"]),
                ("Data", data),
                ("Status", result["status"]),
                ("Raw", result.get("raw") or "—"),
            ],
        )

    def _do_routine(
        self, bus: str, module: str, rid: int, sub: int, data: str, elm: Elm327
    ) -> None:
        """UDS 0x31 RoutineControl (already confirmed by the caller)."""
        wait = _probe_wait()
        self._post(
            self._section,
            f"Routine 0x31 {rid:04X} sub {sub} — {module} on {_bus_label(bus)}",
        )
        self._post(self._write, _write_warning())
        module_rid, resp = _addressed_session(elm, bus, module, wait)
        result = routine_control(elm, rid, sub=sub, data_hex=data, wait=wait, expect_header=resp)
        self._post(
            self._write_table,
            "Routine result",
            ["Item", "Value"],
            [
                ("Module", module_rid),
                ("Bus", _bus_label(bus)),
                ("Routine", result["rid"]),
                ("Sub-function", str(result["sub"])),
                ("Data", data or "—"),
                ("Status", result["status"]),
                ("Raw", result.get("raw") or "—"),
            ],
        )

    # -- live table UI callbacks (UI thread) -------------------------------
    def _live_start(
        self, label: str, columns: list[tuple[str, str]], rows: dict[str, tuple[str, ...]]
    ) -> None:
        self._live = True
        self._live_label = label
        self._samples = 0
        table = self._table
        if table is not None:
            table.clear(columns=True)
            table.add_columns(*columns)
            for key, cells in rows.items():
                table.add_row(*cells, key=key)
        self._live_button_label(label, active=True)
        self._refresh_status()

    def _live_sample(self, values: dict[str, dict[str, str]]) -> None:
        table = self._table
        if table is not None:
            for key, cells in values.items():
                try:
                    for column, value in cells.items():
                        table.update_cell(key, column, value)
                except Exception:  # noqa: BLE001 - the row set was rebuilt under us
                    pass
        self._samples += 1
        self._refresh_status()

    def _live_finished(self, label: str) -> None:
        self._live = False
        self._live_button_label(label, active=False)
        self._refresh_status()
        self._line(f"{label} stopped after {self._samples} sample(s).", "dim")

    def _live_button_label(self, label: str, *, active: bool) -> None:
        """Keep the Watch/Monitor button honest about which one is running."""
        try:
            watch = self.query_one("#btn-watch", Button)
            monitor = self.query_one("#btn-monitor", Button)
        except Exception:  # noqa: BLE001 - DOM gone during teardown
            return
        watch.label = "Stop live watch" if (active and label == "Watch") else "Watch live PIDs"
        monitor.label = "Stop monitor" if (active and label == "Monitor") else "Monitor UDS DIDs…"

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
