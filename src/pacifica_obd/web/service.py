"""Shared read-only operation layer for every pacifica-obd front end.

This module is the single home for the *logic* the CLI and the TUI used to
re-implement (discovery loops, per-module DTC reads, DID reads, the
identification block, the full capture, and the generic OBD-II reads). It is
deliberately pure-ish: **no printing, no argparse, no FastAPI**. A front end
passes in an adapter-like object exposing ``cmd(command, wait=...) -> list[str]``
(the device layer's ``ElmAdapter``/``Elm327``, or a test fake) and gets plain
dicts back.

Safety rules carried from ``AGENTS.md`` that every function here honours:

* Reads set an ``ATCRA`` receive filter pointing at the *responder* — never
  ``configure_flow_control()``. Manual flow control switches the STN into an
  indexed frame format that breaks multi-frame parsing.
* ``configure_bus()`` is used for bus/preset selection (``STP``, never ``ATSP``).
* Every loop is cancellable through an optional ``threading.Event`` so a browser
  ``Stop`` (or the CLI's Ctrl-C / the TUI's ``s``) can interrupt a scan.

The functions return the same shapes as the CLI's ``--json`` output, so a web
``GET`` can be compared line-for-line with a terminal run.
"""

from __future__ import annotations

import re
import threading
from typing import Callable, Iterable, Iterator

from pacifica_obd.adapter import AdapterError, configure_bus
from pacifica_obd.elm import Elm327Error
from pacifica_obd.profiles import (
    detect_powertrain,
    looks_like_vin,
    response_id as module_response_id,
)
from pacifica_obd.uds import (
    BUS_PROTOCOL,
    INFO_DIDS,
    ascii_payload,
    classify_dtc_response,
    enter_session,
    parse_dtcs,
    parse_uds_frame,
    read_did,
)

__all__ = [
    "BUS_PROTOCOL",
    "INFO_DIDS",
    "read_wait",
    "parse_can_id",
    "parse_did",
    "parse_hex_request",
    "parse_timeout_ms",
    "parse_interval",
    "probe_modules",
    "discover_modules",
    "read_module_dtcs",
    "read_dids",
    "module_info",
    "capture_snapshot",
    "obd_dtcs",
    "obd_live",
    "obd_pids",
    "obd_readiness",
    "obd_freeze",
    "obd_info",
    "obd_vin",
    "uds_vin",
    "dtc_description",
    "enrich_dtcs",
    "to_jsonable",
    "jsonable",
    "is_read_only_command",
    "address_module",
    "command_text",
]

Cancel = threading.Event | None
Progress = Callable[[dict], None] | None

# Default probe services, matching the CLI: many modules reject session control
# (10 03) but answer reads (22 F100 / 22 F190), and vice versa.
DEFAULT_PROBES = ("1003", "22F100", "22F190")
DEFAULT_DTC_PROBES = ("1003", "22F100", "22F190")


# --------------------------------------------------------------------------- #
# small helpers (validation mirrors the CLI's argparse types exactly)
# --------------------------------------------------------------------------- #
def read_wait(timeout_ms: int = 100) -> float:
    """Per-request read wait used by the CLI for UDS probes/DID reads."""
    return max(0.25, timeout_ms / 1000 + 0.15)


def parse_can_id(text) -> int:
    """An 11-bit hex CAN ID (mirrors ``cli._can_id``)."""
    try:
        value = int(str(text), 16)
    except (TypeError, ValueError):
        raise ValueError(f"not a hex CAN ID: {text!r}")
    if not 0 <= value <= 0x7FF:
        raise ValueError(f"CAN ID out of 11-bit range: {text!r}")
    return value


def parse_hex_request(text) -> str:
    """An uppercase, even-length hex service request (mirrors ``cli._hex_request``)."""
    value = str(text).upper()
    if len(value) < 2 or len(value) % 2 or not re.fullmatch(r"[0-9A-F]+", value):
        raise ValueError(f"not a hex request: {text!r}")
    return value


def parse_timeout_ms(text) -> int:
    """A per-request timeout in ms, 1..65535 (mirrors ``cli._timeout_ms``)."""
    try:
        value = int(str(text))
    except (TypeError, ValueError):
        raise ValueError(f"not an integer: {text!r}")
    if not 1 <= value <= 65535:
        raise ValueError("timeout must be 1..65535 ms")
    return value


