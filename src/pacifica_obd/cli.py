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

from pacifica_obd.elm import Elm327, Elm327Error
from pacifica_obd.store import Store, logging_enabled

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
def open_elm(args: argparse.Namespace) -> Elm327:
    port = find_port(args.port)
    if not port:
        err_console.print("[bold red]No serial port found.[/bold red]")
        sys.exit(2)
    try:
        return Elm327(port, args.baud, timeout=args.timeout)
    except Elm327Error as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)


def must_ok(elm: Elm327, command: str) -> None:
    """Run a configuration command and require an 'OK' acknowledgement."""
    response = elm.cmd(command)
    if not any("OK" in line.upper() for line in response):
        raise Elm327Error(f"{command!r} rejected by adapter: {response!r}")


def configure_bus(elm: Elm327, bus: str, timeout_ms: int) -> int:
    protocol = _BUS_PROTOCOL[bus]
    must_ok(elm, f"STP {protocol}")
    must_ok(elm, "ATH1")
    must_ok(elm, "ATCAF1")
    must_ok(elm, "ATAL")
    must_ok(elm, f"STPTO {timeout_ms}")
    return protocol


def current_protocol(elm: Elm327):
    """Snapshot the adapter's protocol as ('auto', None) | ('at', n) | ('st', n) | None."""
    strings = elm.cmd("STPRS")
    numbers = elm.cmd("STPR")
    strings_line = (strings[0] if strings else "").upper()
    if "AUTO" in strings_line:
        return ("auto", None)
    number_line = (numbers[0] if numbers else "").upper()
    token = number_line.split()[0] if number_line else ""
    if not re.fullmatch(r"[0-9A-Fa-f]{1,2}", token):
        return None
    return ("at" if "[AT]" in number_line else "st", token)


def restore_protocol(elm: Elm327, previous) -> None:
    """Restore the snapshot without writing adapter EEPROM."""
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


def _did(text) -> int:
    value = str(text).upper().removeprefix("0X")
    if not re.fullmatch(r"[0-9A-Fa-f]{4}", value):
        raise argparse.ArgumentTypeError(f"not a 4-hex DID: {text!r}")
    return int(value, 16)


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
    except Elm327Error as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)

    results = []
    try:
        if args.header:
            elm.cmd("ATSH " + args.header)
        if args.protocol:
            elm.cmd("ATSP" + args.protocol)
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
# STN/ELM protocol presets: 33 = ISO 15765 11-bit 500k (HS/CAN-C),
# 53 = ISO 15765 11-bit 125k (MS/CAN-IHS).
_BUS_PROTOCOL = {"can-c": 33, "can-ihs": 53}


_HEADER_LEN = 3  # 11-bit CAN; our STP presets (33/53) are 11-bit


def _collect_frames(lines: list[str], header_len: int = _HEADER_LEN) -> list[tuple[str, list[bytes]]]:
    """Group response lines by CAN header, preserving arrival order.

    Handles both spaced ("7E8 06 50 03") and compact ("7E8065003") formats.
    """
    frames: dict[str, list[bytes]] = {}
    order: list[str] = []
    for line in lines:
        tokens = line.split()
        if len(tokens) >= 2:
            header, data = tokens[0], "".join(tokens[1:])
        else:
            token = tokens[0] if tokens else ""
            if len(token) <= header_len or not re.fullmatch(r"[0-9A-Fa-f]+", token):
                continue
            header, data = token[:header_len], token[header_len:]
        header = header.upper()
        if not re.fullmatch(r"[0-9A-Fa-f]{3}|[0-9A-Fa-f]{8}", header):
            continue
        if not re.fullmatch(r"[0-9A-Fa-f]+", data) or len(data) % 2:
            continue
        if len(data) > 16:  # classic CAN carries at most 8 bytes per frame
            continue
        if header not in frames:
            frames[header] = []
            order.append(header)
        frames[header].append(bytes.fromhex(data))
    return [(h, frames[h]) for h in order]


