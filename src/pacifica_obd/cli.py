"""Command-line interface for pacifica-obd.

Subcommands
-----------
ports     list candidate serial ports (finds the vLinker FS)
info      adapter identity + vehicle/protocol info
vin       read the VIN (mode 09 02)
dtc       read (optionally clear) stored/pending/permanent trouble codes
live      dump the current value of every PID the car reports as supported
watch     live-poll a set of PIDs and redraw a dashboard (CSV / JSONL logging)
readiness readiness / I-M monitors (pre-emissions-test)
freeze    freeze-frame snapshot behind a DTC (mode 02)
scan      one-shot: VIN + DTCs + supported-PID count

Every read-only command supports --json for machine-readable output.
`watch --json` emits one JSON object per line (JSONL) instead.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import obd
from rich.console import Console
from rich.live import Live
from rich.table import Table

from pacifica_obd.adapter import (
    AdapterError,
    ElmAdapter,
    configure_bus,
    current_protocol,
    find_port,
    must_ok,
    restore_protocol,
)
from pacifica_obd.elm import Elm327, Elm327Error
from pacifica_obd.database import AlfaDatabase
from pacifica_obd.profiles import (
    DEFAULT_PROFILE,
    addresses as module_addresses,
    detect_powertrain,
    looks_like_vin,
    normalize_vin,
    resolve as resolve_profile,
    year_from_vin,
)
from pacifica_obd.report import write_dump
from pacifica_obd.seedkey import (
    SeedKeyError,
    find_secrets,
    load_algorithm,
    load_provider,
    parse_pairs,
    recover_secret,
)
from pacifica_obd.store import Store, data_dir, logging_enabled
from pacifica_obd.uds import (
    BUS_PROTOCOL as _BUS_PROTOCOL,
    INFO_DIDS as _INFO_DIDS,
    ascii_payload as _ascii_payload,
    classify_dtc_response as _classify_dtc_response,
    did_ascii as _did_ascii,
    did_value as _did_value,
    enter_session as _enter_session,
    parse_dtcs as _parse_dtcs,
    parse_uds_frame as _parse_uds_frame,
    probe_ids as _probe_ids,
    read_did as _read_did,
    routine_control as _routine_control,
    security_access as _security_access,
    write_did as _write_did,
)

console = Console()
err_console = Console(stderr=True)

# The vLinker FS runs its host UART at 115.2 kbaud (per FORScan guidance).
DEFAULT_BAUD = 115200

# Handy PIDs for the Pacifica's live dashboard.
DEFAULT_WATCH = [
    "RPM",
    "SPEED",
    "COOLANT_TEMP",
    "ENGINE_LOAD",
    "THROTTLE_POS",
    "INTAKE_TEMP",
    "MAF",
    "FUEL_LEVEL",
    "ELM_VOLTAGE",
]


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
def to_jsonable(value):
    """Fallback for json.dumps: pint Quantities become {value, unit}."""
    if hasattr(value, "magnitude") and hasattr(value, "units"):
        try:
            return {"value": float(value.magnitude), "unit": str(value.units)}
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode(errors="replace")
    return str(value)


def jdump(obj) -> None:
    print(json.dumps(obj, indent=2, default=to_jsonable))


def diag(args: argparse.Namespace):
    """Diagnostic/status console: stderr in JSON mode so stdout stays clean."""
    return err_console if getattr(args, "json", False) else console


def cmd_text(cmd) -> str:
    code = cmd.command
    return code.decode() if isinstance(code, (bytes, bytearray)) else str(code)


def _store(args: argparse.Namespace) -> Store | None:
    return getattr(args, "store", None)


def _session(args: argparse.Namespace):
    return getattr(args, "session_id", None)


def _vin(args: argparse.Namespace):
    """VIN discovered during this run (NOT the --vin argument)."""
    return getattr(args, "discovered_vin", None)


def _open_store(args: argparse.Namespace) -> Store:
    """Read-only store for history/show; never creates files."""
    return Store(db_file=getattr(args, "db", None) or None, readonly=True)


def _codes(conn, cmd) -> list[dict]:
    resp = conn.query(cmd)
    if resp.is_null() or not resp.value:
        return []
    return [{"code": str(c), "description": str(d)} for c, d in resp.value]


# --------------------------------------------------------------------------- #
# connection helpers
# --------------------------------------------------------------------------- #
def find_port(explicit: str | None) -> str | None:
    """Return a serial port, preferring the USB-serial adapter."""
    if explicit:
        return explicit
    ports = obd.scan_serial()
    if not ports:
        return None
    for p in ports:
        low = p.lower()
        if "usbserial" in low or "vlinker" in low or "ftdi" in low:
            return p
    return ports[0]


def connect(args: argparse.Namespace) -> obd.OBD:
    out = diag(args)
    port = find_port(args.port)
    if not port:
        out.print("[bold red]No serial port found.[/bold red]")
        out.print("Plug in the vLinker FS and check `ls /dev/cu.usb*`.")
        sys.exit(2)

    out.print(f"[dim]Opening {port} @ {args.baud} baud…[/dim]")
    try:
        conn = obd.OBD(
            portstr=port,
            baudrate=args.baud,
            fast=True,
            timeout=args.timeout,
            check_voltage=args.check_voltage,
        )
    except Exception as exc:  # noqa: BLE001 - surface any driver error clearly
        out.print(f"[bold red]Could not open the port:[/bold red] {exc}")
        out.print("Is another app (FORScan, screen, …) holding it open?")
        sys.exit(2)

    if not conn.is_connected():
        out.print(
            f"[yellow]Adapter is up but no vehicle bus was found "
            f"(status: {conn.status()}).[/yellow]"
        )
        out.print(
            "[yellow]Turn the ignition to ON (or start the engine) and try again.[/yellow]"
        )
    return conn


# --------------------------------------------------------------------------- #
# raw-ELM (UDS) helpers
# --------------------------------------------------------------------------- #
def open_elm(args: argparse.Namespace) -> ElmAdapter:
    """Open the adapter via the device layer (protocol snapshot/restore on close)."""
    try:
        return ElmAdapter(port=args.port, baudrate=args.baud, timeout=args.timeout)
    except AdapterError as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)


def _can_id(text) -> int:
    try:
        value = int(str(text), 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a hex CAN ID: {text!r}")
    if not 0 <= value <= 0x7FF:
        raise argparse.ArgumentTypeError(f"CAN ID out of 11-bit range: {text!r}")
    return value


def _hex_request(text) -> str:
    value = str(text).upper()
    if len(value) < 2 or len(value) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", value):
        raise argparse.ArgumentTypeError(f"not a hex request: {text!r}")
    return value


def _timeout_ms(text) -> int:
    try:
        value = int(str(text))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}")
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("timeout must be 1..65535 ms")
    return value


def _positive_interval(text) -> float:
    try:
        value = float(str(text))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if not (value > 0) or value != value or value in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError("interval must be a finite positive number")
    return value


def _width(text) -> int:
    try:
        value = int(str(text))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}")
    if not 1 <= value <= 4:
        raise argparse.ArgumentTypeError("width must be 1..4 bytes")
    return value


def _did(text) -> int:
    value = str(text).upper().removeprefix("0X")
    if not re.fullmatch(r"[0-9A-Fa-f]{4}", value):
        raise argparse.ArgumentTypeError(f"not a 4-hex DID: {text!r}")
    return int(value, 16)


def _vin_arg(text) -> str:
    value = normalize_vin(text)
    if not looks_like_vin(value):
        raise argparse.ArgumentTypeError(f"not a valid VIN: {text!r}")
    return value


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_ports(args: argparse.Namespace) -> None:
    ports = obd.scan_serial()
    if args.json:
        jdump({"ports": ports})
        return
    if not ports:
        console.print("[yellow]No candidate serial ports found.[/yellow]")
        return
    table = Table(title="Candidate serial ports")
    table.add_column("#", justify="right")
    table.add_column("Device")
    for i, port in enumerate(ports, 1):
        table.add_row(str(i), port)
    console.print(table)


def cmd_info(args: argparse.Namespace) -> None:
    conn = connect(args)
    data = {
        "port": conn.port_name(),
        "status": str(conn.status()),
        "protocol": conn.protocol_name(),
        "adapter": None if conn.query(obd.commands.ELM_VERSION).is_null()
        else conn.query(obd.commands.ELM_VERSION).value,
        "voltage": None if conn.query(obd.commands.ELM_VOLTAGE).is_null()
        else conn.query(obd.commands.ELM_VOLTAGE).value,
        "vin": None if conn.query(obd.commands.VIN).is_null() else conn.query(obd.commands.VIN).value,
    }
    if args.json:
        jdump(data)
        conn.close()
        return

    table = Table(title="Connection info")
    table.add_column("Item")
    table.add_column("Value")
    for label, key in (
        ("Port", "port"),
        ("Status", "status"),
        ("Protocol", "protocol"),
        ("Adapter (ATI)", "adapter"),
        ("Voltage (ATRV)", "voltage"),
        ("VIN", "vin"),
    ):
        value = data[key]
        table.add_row(label, "—" if value is None else str(value))
    console.print(table)
    conn.close()


def cmd_vin(args: argparse.Namespace) -> None:
    conn = connect(args)
    resp = conn.query(obd.commands.VIN)
    vin = None if resp.is_null() else resp.value
    if vin and _store(args):
        _store(args).upsert_vehicle(vin)
        args.discovered_vin = vin
    if args.json:
        jdump({"vin": vin})
    elif vin is None:
        console.print("[yellow]No VIN returned (vehicle off or unsupported).[/yellow]")
    else:
        console.print(f"[bold green]VIN:[/bold green] {vin}")
    conn.close()


def cmd_dtc(args: argparse.Namespace) -> None:
    if args.clear and args.json and not args.force:
        err_console.print("[red]--json --clear requires --force (no prompt in JSON mode).[/red]")
        sys.exit(2)

    conn = connect(args)
    data = {
        "stored": _codes(conn, obd.commands.GET_DTC),
        "pending": _codes(conn, obd.commands.GET_CURRENT_DTC)
        if hasattr(obd.commands, "GET_CURRENT_DTC")
        else [],
        "permanent": _codes(conn, obd.commands.GET_PERMANENT_DTC)
        if hasattr(obd.commands, "GET_PERMANENT_DTC")
        else [],
        "cleared": False,
    }

    if not args.json:
        _render_codes(data["stored"], "Stored codes (mode 03)")
        _render_codes(data["pending"], "Pending codes (mode 07)")
        _render_codes(data["permanent"], "Permanent codes (mode 0A)")

    total = len(data["stored"]) + len(data["pending"]) + len(data["permanent"])
    if args.clear:
        if total == 0 and not args.force:
            if not args.json:
                console.print("[yellow]Nothing to clear.[/yellow]")
        else:
            if args.force:
                proceed = True
            else:
                console.print(
                    "\n[bold red]About to CLEAR codes.[/bold red] "
                    "This erases freeze-frame data and resets readiness monitors."
                )
                proceed = input("Type 'yes' to confirm: ").strip().lower() == "yes"
            if proceed:
                conn.query(obd.commands.CLEAR_DTC)
                data["cleared"] = True
                if not args.json:
                    console.print("[green]Clear command sent.[/green]")
            elif not args.json:
                console.print("[yellow]Aborted.[/yellow]")

    if args.json:
        jdump(data)
    conn.close()


def _render_codes(codes: list[dict], label: str) -> None:
    if not codes:
        console.print(f"[green]{label}: none[/green]")
        return
    table = Table(title=label)
    table.add_column("Code", style="bold red")
    table.add_column("Description")
    for item in codes:
        table.add_row(item["code"], item["description"])
    console.print(table)


def cmd_live(args: argparse.Namespace) -> None:
    conn = connect(args)
    rows = []
    for cmd in sorted(conn.supported_commands, key=lambda c: c.name):
        if cmd.command.startswith(b"AT") or cmd.command.startswith("AT"):
            continue
        resp = conn.query(cmd)
        rows.append(
            {
                "name": cmd.name,
                "command": cmd_text(cmd),
                "value": None if resp.is_null() else resp.value,
            }
        )

    if args.json:
        jdump({"pids": rows})
        conn.close()
        return

    table = Table(title="Current values — supported PIDs")
    table.add_column("PID")
    table.add_column("Command")
    table.add_column("Value")
    for row in rows:
        table.add_row(
            row["name"],
            row["command"],
            "—" if row["value"] is None else str(row["value"]),
        )
    console.print(table)
    conn.close()


def cmd_watch(args: argparse.Namespace) -> None:
    conn = connect(args)
    names = [n.strip() for n in args.pids.split(",")] if args.pids else DEFAULT_WATCH
    cmds = [getattr(obd.commands, n) for n in names if hasattr(obd.commands, n)]
    if not cmds:
        diag(args).print("[red]None of those PIDs are known.[/red]")
        sys.exit(2)

    csv_file = None
    writer = None
    if args.csv:
        path = os.path.expanduser(args.csv)
        csv_file = open(path, "w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(["timestamp", *[c.name for c in cmds]])
        csv_file.flush()
        diag(args).print(f"[dim]Logging CSV to {path}[/dim]")

    if not args.json:
        diag(args).print(
            f"[dim]Polling {', '.join(c.name for c in cmds)} every {args.interval}s — Ctrl-C to stop.[/dim]"
        )
    else:
        diag(args).print("[dim]Streaming JSONL — Ctrl-C to stop.[/dim]", end="\n")

    live = None if args.json else Live(refresh_per_second=2, console=console)
    if live:
        live.start()
    try:
        while True:
            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            values: dict[str, object] = {}
            for cmd in cmds:
                resp = conn.query(cmd)
                values[cmd.name] = None if resp.is_null() else resp.value

            if args.json:
                print(json.dumps({"timestamp": stamp, "values": values}, default=to_jsonable), flush=True)
            else:
                table = Table(title="Live data")
                table.add_column("PID")
                table.add_column("Value", justify="right")
                for cmd in cmds:
                    value = values[cmd.name]
                    table.add_row(cmd.name, "—" if value is None else str(value))
                live.update(table)

            if writer:
                writer.writerow(
                    [stamp, *["" if values[c.name] is None else str(values[c.name]) for c in cmds]]
                )
                csv_file.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        diag(args).print("[dim]Stopped.[/dim]")
    finally:
        if live:
            live.stop()
        conn.close()
        if csv_file:
            csv_file.close()
            diag(args).print(f"[dim]Saved {args.csv}[/dim]")


def cmd_readiness(args: argparse.Namespace) -> None:
    """Readiness / I-M monitors (mode 01 PID 01) — useful before an emissions test."""
    conn = connect(args)
    resp = conn.query(obd.commands.STATUS)
    if resp.is_null():
        if args.json:
            jdump({"available": False, "reason": "no readiness data"})
        else:
            console.print("[yellow]No readiness data (vehicle off or unsupported).[/yellow]")
        conn.close()
        return

    status = resp.value
    available = [
        (name, value)
        for name, value in status.__dict__.items()
        if hasattr(value, "available") and hasattr(value, "complete") and value.available
    ]
    mil = bool(status.MIL)
    dtc_count = status.DTC_count
    incomplete = [name for name, value in available if not value.complete]
    ready = (not mil) and dtc_count == 0 and not incomplete

    data = {
        "ignition_type": status.ignition_type,
        "mil": mil,
        "dtc_count": dtc_count,
        "ready": ready,
        "monitors": [
            {"name": name, "complete": bool(value.complete)} for name, value in available
        ],
    }

    if args.json:
        jdump(data)
        conn.close()
        return

    console.print(f"[bold]Ignition type:[/bold] {data['ignition_type']}")
    console.print(f"[bold]MIL (check-engine):[/bold] {'[red]ON[/red]' if mil else '[green]off[/green]'}")
    console.print(f"[bold]Stored DTCs:[/bold] {dtc_count}")
    if ready:
        console.print("[bold green]READY — monitors complete, no MIL, no codes.[/bold green]")
    else:
        console.print("[bold yellow]NOT READY[/bold yellow]")

    if available:
        table = Table(title="Readiness monitors")
        table.add_column("Monitor")
        table.add_column("Status")
        for name, value in available:
            mark = "[green]complete[/green]" if value.complete else "[red]incomplete[/red]"
            table.add_row(name.replace("_", " ").title(), mark)
        console.print(table)
    else:
        console.print("[dim]No monitors reported as available.[/dim]")
    conn.close()


# --------------------------------------------------------------------------- #
# freeze frame (mode 02)
# --------------------------------------------------------------------------- #
# (pid, label, unit, decoder). The decoder receives the payload *after* the
# frame-number byte and returns a number.
_FREEZE_PIDS = [
    (0x04, "Engine load", "%", lambda b: b[0] * 100 / 255),
    (0x05, "Coolant temp", "°C", lambda b: b[0] - 40),
    (0x0B, "Intake pressure", "kPa", lambda b: b[0]),
    (0x0C, "Engine RPM", "rpm", lambda b: ((b[0] << 8) | b[1]) / 4),
    (0x0D, "Vehicle speed", "km/h", lambda b: b[0]),
    (0x0E, "Timing advance", "°", lambda b: b[0] / 2 - 64),
    (0x0F, "Intake temp", "°C", lambda b: b[0] - 40),
    (0x10, "MAF", "g/s", lambda b: ((b[0] << 8) | b[1]) / 100),
    (0x11, "Throttle", "%", lambda b: b[0] * 100 / 255),
    (0x06, "Short fuel trim", "%", lambda b: b[0] / 1.28 - 100),
    (0x07, "Long fuel trim", "%", lambda b: b[0] / 1.28 - 100),
    (0x1F, "Run time", "s", lambda b: (b[0] << 8) | b[1]),
    (0x2F, "Fuel level", "%", lambda b: b[0] * 100 / 255),
    (0x42, "Module voltage", "V", lambda b: ((b[0] << 8) | b[1]) / 1000),
    (0x5C, "Oil temp", "°C", lambda b: b[0] - 40),
    (0x33, "Barometric pressure", "kPa", lambda b: b[0]),
]


def _query_freeze(conn: obd.OBD, pid: int) -> bytes | None:
    """Raw mode-02 request; returns the payload after the frame-number byte."""
    try:
        messages = conn.interface.send_and_parse("02%02X00" % pid)
    except Exception:  # noqa: BLE001 - a bad/no response is not fatal
        return None
    if not messages:
        return None
    for msg in messages:
        data = bytes(msg.data)
        if len(data) >= 3 and data[0] == 0x42 and data[1] == pid:
            return data[3:]
    return None


def cmd_freeze(args: argparse.Namespace) -> None:
    conn = connect(args)
    resp = conn.query(obd.commands.FREEZE_DTC)
    raw = None if resp.is_null() else resp.value

    frame_dtc = None
    if isinstance(raw, (list, tuple)) and raw:
        first = raw[0]
        frame_dtc = first[0] if isinstance(first, (list, tuple)) else first
    elif raw:
        frame_dtc = str(raw)

    if not frame_dtc:
        if args.json:
            jdump({"dtc": None, "params": [], "reason": "no freeze-frame data stored"})
        else:
            console.print("[yellow]No freeze-frame data stored.[/yellow]")
            console.print("[dim]A snapshot is only captured when an emissions fault sets.[/dim]")
        conn.close()
        return

    params = []
    for pid, label, unit, decoder in _FREEZE_PIDS:
        payload = _query_freeze(conn, pid)
        if payload is None:
            continue
        try:
            value = decoder(payload)
        except (IndexError, TypeError):
            continue
        params.append({"pid": "0x%02X" % pid, "name": label, "value": value, "unit": unit})

    if args.json:
        jdump({"dtc": str(frame_dtc), "params": params})
        conn.close()
        return

    console.print(f"[bold]Freeze-frame DTC:[/bold] [red]{frame_dtc}[/red]")
    if not params:
        console.print("[dim]No decodable freeze-frame PIDs returned.[/dim]")
        conn.close()
        return
    table = Table(title="Freeze-frame snapshot (mode 02, frame 0)")
    table.add_column("PID")
    table.add_column("Parameter")
    table.add_column("Value", justify="right")
    for item in params:
        table.add_row(item["pid"], item["name"], f"{item['value']:g} {item['unit']}")
    console.print(table)
    conn.close()


def cmd_scan(args: argparse.Namespace) -> None:
    conn = connect(args)
    n_pids = len([c for c in conn.supported_commands if not c.command.startswith(b"AT")])
    data = {
        "status": str(conn.status()),
        "protocol": conn.protocol_name(),
        "vin": None if conn.query(obd.commands.VIN).is_null() else conn.query(obd.commands.VIN).value,
        "stored_dtcs": _codes(conn, obd.commands.GET_DTC),
        "supported_pids": n_pids,
    }

    if _store(args) and data["vin"]:
        _store(args).upsert_vehicle(data["vin"])
        args.discovered_vin = data["vin"]
    if _store(args):
        _store(args).add_dtcs(_session(args), _vin(args), "obd", "PCM", data["stored_dtcs"], None)

    if args.json:
        jdump(data)
        conn.close()
        return

    console.print(f"[bold]Status:[/bold] {data['status']}")
    console.print(f"[bold]Protocol:[/bold] {data['protocol'] or '—'}")
    console.print(f"[bold]VIN:[/bold] {data['vin'] or '—'}")
    _render_codes(data["stored_dtcs"], "Stored codes (mode 03)")
    console.print(f"[bold]Supported OBD-II PIDs:[/bold] {data['supported_pids']}")
    conn.close()


def cmd_raw(args: argparse.Namespace) -> None:
    """Send arbitrary AT/ST/OBD/UDS commands straight to the adapter."""
    port = find_port(args.port)
    if not port:
        err_console.print("[bold red]No serial port found.[/bold red]")
        sys.exit(2)
    try:
        elm = Elm327(port, args.baud, timeout=args.timeout)
    except (AdapterError, Elm327Error) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)

    results = []
    try:
        if args.header:
            elm.cmd("ATSH " + args.header)
        if args.protocol:
            elm.cmd("ATTP" + args.protocol)  # try protocol (non-saving, no EEPROM write)
        if args.headers:
            elm.cmd("ATH1")
        if args.no_auto_format:
            elm.cmd("ATCAF0")
        if args.no_flow_control:
            elm.cmd("ATCFC0")
        if args.allow_long:
            elm.cmd("ATAL")
        for command in args.commands:
            response = elm.cmd(command, wait=args.timeout)
            results.append({"command": command, "response": response})
            if not args.json:
                console.print(f"[bold]{command}[/bold] -> {response}")
    finally:
        elm.close()

    if args.json:
        jdump({"port": port, "results": results})


# --------------------------------------------------------------------------- #
# module discovery (UDS, CAN-C / CAN-IHS)
# --------------------------------------------------------------------------- #
def cmd_modules(args: argparse.Namespace) -> None:
    """Probe a CAN bus for UDS-responding modules."""
    start, end = args.start, args.end  # validated 11-bit ints (see _can_id)
    if end < start:
        err_console.print("[red]--end must be >= --start.[/red]")
        sys.exit(2)

    protocol = _BUS_PROTOCOL[args.bus]
    service = int(args.session[:2], 16)
    if service not in (0x10, 0x22, 0x19, 0x3E) and not getattr(args, "confirm", False):
        err_console.print(
            f"[red]Refusing to send service 0x{service:02X} to a range without --confirm.[/red]"
        )
        sys.exit(2)
    diag(args).print(f"[dim]Scanning {args.bus} (STP {protocol}) {start:03X}–{end:03X}…[/dim]")

    elm = open_elm(args)
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    found: list[dict] = []
    try:
        configure_bus(elm, args.bus, args.timeout_ms)

        for entry in _probe_ids(elm, start, end, args.session, wait):
            if args.identify:
                elm.cmd("ATSH " + entry["request_id"])
                _, vin_uds = _parse_uds_frame(elm.cmd("22F190", wait=wait))
                if vin_uds:
                    vin = _ascii_payload(vin_uds, "62F190")
                    if vin:
                        entry["vin"] = vin
                _, part_uds = _parse_uds_frame(elm.cmd("22F187", wait=wait))
                if part_uds:
                    part = _ascii_payload(part_uds, "62F187")
                    if part:
                        entry["part_number"] = part
            found.append(entry)
    except (AdapterError, Elm327Error) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        elm.close()

    args.discovered_powertrain = detect_powertrain({entry["request_id"] for entry in found})
    if _store(args):
        for entry in found:
            _store(args).add_module(_session(args), _vin(args), args.bus, entry)

    if args.json:
        jdump(
            {
                "bus": args.bus,
                "protocol": protocol,
                "range": ["%03X" % start, "%03X" % end],
                "modules": found,
            }
        )
        return

    if not found:
        console.print("[yellow]No modules responded.[/yellow]")
        return
    table = Table(title=f"Modules on {args.bus} (STP {protocol})")
    table.add_column("Request")
    table.add_column("Response")
    table.add_column("Type")
    table.add_column("VIN / Part")
    for entry in found:
        extra = entry.get("vin") or entry.get("part_number") or ""
        table.add_row(entry["request_id"], entry["response_id"], entry["kind"], extra)
    console.print(table)
    console.print(f"[dim]{len(found)} module(s) responded.[/dim]")


# --------------------------------------------------------------------------- #
# all-module DTC read (UDS 0x19 0x02)
# --------------------------------------------------------------------------- #
def cmd_dtc_all(args: argparse.Namespace) -> None:
    """Read DTCs (UDS 0x19) from every responding module on one or both buses."""
    start, end = args.start, args.end
    if end < start:
        err_console.print("[red]--end must be >= --start.[/red]")
        sys.exit(2)

    module = "%03X" % args.module if args.module is not None else None
    buses = ["can-c", "can-ihs"] if args.bus == "both" else [args.bus]
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)

    elm = open_elm(args)

    results: list[dict] = []
    try:
        for bus in buses:
            diag(args).print(f"[dim]Reading DTCs on {bus} (STP {_BUS_PROTOCOL[bus]})…[/dim]")
            configure_bus(elm, bus, args.timeout_ms)

            if module:
                targets = [{"request_id": module}]
            else:
                targets = list(_probe_ids(elm, start, end, "1003", wait))

            for target in targets:
                rid = target["request_id"]
                elm.cmd("ATSH " + rid)
                elm.cmd("1003", wait=wait)  # enter extended session
                header, uds = _parse_uds_frame(elm.cmd("1902FF", wait=wait))
                if header is None:
                    record = {
                        "bus": bus, "request_id": rid, "response_id": None,
                        "status": "no_response", "raw": None, "dtcs": [],
                    }
                else:
                    classification = _classify_dtc_response(uds)
                    record = {
                        "bus": bus, "request_id": rid, "response_id": header,
                        "status": classification, "raw": uds,
                        "dtcs": _parse_dtcs(uds) if classification == "ok" else [],
                    }
                results.append(record)  # keep every target in the report
                if _store(args):
                    _store(args).add_dtcs(
                        _session(args), _vin(args), bus, rid, record["dtcs"], record["raw"]
                    )
                    _store(args).event(
                        _session(args), "dtc-read", rid,
                        {"status": record["status"], "count": len(record["dtcs"])},
                    )
    except (AdapterError, Elm327Error) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        elm.close()

    total = sum(len(record["dtcs"]) for record in results)

    if args.json:
        jdump(
            {
                "buses": buses,
                "total_dtcs": total,
                "incomplete": any(record["status"] != "ok" for record in results),
                "modules": results,
            }
        )
        return

    if not results:
        console.print("[yellow]No modules responded.[/yellow]")
        return

    table = Table(title="All-module DTCs (UDS 0x19)")
    table.add_column("Bus")
    table.add_column("Module")
    table.add_column("Code", style="bold red")
    table.add_column("Flags")
    for record in results:
        label = record["request_id"]
        if record["dtcs"]:
            for dtc in record["dtcs"]:
                table.add_row(
                    record["bus"],
                    label,
                    f"{dtc['code']}-{dtc['failure_type']}",
                    ", ".join(dtc["flags"]),
                )
                label = ""  # only label the first row of each module
        else:
            note = {
                "negative": "negative response",
                "malformed": "malformed response",
                "unexpected": "unexpected response",
                "no_response": "no response",
                "ok": "no DTCs",
            }[record["status"]]
            table.add_row(record["bus"], label, "—", note)
    console.print(table)
    console.print(f"[dim]{total} DTC(s) across {len(results)} module(s).[/dim]")


# --------------------------------------------------------------------------- #
# read data by identifier (UDS 0x22)
# --------------------------------------------------------------------------- #
def cmd_read_did(args: argparse.Namespace) -> None:
    """Read Data Identifiers (UDS 0x22) from a single module."""
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    elm = open_elm(args)
    rid = "%03X" % args.module
    results: list[dict] = []
    try:
        configure_bus(elm, args.bus, args.timeout_ms)
        elm.cmd("ATSH " + rid)
        expect = _enter_session(elm, rid, wait)  # correlate replies to this ECU
        for did in args.dids:
            record = _read_did(elm, did, wait, expect_header=expect)
            record["module"] = rid
            record["bus"] = args.bus
            results.append(record)
            if _store(args):
                _store(args).add_reading(
                    _session(args), _vin(args), args.bus, rid, record["did"],
                    record.get("name"), record.get("hex"), record.get("ascii"),
                    record["status"], record.get("raw"), category="did",
                )
    except (AdapterError, Elm327Error) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        elm.close()

    if args.json:
        jdump({"module": rid, "bus": args.bus, "dids": results})
        return

    table = Table(title=f"DIDs from module {rid} ({args.bus})")
    table.add_column("DID")
    table.add_column("Name")
    table.add_column("Hex")
    table.add_column("ASCII")
    for record in results:
        table.add_row(record["did"], record.get("name") or "", _did_value(record), _did_ascii(record))
    console.print(table)


def cmd_module_info(args: argparse.Namespace) -> None:
    """Read a module's identification block (F186..F191) from one module or a scan."""
    if args.end < args.start:
        err_console.print("[red]--end must be >= --start.[/red]")
        sys.exit(2)
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    buses = ["can-c", "can-ihs"] if args.bus == "both" else [args.bus]
    elm = open_elm(args)
    results: list[dict] = []
    try:
        for bus in buses:
            configure_bus(elm, bus, args.timeout_ms)
            if args.module is not None:
                request_ids = ["%03X" % args.module]
            else:
                request_ids = [
                    target["request_id"]
                    for target in _probe_ids(elm, args.start, args.end, "1003", wait)
                ]
            for rid in request_ids:
                elm.cmd("ATSH " + rid)
                expect = _enter_session(elm, rid, wait)
                for did in _INFO_DIDS:
                    record = _read_did(elm, did, wait, expect_header=expect)
                    record["module"] = rid
                    record["bus"] = bus
                    results.append(record)
                    if _store(args):
                        _store(args).add_reading(
                            _session(args), _vin(args), bus, rid, record["did"],
                            record.get("name"), record.get("hex"), record.get("ascii"),
                            record["status"], record.get("raw"), category="module-info",
                        )
    except (AdapterError, Elm327Error) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        elm.close()

    if args.json:
        jdump({"buses": buses, "modules": results})
        return

    if not results:
        console.print("[yellow]No modules responded.[/yellow]")
        return
    table = Table(title="Module identification")
    table.add_column("Bus")
    table.add_column("Module")
    table.add_column("Item")
    table.add_column("Hex")
    table.add_column("ASCII")
    last = None
    for record in results:
        key = (record["bus"], record["module"])
        fresh = key != last
        last = key
        table.add_row(
            record["bus"] if fresh else "",
            record["module"] if fresh else "",
            record.get("name") or record["did"],
            _did_value(record),
            _did_ascii(record),
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# full capture + local history
# --------------------------------------------------------------------------- #
def cmd_capture(args: argparse.Namespace) -> None:
    """Full snapshot: discover modules, read DTCs and the ID block, persist it all."""
    if args.end < args.start:
        err_console.print("[red]--end must be >= --start.[/red]")
        sys.exit(2)
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    buses = ["can-c", "can-ihs"] if args.bus == "both" else [args.bus]
    elm = open_elm(args)
    store = _store(args)
    session = _session(args)
    vin = normalize_vin(getattr(args, "vin", None))  # an explicit --vin wins
    snapshot: dict = {"vin": None, "buses": buses, "modules": [], "dtcs": [], "readings": []}
    try:
        for bus in buses:
            diag(args).print(f"[dim]Capturing {bus} (STP {_BUS_PROTOCOL[bus]})…[/dim]")
            configure_bus(elm, bus, args.timeout_ms)
            for target in _probe_ids(elm, args.start, args.end, "1003", wait):
                rid = target["request_id"]
                elm.cmd("ATSH " + rid)
                expect = _enter_session(elm, rid, wait)

                _, dtc_uds = _parse_uds_frame(elm.cmd("1902FF", wait=wait))
                dtcs = (
                    _parse_dtcs(dtc_uds)
                    if dtc_uds and _classify_dtc_response(dtc_uds) == "ok"
                    else []
                )
                snapshot["dtcs"].append({"bus": bus, "request_id": rid, "dtcs": dtcs, "raw": dtc_uds})
                if store:
                    store.add_dtcs(session, vin, bus, rid, dtcs, dtc_uds)
                    store.event(
                        session, "dtc-read", rid,
                        {"status": _classify_dtc_response(dtc_uds) if dtc_uds else "no_response",
                         "count": len(dtcs)},
                    )

                module_vin = None
                part = None
                for did in _INFO_DIDS:
                    record = _read_did(elm, did, wait, expect_header=expect)
                    record["module"] = rid
                    record["bus"] = bus
                    snapshot["readings"].append(record)
                    if did == 0xF190 and record["status"] == "ok" and looks_like_vin(record.get("ascii")):
                        module_vin = record["ascii"]
                        if vin is None:
                            vin = module_vin
                            args.discovered_vin = vin  # record even if we stop early
                            if store:
                                store.upsert_vehicle(vin)
                                store.backfill_vin(session, vin)
                        elif module_vin != vin:
                            err_console.print(
                                f"[yellow]VIN mismatch: {rid} reports {module_vin}, keeping {vin}.[/yellow]"
                            )
                    if did == 0xF187 and record["status"] == "ok":
                        part = record.get("ascii") or record.get("hex")
                    if store:
                        store.add_reading(
                            session, vin, bus, rid, record["did"], record.get("name"),
                            record.get("hex"), record.get("ascii"), record["status"],
                            record.get("raw"), category="module-info",
                        )

                module = {
                    "request_id": rid,
                    "response_id": target.get("response_id"),
                    "kind": target.get("kind"),
                    "vin": module_vin,
                    "part_number": part,
                }
                snapshot["modules"].append(module)
                if store:
                    store.add_module(session, vin, bus, module)
    except (AdapterError, Elm327Error) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        elm.close()

    if vin and store:
        store.upsert_vehicle(vin, powertrain=detect_powertrain({m["request_id"] for m in snapshot["modules"]}))
        args.discovered_vin = vin
    args.discovered_powertrain = detect_powertrain({m["request_id"] for m in snapshot["modules"]})
    snapshot["vin"] = vin

    if args.json:
        jdump(snapshot)
        return

    n_dtcs = sum(len(item["dtcs"]) for item in snapshot["dtcs"])
    console.print(f"[bold]VIN:[/bold] {vin or '—'}")
    console.print(
        f"[bold]Modules:[/bold] {len(snapshot['modules'])}  "
        f"[bold]DTCs:[/bold] {n_dtcs}  "
        f"[bold]Readings:[/bold] {len(snapshot['readings'])}"
    )
    if store:
        console.print(f"[dim]Stored to {store.db_file}[/dim]")
    elif args.no_log:
        console.print("[dim]Not stored (--no-log).[/dim]")


def cmd_history(args: argparse.Namespace) -> None:
    """List stored vehicles and recent sessions."""
    store = _open_store(args)
    if not store.available:
        err_console.print("[yellow]No local database found.[/yellow]")
        sys.exit(1)
    vehicles = store.vehicles()
    sessions = store.sessions(limit=args.limit)
    if args.json:
        jdump({"vehicles": vehicles, "sessions": sessions})
        store.close()
        return

    if vehicles:
        table = Table(title="Vehicles")
        for column in ("vin", "make", "model", "year", "last_seen"):
            table.add_column(column)
        for vehicle in vehicles:
            table.add_row(
                vehicle.get("vin") or "", vehicle.get("make") or "", vehicle.get("model") or "",
                str(vehicle.get("year") or ""), vehicle.get("last_seen") or "",
            )
        console.print(table)

    table = Table(title="Recent sessions")
    for column in ("id", "started_at", "command", "status", "vin"):
        table.add_column(column)
    for session in sessions:
        table.add_row(
            str(session.get("id")), session.get("started_at") or "",
            session.get("command") or "", session.get("status") or "", session.get("vin") or "",
        )
    console.print(table)
    console.print(f"[dim]Database: {store.db_file}[/dim]")
    store.close()


def cmd_show(args: argparse.Namespace) -> None:
    """Show everything stored for a VIN."""
    store = _open_store(args)
    summary = store.vehicle_summary(args.vin)
    if args.json:
        jdump(summary)
        store.close()
        return

    console.print(f"[bold]VIN:[/bold] {summary['vin']}")
    console.print(f"[dim]{len(summary['modules'])} module rows · {len(summary['dtcs'])} DTC rows · {len(summary['readings'])} readings[/dim]")

    table = Table(title="Stored DTCs")
    for column in ("ts", "module", "code", "failure_type", "status", "flags"):
        table.add_column(column)
    for row in summary["dtcs"]:
        table.add_row(
            row.get("ts") or "", row.get("module") or "", row.get("code") or "",
            row.get("failure_type") or "", row.get("status") or "", row.get("flags") or "",
        )
    console.print(table)

    table = Table(title="Stored readings")
    for column in ("ts", "module", "did", "name", "ascii", "hex", "status"):
        table.add_column(column)
    for row in summary["readings"]:
        table.add_row(
            row.get("ts") or "", row.get("module") or "", row.get("did") or "",
            row.get("name") or "", row.get("value_ascii") or "", row.get("value_hex") or "",
            row.get("status") or "",
        )
    console.print(table)
    store.close()


def cmd_tui(args: argparse.Namespace) -> None:
    """Launch the interactive Textual UI."""
    try:
        from pacifica_obd.tui import run
    except ImportError as exc:  # pragma: no cover - depends on optional build
        err_console.print(f"[red]Interactive UI not available: {exc}[/red]")
        sys.exit(1)
    run()


def cmd_vehicles(args: argparse.Namespace) -> None:
    """List vehicles we have records for."""
    store = _open_store(args)
    if not store.available:
        err_console.print("[yellow]No local database found.[/yellow]")
        sys.exit(1)
    vehicles = store.vehicles()
    if args.json:
        jdump({"vehicles": vehicles})
        store.close()
        return
    if not vehicles:
        console.print("[yellow]No vehicles recorded yet.[/yellow]")
        store.close()
        return
    columns = ("vin", "make", "model", "year", "profile_id", "powertrain", "last_seen")
    table = Table(title="Vehicles")
    for column in columns:
        table.add_column(column)
    for vehicle in vehicles:
        table.add_row(*(str(vehicle.get(column) or "") for column in columns))
    console.print(table)
    console.print(f"[dim]Database: {store.db_file}[/dim]")
    store.close()


def cmd_monitor(args: argparse.Namespace) -> None:
    """Poll UDS DIDs on one or many modules and store each sample."""
    if args.end < args.start:
        err_console.print("[red]--end must be >= --start.[/red]")
        sys.exit(2)
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    dids = list(dict.fromkeys(did for group in args.dids for did in group))
    store = _store(args)
    session = _session(args)
    elm = open_elm(args)
    try:
        configure_bus(elm, args.bus, args.timeout_ms)
        if args.module is not None:
            modules = ["%03X" % args.module]
        else:
            modules = [t["request_id"] for t in _probe_ids(elm, args.start, args.end, "1003", wait)]
        if not modules:
            err_console.print("[yellow]No modules responded.[/yellow]")
            sys.exit(1)
        diag(args).print(
            f"[dim]Monitoring {len(modules)} module(s) x {len(dids)} DID(s) every {args.interval}s — Ctrl-C to stop.[/dim]"
        )
        expects = {}
        for rid in modules:
            elm.cmd("ATSH " + rid)
            expects[rid] = _enter_session(elm, rid, wait)
        while True:
            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            rows = []
            for rid in modules:
                # re-enter the extended session each cycle so it never expires
                elm.cmd("ATSH " + rid)
                expects[rid] = _enter_session(elm, rid, wait)
                for did in dids:
                    record = _read_did(elm, did, wait, expect_header=expects.get(rid))
                    value = record.get("hex")
                    number = None
                    if value and len(value) <= 4:
                        try:
                            number = int(value, 16)
                        except ValueError:
                            number = None
                    rows.append(
                        {
                            "module": rid, "did": record["did"], "name": record.get("name"),
                            "status": record["status"], "hex": value,
                            "ascii": record.get("ascii"), "num": number,
                        }
                    )
                    if store:
                        store.add_sample(
                            session, _vin(args), args.bus, rid, record["did"], record.get("name"),
                            number, record.get("ascii") or value, None, record["status"], record.get("raw"),
                        )
            if args.json:
                print(json.dumps({"timestamp": stamp, "values": rows}, default=to_jsonable), flush=True)
            else:
                table = Table(title="Live UDS data")
                for column in ("Module", "DID", "Name", "Hex", "ASCII"):
                    table.add_column(column)
                for row in rows:
                    shown = row["hex"] or (row["status"] if row["status"] != "ok" else "")
                    table.add_row(row["module"], row["did"], row["name"] or "", shown, row["ascii"] or "")
                console.print(table)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        diag(args).print("[dim]Stopped.[/dim]")
    except (AdapterError, Elm327Error) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        elm.close()
    if store:
        diag(args).print(f"[dim]Saved samples to {store.db_file}[/dim]")


# --------------------------------------------------------------------------- #
# module map + guarded write/configure (UDS 0x27 / 0x2E / 0x31)
# --------------------------------------------------------------------------- #
_WRITE_WARNING = (
    "[bold red]WRITE OPERATION[/bold red] — this can change module configuration "
    "and is not reversible by this tool. The Security Gateway (SGW) on 2018+ "
    "vehicles will usually block it without a bypass. You accept the risk."
)


def cmd_module_map(args: argparse.Namespace) -> None:
    """Show known module addresses (built-in profiles + optional AlfaOBD DB)."""
    data: dict = {"profiles": {}, "alfaobd": None}
    for bus in ("can-c", "can-ihs"):
        data["profiles"][bus] = [
            {"module": name, "request": "%03X" % request, "response": "%03X" % response}
            for name, request, response in module_addresses(bus)
        ]

    db = AlfaDatabase(path=getattr(args, "alfa_db", None))
    try:
        if db.available:
            data["alfaobd"] = {
                "path": str(db.path),
                "modules": [
                    {
                        "ecu": row["ecu"],
                        "name": row["name"],
                        "bus": row["bus"],
                        "request": None if row["request"] is None else "%03X" % row["request"],
                        "response": None if row["response"] is None else "%03X" % row["response"],
                    }
                    for row in db.pn_modules()
                ],
            }
    finally:
        db.close()

    if args.json:
        jdump(data)
        return

    for bus, rows in data["profiles"].items():
        table = Table(title=f"Known modules on {bus}")
        for column in ("Module", "Request", "Response"):
            table.add_column(column)
        for row in rows:
            table.add_row(row["module"], row["request"], row["response"])
        console.print(table)

    if data["alfaobd"]:
        table = Table(title=f"AlfaOBD PowerNet modules — {data['alfaobd']['path']}")
        for column in ("ECU", "Name", "Bus", "Request", "Response"):
            table.add_column(column)
        for row in data["alfaobd"]["modules"]:
            table.add_row(row["ecu"] or "", row["name"] or "", row["bus"] or "",
                          row["request"] or "", row["response"] or "")
        console.print(table)
    else:
        console.print(
            "[dim]AlfaOBD DB not found; set PACIFICA_OBD_ALFAOBD_DB (or --alfa-db) "
            "for the full ECU map.[/dim]"
        )


def _require_confirm(args: argparse.Namespace, action: str) -> None:
    if not getattr(args, "confirm", False):
        err_console.print(f"[red]Refusing to {action} without --confirm.[/red]")
        sys.exit(2)


def _addr_and_session(elm, args, wait: float) -> tuple[str, str]:
    """Configure the bus, address the module, and require an extended session.

    A write/service request must not be sent if addressing failed or the ECU did
    not confirm an extended session (5003) — otherwise it could land on whatever
    ECU was previously selected.
    """
    configure_bus(elm, args.bus, args.timeout_ms)
    rid = "%03X" % args.module
    must_ok(elm, "ATSH " + rid)
    header, uds = _parse_uds_frame(elm.cmd("1003", wait=wait))
    if header is None or not uds.startswith("5003"):
        raise AdapterError(f"module {rid} did not enter an extended session; refusing")
    return rid, header


def cmd_security(args: argparse.Namespace) -> None:
    """Request a UDS security seed (0x27); optionally send a key from a provider."""
    if args.unlock and not args.confirm:
        err_console.print("[red]--unlock requires --confirm (a wrong key can lock the ECU).[/red]")
        sys.exit(2)
    if args.unlock and not args.provider:
        err_console.print("[red]--unlock requires --provider FILE.[/red]")
        sys.exit(2)
    provider = None
    if args.provider:
        try:
            provider = load_provider(args.provider)
        except SeedKeyError as exc:
            err_console.print(f"[red]{exc}[/red]")
            sys.exit(2)
    seedkey = provider if args.unlock else None

    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    try:
        with open_elm(args) as elm:
            rid, resp = _addr_and_session(elm, args, wait)
            result = _security_access(elm, args.level, seedkey=seedkey, wait=wait, expect_header=resp)
    except (AdapterError, Elm327Error, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    result["module"] = rid
    if args.json:
        jdump(result)
        return
    console.print(f"[bold]Module {rid} · 0x27 level {args.level:02X}[/bold] -> {result['status']}")
    if result.get("seed"):
        console.print(f"seed: [yellow]{result['seed']}[/yellow]")
    if result["unlocked"]:
        console.print("[green]Security access granted.[/green]")
    elif result["status"] == "no_key_provider":
        console.print("[dim]No seed→key provider; ECU left locked. Use --provider FILE --unlock.[/dim]")
    if args.unlock and not result.get("unlocked"):
        sys.exit(3)


def cmd_seedkey_brute(args: argparse.Namespace) -> None:
    """Recover a short per-ECU secret from observed (seed,key) pairs + an algorithm."""
    try:
        algorithm = load_algorithm(args.algorithm)
        pairs = parse_pairs(",".join(args.pair), width=args.width)
        matches = find_secrets(algorithm, pairs, width=args.width)
    except SeedKeyError as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)

    data = {
        "width": args.width,
        "pairs": [{"seed": "%X" % seed, "key": "%X" % key} for seed, key in pairs],
        "candidates": ["%0*X" % (args.width * 2, match) for match in matches],
    }
    if args.json:
        jdump(data)
        return
    if not matches:
        console.print("[yellow]No secret consistent with those pairs.[/yellow]")
    elif len(matches) > 1:
        console.print("[yellow]Ambiguous: multiple secrets fit; supply more pairs.[/yellow]")
    else:
        console.print(f"[green]Recovered {args.width}-byte secret:[/green] {data['candidates'][0]}")


def cmd_write_did(args: argparse.Namespace) -> None:
    """Write a Data Identifier (UDS 0x2E). Guarded and experimental."""
    _require_confirm(args, "write a DID")
    data = args.data.upper()
    if not data or len(data) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", data):
        err_console.print("[red]--data must be non-empty, even-length hex.[/red]")
        sys.exit(2)
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    err_console.print(_WRITE_WARNING)
    try:
        with open_elm(args) as elm:
            rid, resp = _addr_and_session(elm, args, wait)
            result = _write_did(elm, args.did, data, wait=wait, expect_header=resp)
    except (AdapterError, Elm327Error, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    result["module"] = rid
    if args.json:
        jdump(result)
    else:
        console.print(f"[bold]Write DID {result['did']} on {rid}[/bold] -> {result['status']}")


def cmd_routine(args: argparse.Namespace) -> None:
    """Run a UDS routine (0x31, e.g. activations/calibrations). Guarded/experimental."""
    _require_confirm(args, "run a routine")
    data = (args.data or "").upper()
    if data and (len(data) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", data)):
        err_console.print("[red]--data must be even-length hex.[/red]")
        sys.exit(2)
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    err_console.print(_WRITE_WARNING)
    try:
        with open_elm(args) as elm:
            rid, resp = _addr_and_session(elm, args, wait)
            result = _routine_control(elm, args.rid, sub=args.sub, data_hex=data,
                                      wait=wait, expect_header=resp)
    except (AdapterError, Elm327Error, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    result["module"] = rid
    if args.json:
        jdump(result)
    else:
        console.print(f"[bold]Routine {result['rid']} sub {args.sub} on {rid}[/bold] -> {result['status']}")


# --------------------------------------------------------------------------- #
# full vehicle dump (multi-format)
# --------------------------------------------------------------------------- #
def _tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("pacifica-obd")
    except Exception:  # noqa: BLE001
        return "0.1.0"


def _obd_value(conn, command):
    response = conn.query(command)
    return None if response.is_null() else response.value


def _default_dump_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return data_dir() / "dumps" / stamp


def build_vehicle_dump(args: argparse.Namespace) -> dict:
    """Collect everything readable from the vehicle into one dict."""
    dump: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": {"name": "pacifica-obd", "version": _tool_version()},
        "adapter": {},
        "vehicle": {},
        "generic": {},
        "modules": [],
        "readings": [],
        "totals": {},
    }
    vin = normalize_vin(getattr(args, "vin", None))

    if not args.no_generic:
        conn = connect(args)
        try:
            voltage = _obd_value(conn, obd.commands.ELM_VOLTAGE)
            dump["adapter"] = {
                "port": conn.port_name(),
                "protocol": conn.protocol_name(),
                "status": str(conn.status()),
                "identity": _obd_value(conn, obd.commands.ELM_VERSION),
                "voltage": None if voltage is None else str(voltage),
            }
            gvin = _obd_value(conn, obd.commands.VIN)
            if isinstance(gvin, str) and looks_like_vin(gvin):
                vin = vin or gvin

            stored = _codes(conn, obd.commands.GET_DTC)
            pending = (
                _codes(conn, obd.commands.GET_CURRENT_DTC)
                if hasattr(obd.commands, "GET_CURRENT_DTC") else []
            )
            permanent = (
                _codes(conn, obd.commands.GET_PERMANENT_DTC)
                if hasattr(obd.commands, "GET_PERMANENT_DTC") else []
            )

            readiness: dict = {}
            status = conn.query(obd.commands.STATUS)
            if not status.is_null():
                st = status.value
                available = [
                    (name, value) for name, value in st.__dict__.items()
                    if hasattr(value, "available") and hasattr(value, "complete") and value.available
                ]
                readiness = {
                    "ignition_type": st.ignition_type,
                    "mil": bool(st.MIL),
                    "dtc_count": st.DTC_count,
                    "ready": (not st.MIL) and st.DTC_count == 0
                    and not [n for n, v in available if not v.complete],
                    "monitors": [{"name": n, "complete": bool(v.complete)} for n, v in available],
                }

            live = {}
            for name in DEFAULT_WATCH:
                command = getattr(obd.commands, name, None)
                if command is not None:
                    live[name] = _obd_value(conn, command)

            dump["generic"] = {
                "dtcs": {"stored": stored, "pending": pending, "permanent": permanent},
                "readiness": readiness,
                "live": {k: (None if v is None else str(v)) for k, v in live.items()},
                "supported_pids": len(
                    [c for c in conn.supported_commands if not c.command.startswith(b"AT")]
                ),
            }
        finally:
            conn.close()
        time.sleep(0.3)  # let the adapter settle before the UDS pass reopens it

    if not args.no_uds:
        buses = ["can-c", "can-ihs"] if args.bus == "both" else [args.bus]
        wait = max(0.25, args.timeout_ms / 1000 + 0.15)
        elm = open_elm(args)
        try:
            for bus in buses:
                diag(args).print(f"[dim]Dumping {bus} (STP {_BUS_PROTOCOL[bus]})…[/dim]")
                configure_bus(elm, bus, args.timeout_ms)
                for target in _probe_ids(elm, args.start, args.end, "1003", wait):
                    rid = target["request_id"]
                    elm.cmd("ATSH " + rid)
                    expect = _enter_session(elm, rid, wait)
                    _, dtc_uds = _parse_uds_frame(elm.cmd("1902FF", wait=wait), prefer_header=expect)
                    dtcs = (
                        _parse_dtcs(dtc_uds)
                        if dtc_uds and _classify_dtc_response(dtc_uds) == "ok" else []
                    )
                    identification = {}
                    for did in _INFO_DIDS:
                        record = _read_did(elm, did, wait, expect_header=expect)
                        identification[record["did"]] = {
                            "name": record.get("name"), "hex": record.get("hex"),
                            "ascii": record.get("ascii"), "status": record["status"],
                        }
                        dump["readings"].append(
                            {"bus": bus, "module": rid, "did": record["did"], "name": record.get("name"),
                             "hex": record.get("hex"), "ascii": record.get("ascii"), "status": record["status"]}
                        )
                        if did == 0xF190 and record["status"] == "ok" and looks_like_vin(record.get("ascii")):
                            vin = vin or record["ascii"]
                    dump["modules"].append(
                        {"bus": bus, "request_id": rid, "response_id": target.get("response_id"),
                         "kind": target.get("kind"), "dtcs": dtcs, "identification": identification}
                    )
        finally:
            elm.close()

    year = year_from_vin(vin)
    profile = resolve_profile(year=year) if vin else None
    dump["vehicle"] = {
        "vin": vin, "year": year,
        "make": profile.make if profile else None,
        "model": profile.model if profile else None,
        "profile_id": profile.id if profile else None,
        "powertrain": (None if not profile or profile.powertrain == "any" else profile.powertrain),
    }
    generic_dtcs = dump["generic"].get("dtcs", {})
    dtc_total = sum(len(v) for v in generic_dtcs.values()) + sum(
        len(m["dtcs"]) for m in dump["modules"]
    )
    dump["totals"] = {
        "modules": len(dump["modules"]),
        "dtcs": dtc_total,
        "readings": len(dump["readings"]),
    }
    return dump


def cmd_dump(args: argparse.Namespace) -> None:
    """Dump everything readable and write it as JSON, XML and/or an HTML report."""
    if args.end < args.start:
        err_console.print("[red]--end must be >= --start.[/red]")
        sys.exit(2)
    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    dump = build_vehicle_dump(args)

    if args.out:
        out_dir = os.path.expanduser(args.out)
    else:
        out_dir = _default_dump_dir()
        suffix = 1
        while out_dir.exists():
            out_dir = out_dir.with_name(out_dir.name + "-%d" % suffix)
            suffix += 1
    try:
        paths = write_dump(dump, out_dir, formats)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)

    store = _store(args)
    session = _session(args)
    vehicle = dump["vehicle"]
    if store:
        vin = vehicle.get("vin")
        if vin:
            store.upsert_vehicle(
                vin, make=vehicle.get("make"), model=vehicle.get("model"),
                year=vehicle.get("year"), profile_id=vehicle.get("profile_id"),
                powertrain=vehicle.get("powertrain"),
            )
            args.discovered_vin = vin
        for module in dump["modules"]:
            store.add_dtcs(session, vin, module["bus"], module["request_id"], module["dtcs"], None)
            for did, value in module["identification"].items():
                store.add_reading(session, vin, module["bus"], module["request_id"], did,
                                  value.get("name"), value.get("hex"), value.get("ascii"),
                                  value.get("status"), None, category="module-info")
        for code in dump["generic"].get("dtcs", {}).get("stored", []):
            store.add_dtcs(session, vin, "obd", "PCM", [code], None)

    if args.json:
        jdump({"formats": formats, "files": [str(path) for path in paths], "totals": dump["totals"]})
        return
    for path in paths:
        console.print(f"[green]wrote[/green] {path}")
    console.print(
        f"[dim]{dump['totals']['modules']} module(s), {dump['totals']['dtcs']} DTC(s), "
        f"{dump['totals']['readings']} reading(s)[/dim]"
    )


# --------------------------------------------------------------------------- #
# argparse plumbing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pacifica-obd",
        description="OBD-II toolkit for the 2021 Chrysler Pacifica via a vLinker FS adapter.",
        allow_abbrev=False,
    )
    parser.add_argument("--port", help="serial device (default: auto-detect)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="host baud (default 115200)")
    parser.add_argument("--timeout", type=float, default=1.0, help="per-query timeout seconds")
    parser.add_argument(
        "--check-voltage",
        action="store_true",
        help="verify battery voltage during init (leave off when bench-testing)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--db", metavar="PATH",
                        help="SQLite database path (default: XDG data dir)")
    parser.add_argument("--no-log", action="store_true", help="do not persist this run")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ports", help="list candidate serial ports").set_defaults(func=cmd_ports)
    sub.add_parser("info", help="adapter + vehicle info").set_defaults(func=cmd_info)
    sub.add_parser("vin", help="read the VIN").set_defaults(func=cmd_vin)

    p_dtc = sub.add_parser("dtc", help="read (optionally clear) trouble codes")
    p_dtc.add_argument("--clear", action="store_true", help="clear codes after reading")
    p_dtc.add_argument("--force", action="store_true", help="skip the confirmation prompt")
    p_dtc.set_defaults(func=cmd_dtc)

    sub.add_parser("live", help="dump current value of all supported PIDs").set_defaults(func=cmd_live)

    p_watch = sub.add_parser("watch", help="live-poll PIDs")
    p_watch.add_argument("--pids", help="comma-separated PID names (e.g. RPM,SPEED)")
    p_watch.add_argument("--interval", type=float, default=0.5, help="seconds between polls")
    p_watch.add_argument("--csv", metavar="PATH", help="log every sample to a CSV file")
    p_watch.set_defaults(func=cmd_watch)

    sub.add_parser("readiness", help="readiness / I-M monitors (pre-emissions-test)").set_defaults(
        func=cmd_readiness
    )
    sub.add_parser("freeze", help="show the freeze-frame snapshot behind a DTC").set_defaults(
        func=cmd_freeze
    )
    sub.add_parser("scan", help="one-shot VIN + DTC + PID summary").set_defaults(func=cmd_scan)

    p_raw = sub.add_parser("raw", help="send raw AT/OBD/UDS commands to the adapter")
    p_raw.add_argument("commands", nargs="+", help="commands, e.g. ATI 22F190")
    p_raw.add_argument("--header", help="set CAN header first (ATSH), e.g. 7E0")
    p_raw.add_argument("--protocol", help="try protocol first (ATTP, non-saving), e.g. 6 for CAN 11/500")
    p_raw.add_argument("--headers", action="store_true", help="show response headers (ATH1)")
    p_raw.add_argument("--no-auto-format", action="store_true", help="ATCAF0 (raw CAN, no ISO-TP)")
    p_raw.add_argument("--no-flow-control", action="store_true", help="ATCFC0")
    p_raw.add_argument("--allow-long", action="store_true", help="ATAL (allow >7-byte messages)")
    p_raw.set_defaults(func=cmd_raw)

    p_modules = sub.add_parser("modules", help="discover UDS modules on a CAN bus")
    p_modules.add_argument("--bus", choices=["can-c", "can-ihs"], default="can-c",
                           help="can-c = HS 500k (pins 6/14); can-ihs = MS 125k (pins 3/11)")
    p_modules.add_argument("--start", type=_can_id, default=0x700, help="first request ID, hex")
    p_modules.add_argument("--end", type=_can_id, default=0x7FF, help="last request ID, hex")
    p_modules.add_argument("--session", type=_hex_request, default="1003",
                           help="probe request (default 1003)")
    p_modules.add_argument("--identify", action="store_true",
                           help="also read VIN/part number (22F190 / 22F187)")
    p_modules.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                           help="per-request timeout in ms (STPTO, default 100)")
    p_modules.add_argument("--confirm", action="store_true",
                           help="required to probe with a non-read service (e.g. --session 14)")
    p_modules.add_argument("--vin", help="attach stored data to this VIN")
    p_modules.set_defaults(func=cmd_modules)

    p_dtc_all = sub.add_parser("dtc-all", help="read DTCs from every responding module (UDS 0x19)")
    p_dtc_all.add_argument("--bus", choices=["can-c", "can-ihs", "both"], default="can-c")
    p_dtc_all.add_argument("--start", type=_can_id, default=0x700, help="first request ID, hex")
    p_dtc_all.add_argument("--end", type=_can_id, default=0x7FF, help="last request ID, hex")
    p_dtc_all.add_argument("--module", type=_can_id, default=None,
                           help="query one request ID (e.g. 7E0) instead of scanning")
    p_dtc_all.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                           help="per-request timeout in ms (STPTO, default 100)")
    p_dtc_all.add_argument("--vin", help="attach stored data to this VIN")
    p_dtc_all.set_defaults(func=cmd_dtc_all)

    p_read_did = sub.add_parser("read-did", help="read Data Identifiers (UDS 0x22) from a module")
    p_read_did.add_argument("dids", nargs="+", type=_did, help="DIDs, e.g. F190 F187")
    p_read_did.add_argument("--module", type=_can_id, required=True, help="request ID, e.g. 7E0")
    p_read_did.add_argument("--bus", choices=["can-c", "can-ihs"], default="can-c")
    p_read_did.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                            help="per-request timeout in ms (STPTO, default 100)")
    p_read_did.add_argument("--vin", help="attach stored data to this VIN")
    p_read_did.set_defaults(func=cmd_read_did)

    p_module_info = sub.add_parser(
        "module-info", help="read a module's identification block (F186..F191)"
    )
    p_module_info.add_argument("--module", type=_can_id, default=None,
                               help="one request ID; omit to scan the range")
    p_module_info.add_argument("--bus", choices=["can-c", "can-ihs", "both"], default="can-c")
    p_module_info.add_argument("--start", type=_can_id, default=0x700, help="first request ID, hex")
    p_module_info.add_argument("--end", type=_can_id, default=0x7FF, help="last request ID, hex")
    p_module_info.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                               help="per-request timeout in ms (STPTO, default 100)")
    p_module_info.add_argument("--vin", help="attach stored data to this VIN")
    p_module_info.set_defaults(func=cmd_module_info)

    p_capture = sub.add_parser("capture", help="full snapshot: modules + DTCs + ID block, stored")
    p_capture.add_argument("--bus", choices=["can-c", "can-ihs", "both"], default="can-c")
    p_capture.add_argument("--start", type=_can_id, default=0x700, help="first request ID, hex")
    p_capture.add_argument("--end", type=_can_id, default=0x7FF, help="last request ID, hex")
    p_capture.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                           help="per-request timeout in ms (STPTO, default 100)")
    p_capture.add_argument("--vin", help="attach stored data to this VIN")
    p_capture.set_defaults(func=cmd_capture)

    p_history = sub.add_parser("history", help="show stored vehicles and sessions")
    p_history.add_argument("--limit", type=int, default=25, help="max sessions to list")
    p_history.set_defaults(func=cmd_history)

    p_show = sub.add_parser("show", help="show everything stored for a VIN")
    p_show.add_argument("--vin", required=True, help="vehicle identification number")
    p_show.set_defaults(func=cmd_show)

    sub.add_parser("tui", help="launch the interactive Textual UI").set_defaults(func=cmd_tui)

    p_vehicles = sub.add_parser("vehicles", help="list vehicles with stored records")
    p_vehicles.set_defaults(func=cmd_vehicles)

    p_monitor = sub.add_parser("monitor", help="poll UDS DIDs on modules and store samples")
    p_monitor.add_argument("--module", type=_can_id, default=None, help="one request ID; omit to scan")
    p_monitor.add_argument("--bus", choices=["can-c", "can-ihs"], default="can-c")
    p_monitor.add_argument("--start", type=_can_id, default=0x700, help="first request ID, hex")
    p_monitor.add_argument("--end", type=_can_id, default=0x7FF, help="last request ID, hex")
    p_monitor.add_argument("--did", dest="dids", type=_did, nargs="+", action="append", required=True,
                           help="DID(s) to poll (repeatable), e.g. --did F190 F187")
    p_monitor.add_argument("--interval", type=_positive_interval, default=1.0, help="seconds between polls")
    p_monitor.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                           help="per-request timeout in ms (STPTO, default 100)")
    p_monitor.add_argument("--vin", help="attach stored samples to this VIN")
    p_monitor.set_defaults(func=cmd_monitor)

    p_map = sub.add_parser("module-map", help="show known module addresses (read-only)")
    p_map.add_argument("--alfa-db", metavar="PATH", help="path to the AlfaOBD SQLite DB (optional)")
    p_map.set_defaults(func=cmd_module_map)

    p_sec = sub.add_parser("security", help="request a UDS 0x27 security seed from a module")
    p_sec.add_argument("--module", type=_can_id, required=True, help="request ID, e.g. 7E0")
    p_sec.add_argument("--level", type=int, default=1, help="security level (odd), default 1")
    p_sec.add_argument("--provider", metavar="FILE", help="Python file exposing key_for(seed, **ctx) -> int")
    p_sec.add_argument("--unlock", action="store_true", help="also send the key (requires --confirm)")
    p_sec.add_argument("--confirm", action="store_true", help="acknowledge sending a key to the ECU")
    p_sec.add_argument("--bus", choices=["can-c", "can-ihs"], default="can-c")
    p_sec.add_argument("--timeout-ms", type=_timeout_ms, default=100)
    p_sec.set_defaults(func=cmd_security)

    p_sk = sub.add_parser("seedkey-brute", help="recover a short per-ECU secret from seed/key pairs")
    p_sk.add_argument("--pair", action="append", required=True, help="SEED:KEY in hex (repeatable)")
    p_sk.add_argument("--algorithm", required=True, metavar="FILE",
                      help="Python file exposing algorithm(seed, secret) -> key")
    p_sk.add_argument("--width", type=_width, default=2, help="secret width in bytes, 1..4 (default 2)")
    p_sk.set_defaults(func=cmd_seedkey_brute)

    p_dump = sub.add_parser("dump", help="dump everything readable to json/xml/html")
    p_dump.add_argument("--bus", choices=["can-c", "can-ihs", "both"], default="both")
    p_dump.add_argument("--start", type=_can_id, default=0x700, help="first request ID, hex")
    p_dump.add_argument("--end", type=_can_id, default=0x7FF, help="last request ID, hex")
    p_dump.add_argument("--timeout-ms", type=_timeout_ms, default=100)
    p_dump.add_argument("--formats", default="json,xml,html", help="comma list: json,xml,html")
    p_dump.add_argument("--out", metavar="DIR", help="output dir (default: data dir / dumps / <timestamp>)")
    p_dump.add_argument("--no-generic", action="store_true", help="skip the generic OBD-II pass")
    p_dump.add_argument("--no-uds", action="store_true", help="skip the UDS module pass")
    p_dump.add_argument("--vin", type=_vin_arg, help="tag the dump with this VIN")
    p_dump.set_defaults(func=cmd_dump)

    p_wd = sub.add_parser("write-did", help="write a DID, UDS 0x2E (guarded/experimental)")
    p_wd.add_argument("--module", type=_can_id, required=True)
    p_wd.add_argument("--did", type=_did, required=True)
    p_wd.add_argument("--data", required=True, help="hex payload, e.g. 01")
    p_wd.add_argument("--confirm", action="store_true", help="required acknowledgement")
    p_wd.add_argument("--bus", choices=["can-c", "can-ihs"], default="can-c")
    p_wd.add_argument("--timeout-ms", type=_timeout_ms, default=100)
    p_wd.set_defaults(func=cmd_write_did)

    p_rt = sub.add_parser("routine", help="run a UDS routine, 0x31 (guarded/experimental)")
    p_rt.add_argument("--module", type=_can_id, required=True)
    p_rt.add_argument("--rid", type=_did, required=True, help="routine identifier (4 hex)")
    p_rt.add_argument("--sub", type=int, default=1, choices=[1, 2, 3], help="1 start, 2 stop, 3 results")
    p_rt.add_argument("--data", default="", help="hex payload (optional)")
    p_rt.add_argument("--confirm", action="store_true", help="required acknowledgement")
    p_rt.add_argument("--bus", choices=["can-c", "can-ihs"], default="can-c")
    p_rt.add_argument("--timeout-ms", type=_timeout_ms, default=100)
    p_rt.set_defaults(func=cmd_routine)

    # Allow --json after the subcommand too, without clobbering the global value.
    for sp in sub.choices.values():
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    store = None
    read_only_command = args.command in ("history", "show", "vehicles")
    if logging_enabled(flag_disabled=getattr(args, "no_log", False)) and not read_only_command:
        try:
            store = Store(db_file=getattr(args, "db", None) or None)
        except Exception:  # noqa: BLE001 - never let logging break a command
            store = None
        if store is not None and not store.available:
            diag(args).print(
                "[yellow]Warning: could not open the log database; continuing without persistence.[/yellow]"
            )
            store = None
    args.store = store
    args.discovered_vin = normalize_vin(getattr(args, "vin", None))
    args.discovered_powertrain = None

    payload = {
        key: value
        for key, value in vars(args).items()
        if key not in ("func", "store") and isinstance(value, (str, int, float, bool, list, type(None)))
    }
    session_id = None
    if store is not None:
        try:
            session_id = store.start_session(args.command, payload, getattr(args, "port", None))
        except Exception:  # noqa: BLE001
            session_id = None
    args.session_id = session_id

    status = "ok"
    try:
        args.func(args)
    except SystemExit as exc:
        status = f"exit {exc.code}"
        raise
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        if store is not None:
            vin = getattr(args, "discovered_vin", None)
            if vin:
                try:
                    store.upsert_vehicle(
                        vin, year=year_from_vin(vin),
                        powertrain=getattr(args, "discovered_powertrain", None),
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                store.finish_session(session_id, status, vin)
            except Exception:  # noqa: BLE001
                pass
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