def parse_did(text) -> int:
    """A 4-hex data identifier, optional ``0x`` (mirrors ``cli._did``)."""
    value = str(text).upper().removeprefix("0X")
    if not re.fullmatch(r"[0-9A-F]{4}", value):
        raise ValueError(f"not a 4-hex DID: {text!r}")
    return int(value, 16)


def parse_interval(text) -> float:
    """A finite positive interval in seconds (mirrors ``cli._positive_interval``)."""
    try:
        value = float(str(text))
    except (TypeError, ValueError):
        raise ValueError(f"not a number: {text!r}")
    if not value > 0 or value != value or value in (float("inf"), float("-inf")):
        raise ValueError("interval must be a finite positive number")
    return value


def as_request_id(value) -> str:
    """Normalise a module id (int, ``"0x7E0"`` or ``"7E0"``) to ``"7E0"``."""
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip().upper()
        text = text[2:] if text.startswith("0X") else text
        number = int(text, 16)
    if not 0 <= number <= 0x7FF:
        raise ValueError(f"CAN ID out of 11-bit range: {value!r}")
    return "%03X" % number


def to_jsonable(value):
    """pint Quantities become ``{value, unit}`` (mirrors ``cli.to_jsonable``)."""
    if hasattr(value, "magnitude") and hasattr(value, "units"):
        try:
            return {"value": float(value.magnitude), "unit": str(value.units)}
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode(errors="replace")
    return str(value)


def jsonable(value):
    """Recursively convert a value into something ``json.dumps`` can encode."""
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return to_jsonable(value)


def _acknowledged(lines: Iterable[str]) -> bool:
    return any("OK" in line.upper() for line in lines)