def _messages_from_frames(frames: list[bytes]) -> list[str]:
    """Split a frame list into one or more complete, validated UDS payloads."""
    messages: list[str] = []
    i = 0
    n = len(frames)
    while i < n:
        first = frames[i]
        i += 1
        if not first or len(first) > 8:  # classic CAN: at most 8 bytes/frame
            continue
        pci = first[0] >> 4
        if pci == 0:  # single frame: length must be 1..7
            length = first[0] & 0x0F
            if not 1 <= length <= 7:
                continue
            data = first[1:1 + length]
            if len(data) == length:
                messages.append(data.hex().upper())
        elif pci == 1:  # first frame: always a full 8-byte classic-CAN frame
            if len(first) != 8:
                continue
            length = ((first[0] & 0x0F) << 8) | first[1]
            if not 8 <= length <= 0xFFF:
                continue
            payload = bytearray(first[2:])
            expected = 1
            while i < n and len(payload) < length:
                cf = frames[i]
                if not cf:
                    i += 1
                    continue
                if len(cf) > 8 or (cf[0] >> 4) != 2 or (cf[0] & 0x0F) != expected:
                    break  # invalid / missing / duplicated / reordered -> abandon
                if len(cf) < 8 and len(payload) + (len(cf) - 1) < length:
                    break  # a non-final consecutive frame must be full
                payload += cf[1:]
                expected = (expected + 1) & 0x0F
                i += 1
            if len(payload) >= length:
                messages.append(bytes(payload[:length]).hex().upper())
        # stray consecutive/flow-control frames are ignored
    return messages


def _parse_uds_frame(lines: list[str]) -> tuple[str | None, str | None]:
    """Return (response_header, uds_hex), reassembling ISO-TP if needed.

    Prefers the last non-responsePending (NRC 0x78) message so a pending
    acknowledgement does not mask the real answer.
    """
    for header, frames in _collect_frames(lines):
        messages = _messages_from_frames(frames)
        if not messages:
            continue
        final = [
            m
            for m in messages
            if not (m.startswith("7F") and len(m) >= 6 and m[4:6] == "78")
        ]
        return header, (final[-1] if final else messages[-1])
    return None, None


def _ascii_payload(uds_hex: str, prefix: str) -> str | None:
    if not uds_hex.startswith(prefix):
        return None
    body = uds_hex[len(prefix):]
    if not body or len(body) % 2:
        return None
    try:
        raw = bytes.fromhex(body)
    except ValueError:
        return None
    if any(byte < 0x20 or byte > 0x7E for byte in raw):  # reject non-printable
        return None
    return raw.decode("ascii").strip() or None


def _probe_ids(elm, start: int, end: int, probe: str, wait: float):
    """Yield responding module IDs by sending `probe` to each request ID."""
    service = int(probe[:2], 16)
    for rid in range(start, end + 1):
        elm.cmd("ATSH %03X" % rid)
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


def cmd_modules(args: argparse.Namespace) -> None:
    """Probe a CAN bus for UDS-responding modules."""
    start, end = args.start, args.end  # validated 11-bit ints (see _can_id)
    if end < start:
        err_console.print("[red]--end must be >= --start.[/red]")
        sys.exit(2)

    protocol = _BUS_PROTOCOL[args.bus]
    diag(args).print(f"[dim]Scanning {args.bus} (STP {protocol}) {start:03X}–{end:03X}…[/dim]")

    elm = open_elm(args)
    previous = current_protocol(elm)
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
    except Elm327Error as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        restore_protocol(elm, previous)
        elm.close()

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
_DTC_STATUS_BITS = [
    (0x01, "testFailed"),
    (0x02, "testFailedThisCycle"),
    (0x04, "pending"),
    (0x08, "confirmed"),
    (0x10, "testNotCompletedSinceClear"),
    (0x20, "testFailedSinceClear"),
    (0x40, "testNotCompletedThisCycle"),
    (0x80, "warningLamp"),
]


def _decode_dtc(b0: int, b1: int, b2: int) -> tuple[str, int]:
    """Decode a 3-byte UDS DTC into (e.g. 'P0300', failure_type_byte)."""
    letter = "PCBU"[(b0 >> 6) & 0x03]
    number = ((b0 & 0x3F) << 8) | b1
    return f"{letter}{number:04X}", b2


