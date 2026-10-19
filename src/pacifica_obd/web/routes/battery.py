"""HV battery screen routes (design §3.6) — a purpose-built PHEV pack panel.

Three endpoints, all reads of the vehicle plus one local-config write:

* ``GET  /api/battery/schema`` — the recovered DID map (each field marked
  *verified* or *unverified* with its source) and the saved-preset shape.
* ``POST /api/battery/read``   — one snapshot: the pack DIDs are read from the
  BPCM (``0x7E7``) and HCP (``0x7E2``) through the shared :mod:`service` layer,
  never the adapter directly. Returns per-cell mV, pack V, temperatures, SoC and
  a computed **imbalance = max − min** with a verdict band. A DID that cannot be
  read degrades to a per-field status instead of failing the whole snapshot.
* ``POST /api/battery/preset`` — validate (strictly) and save a DID map, so an
  unverified pack can still be read once the owner supplies the identifier.

What this module deliberately does **not** claim
-----------------------------------------------
The operator's AlfaOBD analysis database (`~/temp/alfa-obd/analysis/alfaobd.db.joined`)
carries **parameter names and units** for the BPCM/HCP pack data but **no DID / hex
/ request column** for the Pacifica hybrid modules — the identifiers were never
recovered. So every pack DID here ships ``verified: false`` and ``did: null``
until the owner supplies one; the screen reads nothing it cannot name. The only
**verified** DIDs are the identification block from `docs/fca-modules.md §4.1`
(observed answering on the real vehicle in `findings/09-real-vehicle-session.md`),
which are not pack data.

Two rules the read path keeps from ``AGENTS.md``: it sets an ``ATCRA`` receive
filter (via the service layer) and never sends a write service — no ``0x2E``
(write-data-by-identifier), no ``0x31`` (routine control), no ``0x27`` (security
access), no ``0x14``/``0x04`` (clear). The standalone tests assert this.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from fastapi import APIRouter, Request

from pacifica_obd import pacifica_data
from pacifica_obd.web import deps, errors, service
from pacifica_obd.web.schemas import BatteryPreset, BatteryReadRequest

router = APIRouter()

# The hybrid pack lives on CAN-C: BPCM holds the cells/temps/current, HCP holds
# the vehicle-level state of charge. Addresses are from the bundled dataset
# (`pacifica_data.modules()`), not invented here.
BPCM = "7E7"
HCP = "7E2"
DEFAULT_SOC_MODULE = HCP

# The imbalance colour band the design calls for: a cell more than this far from
# the pack mean is highlighted; the verdict bands below use the same unit (mV).
AWAY_MV = 50.0
# A Li-ion cell read that lands outside this window almost certainly means the
# DID/scale is wrong, so it is surfaced as a warning rather than trusted.
PLAUSIBLE_MV = (1500.0, 4500.0)

MAX_CELLS = 96

# --------------------------------------------------------------------------- #
# The recovered DID map
# --------------------------------------------------------------------------- #
# Name/unit come from the operator's AlfaOBD analysis copy (read-only):
#   Param_devices Device_ID 265 "BPCM" / 297 "BPCM_PN" and 295 "HCP_PN",
#   joined through Devices_params_units → Param_names/Units.
# There is **no** DID column anywhere in that join, so `did` stays null and
# `verified` stays false. `dataset_name` cross-references the shipped
# `pacifica_data.dids(ecu)` names where they overlap.
_ALFA = "~/temp/alfa-obd/analysis/alfaobd.db.joined (Param_names/Units, no DID column)"
_DATASET = "src/pacifica_obd/data/pacifica.json (name+unit only, no DID)"
_ID_SRC = "docs/fca-modules.md §4.1; ~/temp/alfa-obd/findings/09-real-vehicle-session.md"
_ID_UNCONFIRMED = (_ID_SRC + " (candidate ISO F1xx identification DID; NOT confirmed on this vehicle — "
                   "findings/09 records standard F180–F19E mostly returning NRC 31)")

DID_MAP = {
    "cells": {
        "name": "Cell N Voltage",
        "unit": "mV",
        "did": None,
        "verified": False,
        "scale_default": 1.0,
        "source": f"{_ALFA}; params 3201–3266 (device 265 BPCM)",
    },
    "pack_voltage": {
        "name": "High Voltage Sensor (Pack)",
        "unit": "V",
        "did": None,
        "verified": False,
        "scale_default": 0.1,
        "dataset_name": "Battery Voltage (cell sum)",
        "source": f"{_ALFA}; device 265 BPCM ('Battery Voltage (cell sum)' + 'High Voltage Sensor (Pack)')",
    },
    "soc": {
        "name": "State of Charge",
        "unit": "%",
        "did": None,
        "verified": False,
        "scale_default": 0.5,
        "dataset_name": "HV Battery SOC",
        "source": f"{_ALFA}; device 265 BPCM ('State of Charge') and 295 HCP_PN ('HV Battery SOC')",
    },
    "temperature": {
        "name": "Max/Min Cell Temp",
        "unit": "|C",
        "did": None,
        "verified": False,
        "scale_default": 1.0,
        "dataset_name": "Battery inlet temp / outlet temp",
        "source": f"{_ALFA}; device 265 BPCM ('Max Cell Temp' / 'Min Cell Temp')",
    },
    "current": {
        "name": "HV battery current",
        "unit": "A",
        "did": None,
        "verified": False,
        "scale_default": 0.1,
        "dataset_name": "HV battery current",
        "source": f"{_ALFA}; device 265 BPCM ('HV Battery Current Sensor 1 Hall')",
    },
}

# Identification DIDs. Only the ones the first real-vehicle session lists as
# answering are marked verified; F110's offsets were never recovered, and the
# standard ISO F186–F191 block is offered but *not* confirmed on this vehicle.
IDENTIFICATION_DIDS = [
    {"did": "F100", "name": "CUSW architecture id", "verified": True, "source": _ID_SRC},
    {"did": "F110", "name": "Atlantis architecture id (offsets unverified)", "verified": False, "source": _ID_SRC},
    {"did": "F154", "name": "Supplier identifier", "verified": True, "source": _ID_SRC},
    {"did": "F155", "name": "Supplier identifier (alternate)", "verified": True, "source": _ID_SRC},
    {"did": "F18C", "name": "ECU serial number", "verified": True, "source": _ID_SRC},
    {"did": "F190", "name": "VIN", "verified": True, "source": _ID_SRC},
    {"did": "F122", "name": "Software part number", "verified": True, "source": _ID_SRC},
    {"did": "F132", "name": "ECU part number", "verified": True, "source": _ID_SRC},
    {"did": "F186", "name": "Active diagnostic session", "verified": False, "source": _ID_UNCONFIRMED},
    {"did": "F187", "name": "Spare part number", "verified": False, "source": _ID_UNCONFIRMED},
    {"did": "F188", "name": "ECU software number", "verified": False, "source": _ID_UNCONFIRMED},
    {"did": "F189", "name": "ECU software version", "verified": False, "source": _ID_UNCONFIRMED},
    {"did": "F191", "name": "ECU hardware number", "verified": False, "source": _ID_UNCONFIRMED},
]

# Guided-test routines whose *names* were recovered from the AlfaOBD
# Diag_names table on the same device family. No RID was recovered, so they are
# listed as a roadmap and never sent.
GUIDED_ROUTINES = [
    {"name": "HV Battery Balance Test", "rid": None, "verified": False, "source": _ALFA},
    {"name": "HV Battery Isolation Test", "rid": None, "verified": False, "source": _ALFA},
    {"name": "High Voltage Battery Negative Contactor Test", "rid": None, "verified": False, "source": _ALFA},
    {"name": "High Voltage Battery Positive Contactor Test", "rid": None, "verified": False, "source": _ALFA},
    {"name": "High Voltage Battery Precharge Contactor Test", "rid": None, "verified": False, "source": _ALFA},
]

# A synthetic fixture honoured only by the ``--demo`` adapter. It is *not* a
# confirmed vehicle identifier and is never presented as one — the UI labels it
# `unverified`, and the empty state warns the owner that unverified DIDs may
# read nothing on a real car.
DEMO_PRESET = {
    "cells_did": "F40C",
    "first_cell_index": 1,
    "count": 32,
    "scale": 1.0,
    "pack_voltage_did": "F40D",
    "pack_voltage_scale": 0.1,
    "soc_did": "F40E",
    "soc_scale": 0.5,
    "temp_did": "F40F",
    "temp_scale": 1.0,
    "current_did": None,
    "current_scale": 0.1,
    "module": BPCM,
    "soc_module": HCP,
    "verified": False,
    "source": "demo-synthetic (honoured only by --demo; not a confirmed vehicle identifier)",
}

# An empty preset means "nothing readable yet" — every field degrades to a
# per-field status and the screen shows its empty state.
DEFAULT_PRESET = {
    "cells_did": None,
    "first_cell_index": 1,
    "count": 0,
    "scale": 1.0,
    "pack_voltage_did": None,
    "pack_voltage_scale": 0.1,
    "soc_did": None,
    "soc_scale": 0.5,
    "temp_did": None,
    "temp_scale": 1.0,
    "current_did": None,
    "current_scale": 0.1,
    "module": BPCM,
    "soc_module": HCP,
}


# --------------------------------------------------------------------------- #
# validation helpers (pure; mirror the CLI's strictness)
# --------------------------------------------------------------------------- #
def _norm_did(value, *, field: str) -> str | None:
    """A 4-hex DID (``"F40C"``, ``"0xF40C"``, ``0xF40C``) or None."""
    if value is None or value == "":
        return None
    text = str(value).strip().upper()
    text = text[2:] if text.startswith("0X") else text
    if len(text) != 4 or any(ch not in "0123456789ABCDEF" for ch in text):
        raise ValueError(f"{field}: not a 4-hex DID: {value!r}")
    return text


def _finite_positive(value, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field}: not a number: {value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field}: must be a finite positive number")
    if number > 1e6:
        raise ValueError(f"{field}: implausibly large")
    return number


def _int_in_range(value, *, field: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field}: not an integer: {value!r}") from None
    if not low <= number <= high:
        raise ValueError(f"{field}: must be {low}..{high}")
    return number


def _validate_preset(raw) -> dict:
    """Normalise and strictly validate a preset; a ValueError is a 400."""
    if isinstance(raw, BatteryPreset):
        raw = raw.model_dump()
    raw = dict(raw or {})
    preset = dict(DEFAULT_PRESET)

    for key, field in (("cells_did", "cells DID"), ("pack_voltage_did", "pack voltage DID"),
                       ("soc_did", "SOC DID"), ("temp_did", "temperature DID"),
                       ("current_did", "current DID")):
        if key in raw:
            preset[key] = _norm_did(raw.get(key), field=field)

    if "first_cell_index" in raw:
        preset["first_cell_index"] = _int_in_range(
            raw.get("first_cell_index"), field="first cell index", low=0, high=MAX_CELLS - 1)

    if "count" in raw:
        count = _int_in_range(raw.get("count"), field="cell count", low=0, high=MAX_CELLS)
        preset["count"] = count
    if preset.get("cells_did") and not (1 <= preset["count"] <= MAX_CELLS):
        raise ValueError("cell count must be 1..%d when a cells DID is set" % MAX_CELLS)
    if not preset.get("cells_did"):
        preset["count"] = 0

    for key, field in (("scale", "cells scale"), ("pack_voltage_scale", "pack voltage scale"),
                       ("soc_scale", "SOC scale"), ("temp_scale", "temperature scale"),
                       ("current_scale", "current scale")):
        preset[key] = _finite_positive(raw.get(key, preset[key]), field=field)

    for key, field in (("module", "BPCM module"), ("soc_module", "SoC module")):
        try:
            preset[key] = service.as_request_id(raw.get(key, preset.get(key) or BPCM))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{field}: {exc}") from None

    preset["verified"] = False
    preset["source"] = str(raw.get("source") or "owner preset (unverified)")
    return preset


# --------------------------------------------------------------------------- #
# decode helpers (pure; no adapter)
# --------------------------------------------------------------------------- #
def _words_be(hexstr: str) -> list[int]:
    text = "".join(ch for ch in (hexstr or "").upper() if ch in "0123456789ABCDEF")
    if len(text) < 4:
        return []
    text = text[: (len(text) // 4) * 4]
    return [int(text[i:i + 4], 16) for i in range(0, len(text), 4)]


def _signed_bytes(hexstr: str) -> list[int]:
    text = "".join(ch for ch in (hexstr or "").upper() if ch in "0123456789ABCDEF")
    text = text[: (len(text) // 2) * 2]
    out = []
    for i in range(0, len(text), 2):
        byte = int(text[i:i + 2], 16)
        out.append(byte - 256 if byte >= 128 else byte)
    return out


_REST_NOTE = "A spread is only meaningful with the pack at rest and the decoding confirmed."


def _verdict(imbalance_mv: float | None, *, complete: bool = True,
             plausible: bool = True) -> dict:
    """A plain-English band for a max−min spread, in mV.

    Deliberately conservative: the bands flag *what to look at*; they never
    certify pack health. The verdict is withheld entirely when the read is
    incomplete or the values are implausible, because a partial or
    wrongly-scaled column would otherwise read as "healthy".
    """
    def band(name: str, text: str) -> dict:
        return {"band": name, "threshold_mv": AWAY_MV, "text": text, "note": _REST_NOTE}

    if not complete:
        return band("incomplete", "Only some cells were read — coverage is incomplete, "
                                  "so no health verdict is offered.")
    if not plausible:
        return band("unknown", "A cell value is outside a plausible Li-ion range — the DID "
                               "or scale is probably wrong, so no verdict is offered.")
    if imbalance_mv is None:
        return band("unknown", "No cell voltages were read, so there is nothing to judge.")
    spread = abs(float(imbalance_mv))
    if spread <= 30:
        return band("balanced", f"Cells within {spread:.0f} mV of each other — no spread worth "
                                "flagging on this (unverified) read.")
    if spread <= 60:
        return band("watch", f"A {spread:.0f} mV spread — worth watching; not alarming on its own.")
    if spread <= 120:
        return band("imbalanced", f"A {spread:.0f} mV spread — a real imbalance. Note the weakest "
                                  f"cell and re-check at rest.")
    return band("severe", (f"A {spread:.0f} mV spread — severe. Re-check at rest and do not treat this "
                           "as a diagnosis; a dealer pack test is the authority."))


def _decode_cells(record: dict, preset: dict) -> dict:
    """Turn one 0x22 reply into per-cell mV using the preset's agreed convention.

    Unverified convention: the payload after ``62 <DID>`` is a run of big-endian
    16-bit words, one per cell, scaled by ``scale`` into mV. The owner confirms
    the convention by cross-checking one column against `#/raw` (`22 <DID>`).
    """
    out = {
        "status": record.get("status", "no_response"),
        "did": preset.get("cells_did"),
        "first_cell_index": preset.get("first_cell_index", 1),
        "count": preset.get("count", 0),
        "scale": preset.get("scale", 1.0),
        "values_mv": [],
        "index": [],
    }
    if out["status"] != "ok":
        return out
    words = _words_be(record.get("hex") or "")
    if not words:
        out["status"] = "unexpected"
        out["complete"] = False
        return out
    configured = int(preset.get("count") or 0)
    # Completeness is judged against the configured count: a short reply must
    # never read as a full, healthy pack.
    out["complete"] = configured == 0 or len(words) >= configured
    limit = configured or len(words)
    words = words[:limit]
    if not out["complete"]:
        out["status"] = "partial"
    scale = float(preset.get("scale", 1.0))
    first = int(preset.get("first_cell_index", 1))
    values = [round(word * scale, 3) for word in words]
    out["values_mv"] = values
    out["index"] = list(range(first, first + len(values)))
    if values:
        out["mean_mv"] = round(sum(values) / len(values), 3)
        low = min(values)
        high = max(values)
        out["min_mv"] = low
        out["max_mv"] = high
        out["min_index"] = out["index"][values.index(low)]
        out["max_index"] = out["index"][values.index(high)]
        out["imbalance_mv"] = round(high - low, 3)
        out["away"] = [idx for idx, value in zip(out["index"], values)
                       if abs(value - out["mean_mv"]) > AWAY_MV]
        out["away_mv"] = AWAY_MV
        lo, hi = PLAUSIBLE_MV
        out["plausible"] = all(lo <= value <= hi for value in values)
    return out


def _scalar(record: dict | None, did: str | None, scale: float, *, signed: bool = False):
    """Decode a single-word value; returns a status dict, never raises."""
    if not did:
        return {"value": None, "unit": None, "status": "unconfigured", "did": None}
    if record is None:
        return {"value": None, "unit": None, "status": "no_response", "did": did}
    status = record.get("status", "no_response")
    base = {"value": None, "unit": None, "status": status, "did": did,
            "raw": record.get("raw")}
    if status != "ok":
        return base
    if signed:
        values = _signed_bytes(record.get("hex") or "")
    else:
        words = _words_be(record.get("hex") or "")
        values = words[:1]
    if not values:
        base["status"] = "unexpected"
        return base
    base["value"] = round(values[0] * scale, 3)
    return base


def _decode_temps(record: dict | None, did: str | None, scale: float):
    if not did:
        return {"min_c": None, "max_c": None, "unit": "|C", "status": "unconfigured", "did": None}
    if record is None:
        return {"min_c": None, "max_c": None, "unit": "|C", "status": "no_response", "did": did}
    status = record.get("status", "no_response")
    base = {"min_c": None, "max_c": None, "unit": "|C", "status": status, "did": did}
    if status != "ok":
        return base
    values = _signed_bytes(record.get("hex") or "")
    if not values:
        base["status"] = "unexpected"
        return base
    scaled = [round(value * scale, 3) for value in values]
    base["min_c"] = min(scaled)
    base["max_c"] = max(scaled)
    base["values_c"] = scaled
    return base


# --------------------------------------------------------------------------- #
# local preset store (read/write JSON under the app home; never crashes)
# --------------------------------------------------------------------------- #
def _app_home() -> Path:
    override = os.environ.get("PACIFICA_OBD_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "pacifica-obd"
    return Path.home() / ".config" / "pacifica-obd"


def _preset_path() -> Path:
    return _app_home() / "battery_preset.json"


def load_saved_preset() -> dict | None:
    """A validated saved preset, or None. A corrupt file is ignored, not fatal."""
    path = _preset_path()
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _validate_preset(raw)
    except Exception:  # noqa: BLE001 - persistence must never break a request
        return None


def save_preset(preset: dict) -> bool:
    """Write the preset atomically; a failure is reported, never raised."""
    path = _preset_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(preset, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 - a read-only home must not break the read screen
        return False


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
def _reference_fields() -> list[dict]:
    """BPCM/HCP dataset items that mention a pack quantity (name+unit only)."""
    wanted = ("cell", "volt", "soc", "state of charge", "temp", "current", "impedance", "contactor")
    rows = []
    for ecu in ("BPCM_PN", "HCP_PN"):
        for item in pacifica_data.dids(ecu):
            name = str(item.get("name") or "").lower()
            if any(token in name for token in wanted):
                rows.append({"ecu": ecu, "name": item.get("name"), "unit": item.get("unit"),
                             "kind": item.get("kind")})
    return rows


def _schema(request: Request) -> dict:
    settings = deps.get_settings(request)
    saved = load_saved_preset()
    demo_preset = dict(DEMO_PRESET) if settings.demo else None
    effective = saved or demo_preset or dict(DEFAULT_PRESET)
    return {
        "module": {"bpcm": BPCM, "hcp": HCP, "bus": "can-c"},
        "units": {"cells": "mV", "imbalance": "mV", "pack_voltage": "V",
                  "soc": "%", "temperature": "|C", "current": "A"},
        "thresholds": {"away_mv": AWAY_MV, "plausible_mv": list(PLAUSIBLE_MV),
                       "max_cells": MAX_CELLS},
        "dids": DID_MAP,
        "identification_dids": IDENTIFICATION_DIDS,
        "guided_routines": GUIDED_ROUTINES,
        "preset": effective,
        "saved": bool(saved),
        "demo_preset": demo_preset,
        "dataset_reference": _reference_fields(),
        "notes": [
            "No pack DID was recovered from the analysis database: the BPCM/HCP rows carry names and units only, no DID/hex column.",
            "The identification block (F1xx) is the only confirmed-readable DID set; it is not pack data.",
            "Unverified DIDs may read nothing on a real vehicle — confirm one column against the Raw console (22 <DID>) before trusting the chart.",
        ],
    }


@router.get("/battery/schema", summary="HV battery DID map + saved preset (offline)")
async def battery_schema(request: Request):
    return _schema(request)


@router.post("/battery/preset", summary="Validate and save the HV battery DID map")
async def battery_preset(request: Request, body: BatteryPreset):
    try:
        preset = _validate_preset(body)
    except ValueError as exc:
        raise errors.validation(str(exc)) from None
    persisted = save_preset(preset)
    return {
        "preset": preset,
        "saved": persisted,
        "verified": False,
        "warning": ("Unverified DID map. Nothing here is confirmed for this vehicle; "
                    "an unverified DID may read nothing or the wrong quantity."),
    }


@router.post("/battery/read", summary="One HV battery snapshot (BPCM pack reads + imbalance)")
async def battery_read(request: Request, body: BatteryReadRequest):
    worker = deps.get_worker(request)
    timeout_ms = deps.parse_timeout_ms(body.timeout_ms)

    if body.preset is not None:
        try:
            preset = _validate_preset(body.preset)
        except ValueError as exc:
            raise errors.validation(str(exc)) from None
    else:
        settings = deps.get_settings(request)
        fallback = dict(DEMO_PRESET) if settings.demo else dict(DEFAULT_PRESET)
        preset = load_saved_preset() or fallback

    def job(ctx):
        adapter = ctx.uds()
        session_id = ctx.open_session("battery-read", {"preset": {
            k: preset.get(k) for k in ("cells_did", "count", "scale", "pack_voltage_did",
                                       "soc_did", "temp_did")}})
        try:
            result = _read_snapshot(adapter, body, preset, timeout_ms,
                                    store=ctx.store if body.store else None,
                                    session_id=session_id)
            return result
        finally:
            ctx.close_session(session_id, "ok")

    return await deps.run_job(worker, "battery-read", job, timeout_s=180.0)


def _read_snapshot(adapter, body: BatteryReadRequest, preset: dict, timeout_ms: int, *,
                   store=None, session_id=None) -> dict:
    """Read the pack DIDs through the service layer, then decode.

    Each module read is isolated: an unaddressed module (the common case for an
    unverified DID map, or a vehicle asleep) marks its fields ``no_response``
    instead of failing the snapshot.
    """
    bus = body.bus
    bpcm = preset.get("module") or BPCM
    soc_module = preset.get("soc_module") or DEFAULT_SOC_MODULE
    warnings: list[str] = []

    def read_module(module: str, dids: list[str]) -> tuple[dict, dict]:
        wanted = [did for did in dids if did]
        by_did: dict[str, dict] = {}
        meta = {"module": module, "status": "ok", "error": None}
        if not wanted:
            return by_did, meta
        try:
            payload = service.read_dids(adapter, bus=bus, module=module,
                                        dids=[service.parse_did(did) for did in wanted],
                                        timeout_ms=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - a failed module must not fail the snapshot
            meta["status"] = "no_response"
            meta["error"] = str(exc)
            return by_did, meta
        for record in payload.get("dids", []):
            by_did[str(record.get("did", "")).upper()] = record
        return by_did, meta

    bpcm_dids = [preset.get("cells_did"), preset.get("pack_voltage_did"),
                 preset.get("temp_did"), preset.get("current_did")]
    soc_dids = [preset.get("soc_did")]
    run_soc_on_soc_module = bool(soc_dids[0]) and soc_module != bpcm
    if soc_dids[0] and not run_soc_on_soc_module:
        # SoC is read from the pack module itself; fold it into that read.
        bpcm_dids.append(soc_dids[0])

    bpcm_records, bpcm_meta = read_module(bpcm, bpcm_dids)
    soc_records: dict = {}
    if run_soc_on_soc_module:
        soc_records, _ = read_module(soc_module, soc_dids)
    else:
        soc_records = bpcm_records

    cells = _decode_cells(bpcm_records.get(str(preset.get("cells_did") or "").upper(), {}), preset)
    pack = _scalar(bpcm_records.get(str(preset.get("pack_voltage_did") or "").upper()),
                   preset.get("pack_voltage_did"), float(preset.get("pack_voltage_scale", 0.1)))
    pack["unit"] = "V"
    soc = _scalar(soc_records.get(str(preset.get("soc_did") or "").upper()),
                  preset.get("soc_did"), float(preset.get("soc_scale", 0.5)))
    soc["unit"] = "%"
    temps = _decode_temps(bpcm_records.get(str(preset.get("temp_did") or "").upper()),
                          preset.get("temp_did"), float(preset.get("temp_scale", 1.0)))
    current = _scalar(bpcm_records.get(str(preset.get("current_did") or "").upper()),
                      preset.get("current_did"), float(preset.get("current_scale", 0.1)))
    current["unit"] = "A"

    # The cell sum is only a legitimate stand-in for pack voltage when the cell
    # set is complete and plausible: a partial or mis-scaled column would
    # otherwise present a nonsense "pack voltage" as an `ok` reading.
    cells_complete = cells.get("complete", False)
    cells_plausible = cells.get("plausible", True)
    if (pack["value"] is None and cells["values_mv"]
            and cells_complete and cells_plausible):
        total = sum(cells["values_mv"]) / 1000.0
        pack = {"value": round(total, 3), "unit": "V", "status": "ok",
                "did": preset.get("cells_did"), "source": "cells_sum"}
    else:
        pack.setdefault("source", "did" if preset.get("pack_voltage_did") else "unconfigured")
        if pack["value"] is None and cells["values_mv"]:
            pack["source"] = "cells_sum_incomplete"

    if cells["values_mv"] and not cells_plausible:
        warnings.append("A cell value is outside a plausible Li-ion range — the DID or scale "
                        "is probably wrong; treat the chart as unverified.")
    if cells["values_mv"] and not cells_complete:
        warnings.append(f"Only {len(cells['values_mv'])} of the configured "
                        f"{preset.get('count')} cells were read — the verdict is withheld and the "
                        "pack voltage is not inferred from a partial sum.")

    imbalance = cells.get("imbalance_mv")
    # Withhold the verdict only when a *partial* column was actually read; an
    # empty read is reported as "nothing to judge", not as incomplete.
    verdict = _verdict(imbalance, complete=(cells_complete or not cells["values_mv"]),
                       plausible=cells_plausible)

    recorded = None
    if store is not None:
        try:
            rows = 0
            for index, value in zip(cells["index"], cells["values_mv"]):
                store.add_sample(session_id, None, bus, bpcm, str(preset.get("cells_did")),
                                 f"cell_{index}", value, str(value), "mV", "ok")
                rows += 1
            if imbalance is not None:
                store.add_sample(session_id, None, bus, bpcm, str(preset.get("cells_did")),
                                 "imbalance", imbalance, str(imbalance), "mV", "ok")
                rows += 1
            for name, field in (("pack_voltage", pack), ("soc", soc)):
                if field.get("value") is not None:
                    store.add_sample(session_id, None, bus,
                                     soc_module if name == "soc" else bpcm,
                                     str(field.get("did")), name, field["value"],
                                     str(field["value"]), field.get("unit"), "ok")
                    rows += 1
            for name, value in (("temp_min", temps.get("min_c")), ("temp_max", temps.get("max_c"))):
                if value is not None:
                    store.add_sample(session_id, None, bus, bpcm,
                                     str(preset.get("temp_did")), name, value, str(value), "|C", "ok")
                    rows += 1
            recorded = {"session_id": session_id, "rows": rows}
        except Exception:  # noqa: BLE001 - recording must never fail the read
            recorded = None

    return {
        "module": {"bpcm": bpcm, "hcp": soc_module, "bus": bus, "bpcm_status": bpcm_meta["status"]},
        "cells": cells,
        "imbalance_mv": imbalance,
        "verdict": verdict,
        "pack_voltage": pack,
        "soc": soc,
        "temperature": temps,
        "current": current,
        "preset": preset,
        "recorded": recorded,
        "warnings": warnings,
        "cancelled": False,
    }