def _cancelled(cancel: Cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _note(progress: Progress, event: dict) -> None:
    if progress is not None:
        progress(event)


# --------------------------------------------------------------------------- #
# the read-only command allowlist lives in pacifica_obd.obd_guard (shared with
# the CLI and the TUI, so the rule cannot drift between front ends)
# --------------------------------------------------------------------------- #
from pacifica_obd.obd_guard import (  # noqa: E402 - keep the guard imports obvious
    REFUSED_COMMANDS,
    command_text,
    is_read_only_command,
)


def address_module(adapter, request_id) -> tuple[str | None, bool]:
    """Aim the adapter at one module: the receive filter *first*, then the header.

    Non-standard modules answer at ``request - 0x280``, and the adapter needs
    ``ATCRA`` pointed at the responder so it can reassemble a multi-frame reply.
    A filter left over from the previous module would silently hide this one, so
    it is always re-set **and its acknowledgement is required**: a refused filter
    is cleared (``ATAR``) and reported as not-addressable, because reading a
    module through someone else's filter would silently lose every reply.

    Returns ``(expected_response_id_or_None, addressable)``.
    """
    mapped: str | None = None
    try:
        mapped = "%03X" % module_response_id(int(str(request_id), 16))
    except (TypeError, ValueError):
        mapped = None
    if mapped:
        filter_ok = _acknowledged(adapter.cmd("ATCRA " + mapped))
        if not filter_ok:
            adapter.cmd("ATAR")  # clear rather than keep a stale responder filter
    else:
        filter_ok = _acknowledged(adapter.cmd("ATAR"))
    header_ok = _acknowledged(adapter.cmd("ATSH " + str(request_id)))
    return mapped, bool(filter_ok and header_ok)


# --------------------------------------------------------------------------- #
# UDS discovery
# --------------------------------------------------------------------------- #
def probe_modules(
    adapter,
    *,
    start: int,
    end: int,
    probes: Iterable[str] = DEFAULT_PROBES,
    wait: float = 0.5,
    cancel: Cancel = None,
    receive: Callable[[int], int] | None = module_response_id,
    progress: Progress = None,
) -> Iterator[dict]:
    """Cancellable multi-service module probe (the TUI's ``_probe_modules_multi``).

    Yields one dict per responding module. ``receive(rid)`` returns the address to
    accept replies from; when it raises the filter is cleared (``ATAR``) so a
    broadcast/no-mapping id still works. An id whose ``ATSH`` is rejected is
    skipped rather than mis-attributed to the previously addressed module.
    """
    known: set[int] = set()
    ignored: set[int] = set()
    for probe in probes:
        service = int(probe[:2], 16)
        for rid in range(start, end + 1):
            if _cancelled(cancel):
                return
            if rid in known:
                continue
            _note(progress, {"kind": "probe", "request_id": "%03X" % rid, "service": probe})
            if not _acknowledged(adapter.cmd("ATSH %03X" % rid)):
                known.add(rid)
                continue
            mapped: str | None = None
            if receive is not None:
                try:
                    mapped = "%03X" % receive(rid)
                    if not _acknowledged(adapter.cmd("ATCRA " + mapped)):
                        adapter.cmd("ATAR")  # never probe through a stale filter
                        mapped = None
                except Exception:  # noqa: BLE001 - no mapping (e.g. broadcast)
                    adapter.cmd("ATAR")
                    mapped = None
            header, uds = parse_uds_frame(adapter.cmd(probe, wait=wait))
            if header is None:
                continue
            if mapped is not None and header != mapped:
                # Someone else answered; yielding it would outrank the mapping.
                ignored.add(rid)
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


def discover_modules(
    adapter,
    *,
    bus: str = "can-c",
    start: int = 0x700,
    end: int = 0x7FF,
    timeout_ms: int = 100,
    identify: bool = False,
    probes: Iterable[str] = DEFAULT_PROBES,
    cancel: Cancel = None,
    progress: Progress = None,
) -> dict:
    """Probe a CAN bus for UDS modules (parity with ``pacifica-obd modules``)."""
    if bus not in BUS_PROTOCOL:
        raise ValueError(f"unknown bus: {bus!r}")
    if end < start:
        raise ValueError("end must be >= start")
    wait = read_wait(timeout_ms)
    protocol = configure_bus(adapter, bus, timeout_ms)
    found: list[dict] = []
    for entry in probe_modules(
        adapter, start=start, end=end, probes=probes, wait=wait,
        cancel=cancel, progress=progress,
    ):
        if identify:
            adapter.cmd("ATSH " + entry["request_id"])
            _, vin_uds = parse_uds_frame(adapter.cmd("22F190", wait=wait))
            if vin_uds:
                vin = ascii_payload(vin_uds, "62F190")
                if vin:
                    entry["vin"] = vin
            _, part_uds = parse_uds_frame(adapter.cmd("22F187", wait=wait))
            if part_uds:
                part = ascii_payload(part_uds, "62F187")
                if part:
                    entry["part_number"] = part
        found.append(entry)
        _note(progress, {"kind": "found", "request_id": entry["request_id"],
                         "count": len(found)})
    return {
        "bus": bus,
        "protocol": protocol,
        "range": ["%03X" % start, "%03X" % end],
        "modules": found,
        "powertrain": detect_powertrain({entry["request_id"] for entry in found}),
        "cancelled": _cancelled(cancel),
    }


# --------------------------------------------------------------------------- #
# DTCs
# --------------------------------------------------------------------------- #
def dtc_description(code: str | None) -> str | None:
    """Short description for a decoded DTC (e.g. ``P0AA1``) from the dataset."""
    if not code or len(code) < 2 or code[0].upper() not in "PCBU":
        return None
    try:
        from pacifica_obd import pacifica_data

        return pacifica_data.dtc_text("%04X" % int(code[1:], 16))
    except Exception:  # noqa: BLE001 - a missing description must never break a read
        return None


def enrich_dtcs(dtcs: list[dict]) -> list[dict]:
    """Attach the bundled dataset description to each DTC (in place)."""
    for dtc in dtcs:
        if dtc.get("description") is None:
            dtc["description"] = dtc_description(dtc.get("code"))
    return dtcs


def read_module_dtcs(
    adapter,
    *,
    bus: str = "can-c",
    start: int = 0x700,
    end: int = 0x7FF,
    module=None,
    timeout_ms: int = 100,
    cancel: Cancel = None,
    progress: Progress = None,
) -> dict:
    """Read DTCs (UDS 0x19 0x02) from every responding module (parity with ``dtc-all``)."""
    if end < start:
        raise ValueError("end must be >= start")
    buses = ["can-c", "can-ihs"] if bus == "both" else [bus]
    for one in buses:
        if one not in BUS_PROTOCOL:
            raise ValueError(f"unknown bus: {one!r}")
    module_id = as_request_id(module) if module is not None else None
    wait = read_wait(timeout_ms)
    results: list[dict] = []
    for one in buses:
        if _cancelled(cancel):
            break
        configure_bus(adapter, one, timeout_ms)
        if module_id:
            targets: list[dict] = [{"request_id": module_id}]
        else:
            targets = list(probe_modules(
                adapter, start=start, end=end, probes=DEFAULT_DTC_PROBES,
                wait=wait, cancel=cancel, progress=progress,
            ))
        for target in targets:
            if _cancelled(cancel):
                break
            rid = target["request_id"]
            mapped, ok = address_module(adapter, rid)
            if not ok:
                results.append({"bus": one, "request_id": rid, "response_id": None,
                                "status": "no_response", "raw": None, "dtcs": []})
                _note(progress, {"kind": "module", "request_id": rid, "bus": one,
                                 "status": "no_response", "count": 0})
                continue
            expect = enter_session(adapter, rid, wait)
            header, uds = parse_uds_frame(adapter.cmd("1902FF", wait=wait),
                                          prefer_header=expect or mapped)
            if header is None:
                record = {"bus": one, "request_id": rid, "response_id": None,
                          "status": "no_response", "raw": None, "dtcs": []}
            else:
                classification = classify_dtc_response(uds)
                record = {
                    "bus": one, "request_id": rid, "response_id": header,
                    "status": classification, "raw": uds,
                    "dtcs": enrich_dtcs(parse_dtcs(uds))
                    if classification == "ok" else [],
                }
            results.append(record)  # every target is kept, including no_response
            _note(progress, {"kind": "module", "request_id": rid, "bus": one,
                             "status": record["status"], "count": len(record["dtcs"])})
    return {
        "buses": buses,
        "total_dtcs": sum(len(record["dtcs"]) for record in results),
        "incomplete": any(record["status"] != "ok" for record in results),
        "modules": results,
        "cancelled": _cancelled(cancel),
    }


# --------------------------------------------------------------------------- #
# DIDs + identification block
# --------------------------------------------------------------------------- #
def read_dids(
    adapter,
    *,
    bus: str = "can-c",
    module,
    dids: Iterable[int],
    timeout_ms: int = 100,
    cancel: Cancel = None,
) -> dict:
    """Read Data Identifiers (UDS 0x22) from one module (parity with ``read-did``)."""
    rid = as_request_id(module)
    wait = read_wait(timeout_ms)
    configure_bus(adapter, bus, timeout_ms)
    mapped, ok = address_module(adapter, rid)
    if not ok:
        raise AdapterError(f"module {rid} did not acknowledge ATSH")
    expect = enter_session(adapter, rid, wait)
    results: list[dict] = []
    for did in dids:
        if _cancelled(cancel):
            break
        record = read_did(adapter, int(did), wait, expect_header=expect or mapped)
        record["module"] = rid
        record["bus"] = bus
        results.append(record)
    return {"module": rid, "bus": bus, "dids": results,
            "vin": _vin_from_records(results), "cancelled": _cancelled(cancel)}


def module_info(
    adapter,
    *,
    bus: str = "can-c",
    module=None,
    start: int = 0x700,
    end: int = 0x7FF,
    timeout_ms: int = 100,
    cancel: Cancel = None,
    progress: Progress = None,
) -> dict:
    """Read the identification block F186..F191 (parity with ``module-info``)."""
    if end < start:
        raise ValueError("end must be >= start")
    buses = ["can-c", "can-ihs"] if bus == "both" else [bus]
    module_id = as_request_id(module) if module is not None else None
    wait = read_wait(timeout_ms)
    results: list[dict] = []
    for one in buses:
        if _cancelled(cancel):
            break
        configure_bus(adapter, one, timeout_ms)
        if module_id:
            request_ids = [module_id]
        else:
            request_ids = [
                target["request_id"]
                for target in probe_modules(
                    adapter, start=start, end=end, probes=DEFAULT_PROBES,
                    wait=wait, cancel=cancel, progress=progress,
                )
            ]
        for rid in request_ids:
            if _cancelled(cancel):
                break
            mapped, ok = address_module(adapter, rid)
            if not ok:
                _note(progress, {"kind": "module", "request_id": rid, "bus": one,
                                 "status": "no_response"})
                continue
            expect = enter_session(adapter, rid, wait)
            for did in INFO_DIDS:
                if _cancelled(cancel):
                    break
                record = read_did(adapter, did, wait, expect_header=expect or mapped)
                record["module"] = rid
                record["bus"] = one
                results.append(record)
            _note(progress, {"kind": "module", "request_id": rid, "bus": one})
    return {"buses": buses, "modules": results, "vin": _vin_from_records(results),
            "cancelled": _cancelled(cancel)}


def _vin_from_records(records: list[dict]) -> str | None:
    for record in records:
        if record.get("did") == "F190" and record.get("status") == "ok":
            value = record.get("ascii")
            if looks_like_vin(value):
                return value
    return None


# --------------------------------------------------------------------------- #
# full capture
# --------------------------------------------------------------------------- #
def capture_snapshot(
    adapter,
    *,
    buses: Iterable[str] = ("can-c",),
    start: int = 0x700,
    end: int = 0x7FF,
    timeout_ms: int = 100,
    vin: str | None = None,
    store=None,
    session_id: int | None = None,
    cancel: Cancel = None,
    progress: Progress = None,
) -> dict:
    """Full snapshot: modules + DTCs + identification block (parity with ``capture``).

    A mid-scan adapter failure is reported as ``aborted`` with everything read so
    far kept — the CLI's "partial capture" behaviour, which matters because a scan
    over 0x700-0x7FF is not instant.
    """
    if end < start:
        raise ValueError("end must be >= start")
    bus_list = list(buses)
    for one in bus_list:
        if one not in BUS_PROTOCOL:
            raise ValueError(f"unknown bus: {one!r}")
    wait = read_wait(timeout_ms)
    snapshot: dict = {
        "vin": vin, "buses": bus_list, "modules": [], "dtcs": [],
        "readings": [], "aborted": None, "cancelled": False,
    }
    try:
        for one in bus_list:
            if _cancelled(cancel):
                break
            configure_bus(adapter, one, timeout_ms)
            _note(progress, {"kind": "bus", "bus": one})
            for target in probe_modules(
                adapter, start=start, end=end, probes=DEFAULT_PROBES,
                wait=wait, cancel=cancel, progress=progress,
            ):
                rid = target["request_id"]
                mapped, ok = address_module(adapter, rid)
                if not ok:
                    continue
                expect = enter_session(adapter, rid, wait)
                _, dtc_uds = parse_uds_frame(adapter.cmd("1902FF", wait=wait),
                                             prefer_header=expect or mapped)
                dtcs = (
                    enrich_dtcs(parse_dtcs(dtc_uds))
                    if dtc_uds and classify_dtc_response(dtc_uds) == "ok" else []
                )
                snapshot["dtcs"].append(
                    {"bus": one, "request_id": rid, "dtcs": dtcs, "raw": dtc_uds}
                )
                if store is not None:
                    store.add_dtcs(session_id, snapshot.get("vin"), one, rid, dtcs, dtc_uds)

                module_vin = None
                part = None
                for did in INFO_DIDS:
                    if _cancelled(cancel):
                        break
                    record = read_did(adapter, did, wait, expect_header=expect or mapped)
                    record["module"] = rid
                    record["bus"] = one
                    snapshot["readings"].append(record)
                    if did == 0xF190 and record.get("status") == "ok" and looks_like_vin(record.get("ascii")):
                        module_vin = record["ascii"]
                        if snapshot["vin"] is None:
                            snapshot["vin"] = module_vin
                            if store is not None:
                                store.upsert_vehicle(module_vin)
                                store.backfill_vin(session_id, module_vin)
                    if did == 0xF187 and record.get("status") == "ok":
                        part = record.get("ascii") or record.get("hex")
                    if store is not None:
                        store.add_reading(
                            session_id, snapshot.get("vin"), one, rid, record["did"],
                            record.get("name"), record.get("hex"), record.get("ascii"),
                            record["status"], record.get("raw"), category="module-info",
                        )
                module = {
                    "request_id": rid,
                    "response_id": target.get("response_id"),
                    "kind": target.get("kind"),
                    "vin": module_vin,
                    "part_number": part,
                }
                snapshot["modules"].append(module)
                if store is not None:
                    store.add_module(session_id, snapshot.get("vin"), one, module)
                _note(progress, {"kind": "module", "request_id": rid, "bus": one,
                                 "modules": len(snapshot["modules"])})
    except (AdapterError, Elm327Error) as exc:
        snapshot["aborted"] = str(exc)
    snapshot["cancelled"] = _cancelled(cancel)
    if snapshot["cancelled"] and not snapshot["aborted"]:
        snapshot["aborted"] = "cancelled"
    snapshot["powertrain"] = detect_powertrain(
        {module["request_id"] for module in snapshot["modules"]}
    )
    existing = snapshot["vin"]
    vehicle_vin = existing if isinstance(existing, str) and existing else None
    if vehicle_vin and store is not None:
        store.upsert_vehicle(vehicle_vin, powertrain=snapshot["powertrain"],
                             year=_year_from_vin(vehicle_vin))
    return snapshot


def _year_from_vin(vin: str) -> int | None:
    try:
        from pacifica_obd.profiles import year_from_vin

        return year_from_vin(vin)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# generic OBD-II reads (python-OBD connection)
# --------------------------------------------------------------------------- #
def _codes(conn, command) -> list[dict]:
    resp = conn.query(command)
    if resp.is_null() or not resp.value:
        return []
    return [{"code": str(code), "description": str(description)}
            for code, description in resp.value]


def obd_dtcs(conn) -> dict:
    """Stored/pending/permanent DTCs (modes 03/07/0A) as the CLI reports them."""
    import obd

    def _maybe(name):
        command = getattr(obd.commands, name, None)
        return _codes(conn, command) if command is not None else []

    stored = _codes(conn, obd.commands.GET_DTC)
    pending = _maybe("GET_CURRENT_DTC")
    permanent = _maybe("GET_PERMANENT_DTC")
    return {
        "stored": stored, "pending": pending, "permanent": permanent,
        "total": len(stored) + len(pending) + len(permanent),
    }


def obd_live(conn, names: Iterable[str] | None = None) -> list[dict]:
    """Current value of every supported **read-only** PID (parity with ``live``).

    ``conn.supported_commands`` contains ``CLEAR_DTC``; it is filtered out here by
    the mode-01 allowlist, so this can never clear a code.
    """
    import obd

    rows: list[dict] = []
    if names:
        requested = [getattr(obd.commands, name) for name in names if hasattr(obd.commands, name)]
        refused = [name for name in names if not hasattr(obd.commands, name)]
        for name in names:
            command = getattr(obd.commands, name, None)
            if command is not None and not is_read_only_command(command):
                refused.append(name)
        if refused and not [c for c in requested if is_read_only_command(c)]:
            raise ValueError(f"not a read-only PID: {', '.join(sorted(set(refused)))}")
        commands = [c for c in requested if is_read_only_command(c)]
    else:
        commands = sorted((c for c in conn.supported_commands if is_read_only_command(c)),
                          key=lambda c: c.name)
    for command in commands:
        text = command_text(command)
        resp = conn.query(command)
        rows.append({
            "name": command.name,
            "command": text,
            "value": None if resp.is_null() else jsonable(resp.value),
        })
    return rows


def obd_pids(conn) -> list[dict]:
    """Supported read-only PIDs as picker rows (name, command, pid hex)."""
    rows: list[dict] = []
    for command in sorted((c for c in conn.supported_commands if is_read_only_command(c)),
                          key=lambda c: c.name):
        text = command_text(command)
        pid = text[2:4] if len(text) >= 4 else None
        rows.append({
            "name": command.name,
            "command": text,
            "pid": pid,
            "description": getattr(command, "desc", None),
        })
    return rows


def obd_readiness(conn) -> dict:
    """Readiness / I-M monitors (mode 01 PID 01), pre-emissions-test (``readiness``)."""
    import obd

    resp = conn.query(obd.commands.STATUS)
    if resp.is_null():
        return {"available": False, "reason": "no readiness data"}
    status = resp.value
    available = [
        (name, value)
        for name, value in status.__dict__.items()
        if hasattr(value, "available") and hasattr(value, "complete") and value.available
    ]
    mil = bool(status.MIL)
    dtc_count = status.DTC_count
    incomplete = [name for name, value in available if not value.complete]
    return jsonable({
        "available": True,
        "ignition_type": status.ignition_type,
        "mil": mil,
        "dtc_count": dtc_count,
        "ready": (not mil) and dtc_count == 0 and not incomplete,
        "monitors": [{"name": name, "complete": bool(value.complete)}
                     for name, value in available],
    })


# (pid, label, unit, decoder); the decoder receives the payload after the frame byte.
FREEZE_PIDS = [
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


def _query_freeze(conn, pid: int) -> bytes | None:
    """Raw mode-02 request; returns the payload after the frame-number byte."""
    try:
        messages = conn.interface.send_and_parse("02%02X00" % pid)
    except Exception:  # noqa: BLE001 - a bad/no response is not fatal
        return None
    if not messages:
        return None
    for message in messages:
        data = bytes(message.data)
        if len(data) >= 3 and data[0] == 0x42 and data[1] == pid:
            return data[3:]
    return None


def obd_freeze(conn) -> dict:
    """Freeze-frame snapshot behind a DTC (mode 02), parity with ``freeze``."""
    import obd

    resp = conn.query(obd.commands.FREEZE_DTC)
    raw = None if resp.is_null() else resp.value
    frame_dtc = None
    if isinstance(raw, (list, tuple)) and raw:
        first = raw[0]
        frame_dtc = first[0] if isinstance(first, (list, tuple)) else first
    elif raw:
        frame_dtc = str(raw)
    if not frame_dtc:
        return {"dtc": None, "params": [], "reason": "no freeze-frame data stored"}
    params = []
    for pid, label, unit, decoder in FREEZE_PIDS:
        payload = _query_freeze(conn, pid)
        if payload is None:
            continue
        try:
            value = decoder(payload)
        except (IndexError, TypeError):
            continue
        params.append({"pid": "0x%02X" % pid, "name": label, "value": value, "unit": unit})
    return {"dtc": str(frame_dtc), "params": params}


def obd_info(conn) -> dict:
    """Adapter/vehicle info (parity with ``info``)."""
    import obd

    voltage = conn.query(obd.commands.ELM_VOLTAGE)
    version = conn.query(obd.commands.ELM_VERSION)
    vin = conn.query(obd.commands.VIN)
    return {
        "port": conn.port_name(),
        "status": str(conn.status()),
        "protocol": conn.protocol_name(),
        "adapter": None if version.is_null() else jsonable(version.value),
        "voltage": None if voltage.is_null() else jsonable(voltage.value),
        "vin": None if vin.is_null() else jsonable(vin.value),
    }


def obd_vin(conn) -> str | None:
    """VIN via mode 09 02 only (may be unserved; see :func:`uds_vin`)."""
    import obd

    if conn is None:
        return None
    resp = conn.query(obd.commands.VIN)
    value = None if resp.is_null() else resp.value
    if isinstance(value, str) and looks_like_vin(value):
        return value
    return None


def uds_vin(adapter, *, buses: Iterable[str] = ("can-c", "can-ihs"),
            timeout_ms: int = 100, cancel: Cancel = None) -> str | None:
    """VIN via UDS F190 across the bundled module list (the ``vin`` fallback).

    The Pacifica Hybrid often does not serve mode 09 02 at all, so this is not
    optional. Best effort: returns the first plausible VIN.
    """
    from pacifica_obd import pacifica_data

    wait = read_wait(timeout_ms)
    for bus in buses:
        if _cancelled(cancel):
            return None
        rows = [row for row in pacifica_data.modules() if (row.get("bus") or "") == bus]
        if not rows:
            continue
        configure_bus(adapter, bus, timeout_ms)
        for row in rows:
            try:
                request = int(str(row.get("request")), 16)
            except (TypeError, ValueError):
                continue
            rid = "%03X" % request
            adapter.cmd("ATSH " + rid)
            try:
                adapter.cmd("ATCRA %03X" % int(str(row.get("response")), 16))
            except (TypeError, ValueError):
                pass
            expect = enter_session(adapter, rid, wait)
            record = read_did(adapter, 0xF190, wait, expect_header=expect)
            if record["status"] == "ok" and looks_like_vin(record.get("ascii")):
                return record["ascii"]
    return None