def _classify_dtc_response(uds_hex: str) -> str:
    """Classify a 0x19 0x02 reply: ok | negative | unexpected | malformed."""
    if uds_hex.startswith("7F"):
        return "negative"
    if not uds_hex.startswith("5902"):
        return "unexpected"
    body = uds_hex[4:]
    if len(body) % 2:
        return "malformed"
    nbytes = len(body) // 2
    if nbytes == 0 or (nbytes - 1) % 4 != 0:  # mask byte + N*4-byte records
        return "malformed"
    return "ok"


def _parse_dtcs(uds_hex: str) -> list[dict]:
    """Parse a well-formed 0x59 0x02 response: 59 02 <mask> (<dtc3><status>)*."""
    if _classify_dtc_response(uds_hex) != "ok":
        return []
    records = bytes.fromhex(uds_hex[4:])[1:]  # skip the status-availability mask
    dtcs: list[dict] = []
    for i in range(0, len(records), 4):
        b0, b1, b2, status = records[i:i + 4]
        code, failure_type = _decode_dtc(b0, b1, b2)
        dtcs.append(
            {
                "code": code,
                "failure_type": "%02X" % failure_type,
                "status": "%02X" % status,
                "flags": [name for bit, name in _DTC_STATUS_BITS if status & bit],
            }
        )
    return dtcs


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
    previous = current_protocol(elm)

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
    except Elm327Error as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        restore_protocol(elm, previous)
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
_DID_NAMES = {
    0xF186: "Active diagnostic session",
    0xF187: "Spare part number",
    0xF188: "ECU software number",
    0xF189: "ECU software version",
    0xF18A: "System supplier identifier",
    0xF18C: "ECU serial number",
    0xF190: "VIN",
    0xF191: "ECU hardware number",
}
_INFO_DIDS = list(_DID_NAMES)


def _read_did(elm: Elm327, did: int, wait: float, expect_header: str | None = None) -> dict:
    request = "22%04X" % did
    lines = elm.cmd(request, wait=wait)
    header, uds = _parse_uds_frame(lines)
    record = {
        "did": "%04X" % did,
        "name": _DID_NAMES.get(did),
        "response_id": header,
        "raw": uds,
    }
    if header is None:
        record["status"] = "can_error" if any(
            "CAN ERROR" in line.upper() or "BUFFER FULL" in line.upper() for line in lines
        ) else "no_response"
        record["raw_lines"] = lines
        return record
    if expect_header and header != expect_header:
        record["status"] = "wrong_responder"  # a different ECU answered
        return record
    if uds.startswith("7F"):
        if len(uds) >= 6 and uds[2:4] == "22":
            record["status"] = "pending" if uds[4:6] == "78" else "negative"
        else:
            record["status"] = "malformed"
        return record
    prefix = "62%04X" % did
    if not uds.startswith(prefix):
        record["status"] = "unexpected"
        return record
    data_hex = uds[len(prefix):]
    if not data_hex:
        record["status"] = "malformed"
        return record
    record["status"] = "ok"
    record["hex"] = data_hex
    record["ascii"] = _ascii_payload(uds, prefix)
    return record


_DID_STATUS_TEXT = {
    "no_response": "no response",
    "can_error": "CAN error",
    "negative": "negative response",
    "pending": "pending (no final response)",
    "unexpected": "unexpected response",
    "wrong_responder": "wrong responder",
    "malformed": "malformed data",
}


def _did_value(record: dict) -> str:
    if record["status"] != "ok":
        return _DID_STATUS_TEXT.get(record["status"], record["status"])
    return record.get("hex", "")


def _did_ascii(record: dict) -> str:
    return (record.get("ascii") or "") if record["status"] == "ok" else ""


def _enter_session(elm: Elm327, rid: str, wait: float) -> str | None:
    """Send 0x10 0x03; return the ECU's response header and filter RX to it."""
    lines = elm.cmd("1003", wait=wait)
    header, _ = _parse_uds_frame(lines)
    if header:
        elm.cmd("ATCRA " + header)
    return header


def cmd_read_did(args: argparse.Namespace) -> None:
    """Read Data Identifiers (UDS 0x22) from a single module."""
    wait = max(0.25, args.timeout_ms / 1000 + 0.15)
    elm = open_elm(args)
    previous = current_protocol(elm)
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
    except Elm327Error as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        restore_protocol(elm, previous)
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
    previous = current_protocol(elm)
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
    except Elm327Error as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        restore_protocol(elm, previous)
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
    previous = current_protocol(elm)
    store = _store(args)
    session = _session(args)
    vin = None
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
                    if did == 0xF190 and record["status"] == "ok" and record.get("ascii"):
                        module_vin = record["ascii"]
                        if vin is None:
                            vin = module_vin
                            args.discovered_vin = vin  # record even if we stop early
                            if store:
                                store.upsert_vehicle(vin)
                                store.backfill_vin(session, vin)
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
    except Elm327Error as exc:
        err_console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    finally:
        restore_protocol(elm, previous)
        elm.close()

    if vin and store:
        store.upsert_vehicle(vin)
        args.discovered_vin = vin
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


# --------------------------------------------------------------------------- #
# argparse plumbing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pacifica-obd",
        description="OBD-II toolkit for the 2021 Chrysler Pacifica via a vLinker FS adapter.",
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
    p_raw.add_argument("--protocol", help="pin protocol first (ATSP), e.g. 6 for CAN 11/500")
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
    p_modules.set_defaults(func=cmd_modules)

    p_dtc_all = sub.add_parser("dtc-all", help="read DTCs from every responding module (UDS 0x19)")
    p_dtc_all.add_argument("--bus", choices=["can-c", "can-ihs", "both"], default="can-c")
    p_dtc_all.add_argument("--start", type=_can_id, default=0x700, help="first request ID, hex")
    p_dtc_all.add_argument("--end", type=_can_id, default=0x7FF, help="last request ID, hex")
    p_dtc_all.add_argument("--module", type=_can_id, default=None,
                           help="query one request ID (e.g. 7E0) instead of scanning")
    p_dtc_all.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                           help="per-request timeout in ms (STPTO, default 100)")
    p_dtc_all.set_defaults(func=cmd_dtc_all)

    p_read_did = sub.add_parser("read-did", help="read Data Identifiers (UDS 0x22) from a module")
    p_read_did.add_argument("dids", nargs="+", type=_did, help="DIDs, e.g. F190 F187")
    p_read_did.add_argument("--module", type=_can_id, required=True, help="request ID, e.g. 7E0")
    p_read_did.add_argument("--bus", choices=["can-c", "can-ihs"], default="can-c")
    p_read_did.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                            help="per-request timeout in ms (STPTO, default 100)")
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
    p_module_info.set_defaults(func=cmd_module_info)

    p_capture = sub.add_parser("capture", help="full snapshot: modules + DTCs + ID block, stored")
    p_capture.add_argument("--bus", choices=["can-c", "can-ihs", "both"], default="can-c")
    p_capture.add_argument("--start", type=_can_id, default=0x700, help="first request ID, hex")
    p_capture.add_argument("--end", type=_can_id, default=0x7FF, help="last request ID, hex")
    p_capture.add_argument("--timeout-ms", type=_timeout_ms, default=100,
                           help="per-request timeout in ms (STPTO, default 100)")
    p_capture.set_defaults(func=cmd_capture)

    p_history = sub.add_parser("history", help="show stored vehicles and sessions")
    p_history.add_argument("--limit", type=int, default=25, help="max sessions to list")
    p_history.set_defaults(func=cmd_history)

    p_show = sub.add_parser("show", help="show everything stored for a VIN")
    p_show.add_argument("--vin", required=True, help="vehicle identification number")
    p_show.set_defaults(func=cmd_show)

    # Allow --json after the subcommand too, without clobbering the global value.
    for sp in sub.choices.values():
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    store = None
    read_only_command = args.command in ("history", "show")
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
    args.discovered_vin = None

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
            try:
                store.finish_session(session_id, status, getattr(args, "discovered_vin", None))
            except Exception:  # noqa: BLE001
                pass
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
