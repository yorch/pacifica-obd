"""DID discovery sweep — a bounded sweep *planner* and a *payload classifier*.

The HV battery screen is configuration-driven because the pack DIDs were never
recovered: the parameter *names* exist ("Cell 1..N Voltage", "HV Battery
current", "State of Charge") but no hex identifiers. A sweep on the real vehicle
is the only way to find them, so this module answers two questions, both without
touching the adapter:

* :func:`plan` — **which DIDs to read**, given a range, a stride and a hard cap.
* :func:`classify` — **what a 0x22 reply plausibly is**, from its bytes alone.

It is deliberately pure: no I/O, no printing, no FastAPI, no adapter. The
front ends (the ``did-sweep`` CLI command and ``POST /api/battery/sweep``) call
the shared service layer to actually send the reads; this module only decides
what to ask and how to interpret the answer.

Reads only
----------
A sweep sends exactly two services: ``10 03`` to open the extended session and
``22 <DID>`` per identifier. :func:`assert_read_only` refuses anything else by
delegating to :mod:`pacifica_obd.raw_guard` (the one place the read/confirm/deny
tiers are defined), so the rule cannot drift from the Raw console.

Thresholds (conservative — a wrong guess is worse than "unknown")
----------------------------------------------------------------
Every threshold below is written down so a reviewer can argue with the number
rather than with the code. The bias is everywhere the same: a shape is only
named when the bytes fit it *and* do not fit a more specific shape, and any
overlap lands in ``ambiguous`` rather than a confident guess.

``cell_voltages``
    A run of ``>= 4`` consecutive **big-endian u16** words (>= 8 bytes), every
    word within ``1000..5500 mV``, max-min spread ``< 500 mV``, and 4..96 words.
    ``count`` = payload length / 2, ``scale`` = 1, units ``mV``. A block of
    ``>= 8`` words (>= 16 bytes) is treated as *confidently* a cell column: no
    pack-voltage DID returns 16+ bytes, so the leading-word pack reading is
    dropped there. A 4..7-word block is kept **ambiguous** with pack voltage.

``pack_voltage``
    The **leading** big-endian u16, ``scale 0.1`` when the raw count is
    ``1500..5000`` (150.0-500.0 V), or ``scale 1`` when the raw count is
    ``150..500`` (150-500 V). Suppressed when the payload is a confident cell
    block (previous rule).

``soc``
    A single u8/u16 whose raw count ``<= 200`` so that ``scale 1`` (raw <= 100)
    or ``scale 0.5`` (raw <= 200) lands in ``0..100 %``. ``scale`` is 1 when the
    raw count is already <= 100, else 0.5. A SoC-shaped value almost always also
    fits ``temperature`` at ``0.1``, so without a ``context="soc"`` hint it is
    reported ``medium``/``ambiguous`` — never a confident guess.

``temperature``
    One byte as i8/u8 in ``-40..100 °C`` (scale 1), or two bytes as a signed
    i16 whose ``scale 0.1`` value lands in ``-40.0..100.0 °C`` (raw
    ``-400..1000``).

``identification``
    All bytes printable ASCII (``0x20..0x7E``) and length ``>= 4``: a VIN-shaped
    17-char string, a plausible serial/part string, or other printable text.

``ambiguous``
    More than one of the above fits; ``confidence`` is ``ambiguous`` and
    ``alternatives`` names the other shapes.

``unknown``
    Nothing above fits (an empty, odd-length, non-hex or over-long payload, or a
    value outside every window) — no interpretation is invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "READ_SERVICES",
    "DEFAULT_MAX_READS",
    "ABSOLUTE_MAX_READS",
    "IDENTIFICATION_RANGE",
    "PACK_RANGE",
    "SweepPlan",
    "Shape",
    "Verdict",
    "Candidate",
    "plan",
    "classify",
    "read_command",
    "session_commands",
    "assert_read_only",
    "candidates",
    "build_preset",
    "preset_suggestion",
    "CONFIDENCE_RANK",
]

#: The only services a sweep may put on the wire (UDS session control + read DID).
READ_SERVICES = frozenset({"10", "22"})

# --------------------------------------------------------------------------- #
# planner bounds
# --------------------------------------------------------------------------- #
#: The default cap. A sweep of a whole 0x100 window (256 reads) is the common
#: case; a range over more than this is almost always a mistake and is refused.
DEFAULT_MAX_READS = 512
#: An absolute ceiling on any plan, even when the caller raises the cap. Keeps a
#: sweep from ever becoming the "probe all 65536 DIDs" operation.
ABSOLUTE_MAX_READS = 4096

#: Sensible sweeps named by the design: the identification block and the pack
#: block. Either may be passed straight to :func:`plan`.
IDENTIFICATION_RANGE = (0xF100, 0xF1FF)
PACK_RANGE = (0xF200, 0xF2FF)

_DID_MAX = 0xFFFF

# --------------------------------------------------------------------------- #
# classifier thresholds (see the module docstring for the reasoning)
# --------------------------------------------------------------------------- #
CELL_MV = (1000, 5500)
CELL_SPREAD_MV = 500
CELL_MIN_WORDS = 4
CELL_MAX_WORDS = 96
#: At or above this word count a run is confidently a cell column.
CELL_CONFIDENT_WORDS = 8

#: Leading-u16 raw counts that read as a pack voltage.
PACK_V_SCALE01_RAW = (1500, 5000)   # x 0.1 -> 150.0..500.0 V
PACK_V_SCALE1_RAW = (150, 500)      # x 1   -> 150..500 V

SOC_MAX = 100

TEMP_C = (-40, 100)
TEMP_U16_RAW = (-400, 1000)         # x 0.1 -> -40.0..100.0 °C

IDENT_MIN_CHARS = 4
#: Bigger than any real DID payload; a defence against a runaway reply.
MAX_PAYLOAD_BYTES = 512

CONFIDENCE_RANK = {"high": 3, "medium": 2, "ambiguous": 1, "unknown": 0}

_HEX = re.compile(r"^[0-9A-F]+$")
#: 17 chars, no I/O/Q (the ISO 3779 VIN alphabet).
_VIN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


# --------------------------------------------------------------------------- #
# the planner
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SweepPlan:
    """A bounded, deterministic list of DIDs to read (ascending, stride ``step``)."""

    start: int
    end: int
    step: int
    max_reads: int
    dids: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.dids)

    def as_dict(self) -> dict:
        return {
            "start": "%04X" % self.start,
            "end": "%04X" % self.end,
            "step": self.step,
            "count": self.count,
            "max_reads": self.max_reads,
            "dids": ["%04X" % did for did in self.dids],
        }


def _did_int(value, *, field_: str) -> int:
    """A strict 16-bit DID from an int or a hex string (no floats, no bools)."""
    if isinstance(value, bool):
        raise ValueError(f"{field_}: not a DID: {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip().upper()
        text = text[2:] if text.startswith("0X") else text
        if not text:
            raise ValueError(f"{field_}: not a hex DID: {value!r}")
        try:
            number = int(text, 16)
        except ValueError:
            raise ValueError(f"{field_}: not a hex DID: {value!r}") from None
    else:
        raise ValueError(f"{field_}: not a hex DID: {value!r}")
    if not 0 <= number <= _DID_MAX:
        raise ValueError(f"{field_}: DID out of 16-bit range: {value!r}")
    return number


def _strict_int(value, *, field_: str, minimum: int | None = None) -> int:
    """An integer argument: int or a base-10 string, never a bool/float."""
    if isinstance(value, bool):
        raise ValueError(f"{field_}: not an integer: {value!r}")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        try:
            number = int(value.strip(), 10)
        except ValueError:
            raise ValueError(f"{field_}: not an integer: {value!r}") from None
    else:
        raise ValueError(f"{field_}: not an integer: {value!r}")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field_}: must be >= {minimum}")
    return number


def plan(start, end, step: int = 1, max_reads: int | None = None) -> SweepPlan:
    """Plan a bounded DID sweep from ``start`` to ``end`` inclusive.

    DIDs are 16-bit. ``step`` must be >= 1; a reversed range fails. The plan is
    refused when its length exceeds ``max_reads`` (default
    :data:`DEFAULT_MAX_READS`, 512) — the assertion is a *refusal*, not a silent
    truncation, because a sweep that quietly stops part-way would hide the DIDs
    it never asked about. A caller may raise the cap explicitly, up to
    :data:`ABSOLUTE_MAX_READS`; beyond that it is refused however it is asked.
    """
    start_id = _did_int(start, field_="start")
    end_id = _did_int(end, field_="end")
    step = _strict_int(step, field_="step", minimum=1)
    if end_id < start_id:
        raise ValueError("end must be >= start")
    cap = DEFAULT_MAX_READS if max_reads is None else _strict_int(
        max_reads, field_="max_reads", minimum=1)
    if cap > ABSOLUTE_MAX_READS:
        raise ValueError(
            f"max_reads must be <= {ABSOLUTE_MAX_READS} (an unbounded sweep is refused)"
        )
    dids = tuple(range(start_id, end_id + 1, step))
    if len(dids) > cap:
        raise ValueError(
            f"{len(dids)} reads exceeds the cap of {cap}; "
            f"narrow the range/raise --step, or raise max_reads explicitly "
            f"(<= {ABSOLUTE_MAX_READS})"
        )
    return SweepPlan(start=start_id, end=end_id, step=step, max_reads=cap, dids=dids)


# --------------------------------------------------------------------------- #
# the classifier
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Shape:
    """One interpretation of a payload."""

    kind: str
    scale: float | None
    units: str | None
    count: int | None
    reason: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "scale": self.scale, "units": self.units,
                "count": self.count, "reason": self.reason}


@dataclass(frozen=True)
class Verdict:
    """The classification of one payload.

    ``kind`` is the best-fit shape (or ``"unknown"``); ``confidence`` is one of
    ``high``/``medium``/``ambiguous``/``unknown``. When more than one shape fits,
    ``confidence`` is ``ambiguous`` and ``alternatives`` names the others.
    """

    kind: str
    confidence: str
    scale: float | None = None
    units: str | None = None
    count: int | None = None
    reason: str = ""
    alternatives: tuple[str, ...] = ()
    shapes: tuple[Shape, ...] = field(default=(), repr=False)

    @property
    def confident(self) -> bool:
        return self.confidence in ("high", "medium")

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "confidence": self.confidence,
            "scale": self.scale,
            "units": self.units,
            "count": self.count,
            "reason": self.reason,
            "alternatives": list(self.alternatives),
        }


def _did_or_none(did) -> int | None:
    """A valid 16-bit DID, or None. Never raises; out-of-range/junk → None."""
    if did is None or isinstance(did, bool):
        return None
    try:
        number = int(did, 16) if isinstance(did, str) else int(did)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= _DID_MAX else None


def _payload_bytes(payload_hex, did, full_reply: bool) -> bytes | None:
    """Normalise a payload to bytes, or None when it is not usable.

    Accepts plain data hex (what the service layer returns after ``62 <DID>``),
    spacing/colon separated hex, and ``0x``-prefixed hex. A full UDS reply of the
    form ``62<DID><data>`` is only stripped when the caller sets
    ``full_reply=True`` *and* a valid ``did`` is given — the service layer already
    hands over the data bytes alone, so a legitimate payload that happens to
    begin ``62 <DID>`` must never be silently shortened. Never raises.

    Input is bounded before it is upper-cased, scanned or converted, so an
    absurdly large string cannot make the "never raises" contract expensive.
    """
    if payload_hex is None:
        return None
    if isinstance(payload_hex, (bytes, bytearray)):
        text = bytes(payload_hex).hex()
    else:
        try:
            text = str(payload_hex)
        except Exception:  # noqa: BLE001 - a hostile __str__ must not raise through
            return None
    if not text.isascii():
        return None
    # A generous bound on the *raw* text (3x covers separators) before any work.
    if len(text) > MAX_PAYLOAD_BYTES * 3 + 16:
        return None
    text = text.strip().upper()
    for junk in (" ", ":", "-", "\n", "\t"):
        text = text.replace(junk, "")
    if text.startswith("0X"):
        text = text[2:]
    if full_reply:
        did_int = _did_or_none(did)
        if did_int is not None:
            prefix = "62%04X" % did_int
            if text.startswith(prefix):
                text = text[len(prefix):]
    if not text or len(text) % 2 or not _HEX.match(text):
        return None
    if len(text) // 2 > MAX_PAYLOAD_BYTES:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:  # pragma: no cover - _HEX already excludes this
        return None


def _words_be(raw: bytes) -> list[int]:
    return [int.from_bytes(raw[index:index + 2], "big")
            for index in range(0, len(raw) - 1, 2)]


def _cell_shape(raw: bytes) -> Shape | None:
    if len(raw) % 2 or len(raw) < CELL_MIN_WORDS * 2:
        return None
    words = _words_be(raw)
    if not CELL_MIN_WORDS <= len(words) <= CELL_MAX_WORDS:
        return None
    low, high = CELL_MV
    if any(word < low or word > high for word in words):
        return None
    spread = max(words) - min(words)
    if spread >= CELL_SPREAD_MV:
        return None
    return Shape("cell_voltages", 1.0, "mV", len(words),
                 f"{len(words)} big-endian u16 words in {low}-{high} mV, "
                 f"spread {spread} mV (< {CELL_SPREAD_MV})")


def _pack_shape(raw: bytes) -> Shape | None:
    if len(raw) < 2 or len(raw) % 2:
        return None
    word = int.from_bytes(raw[0:2], "big")
    if PACK_V_SCALE01_RAW[0] <= word <= PACK_V_SCALE01_RAW[1]:
        return Shape("pack_voltage", 0.1, "V", 1,
                     f"leading u16 {word} x 0.1 = {word * 0.1:.1f} V "
                     f"({PACK_V_SCALE01_RAW[0]}-{PACK_V_SCALE01_RAW[1]} counts)")
    if PACK_V_SCALE1_RAW[0] <= word <= PACK_V_SCALE1_RAW[1]:
        return Shape("pack_voltage", 1.0, "V", 1,
                     f"leading u16 {word} x 1 = {word} V "
                     f"({PACK_V_SCALE1_RAW[0]}-{PACK_V_SCALE1_RAW[1]} counts)")
    return None


def _soc_shape(raw: bytes) -> Shape | None:
    if len(raw) not in (1, 2):
        return None
    value = int.from_bytes(raw, "big")
    if value > 2 * SOC_MAX:
        return None
    scale = 1.0 if value <= SOC_MAX else 0.5
    scaled = value * scale
    if not 0.0 <= scaled <= float(SOC_MAX):
        return None
    return Shape("soc", scale, "%", 1,
                 f"{value} x {scale:g} = {scaled:.1f} % (0-{SOC_MAX} %)")


def _temp_shape(raw: bytes) -> Shape | None:
    if len(raw) == 1:
        byte = raw[0]
        signed = byte - 256 if byte >= 128 else byte
        if TEMP_C[0] <= signed <= TEMP_C[1]:
            return Shape("temperature", 1.0, "\u00b0C", 1,
                         f"{signed} \u00b0C (i8/u8, {TEMP_C[0]}..{TEMP_C[1]} \u00b0C)")
        return None
    if len(raw) == 2:
        word = int.from_bytes(raw, "big", signed=True)
        scaled = word * 0.1
        if TEMP_U16_RAW[0] <= word <= TEMP_U16_RAW[1] and TEMP_C[0] <= scaled <= TEMP_C[1]:
            return Shape("temperature", 0.1, "\u00b0C", 1,
                         f"{word} x 0.1 = {scaled:.1f} \u00b0C (u16, "
                         f"{TEMP_C[0]}..{TEMP_C[1]} \u00b0C)")
    return None


def _ident_shape(raw: bytes) -> Shape | None:
    if len(raw) < IDENT_MIN_CHARS or any(byte < 0x20 or byte > 0x7E for byte in raw):
        return None
    text = raw.decode("ascii").strip()
    if len(text) < IDENT_MIN_CHARS:
        return None
    if _VIN.match(text.upper()):
        return Shape("identification", None, None, None,
                     f"17-char VIN-shaped ASCII string {text!r}")
    alnum = text.replace(" ", "").replace("-", "")
    serial = (len(alnum) >= 6 and alnum.isalnum()
              and any(char.isdigit() for char in alnum)
              and any(char.isalpha() for char in alnum))
    label = "serial/part-shaped" if serial else "printable ASCII"
    return Shape("identification", None, None, None,
                 f"{label} string {text!r} ({len(raw)} chars)")


#: Preference order when several shapes fit; the first is reported as ``kind``.
_SHAPE_ORDER = ("cell_voltages", "pack_voltage", "soc", "identification", "temperature")


def classify(payload_hex, *, did=None, context: str | None = None,
             full_reply: bool = False) -> Verdict:
    """Classify one 0x22 payload. Never raises; unknown input → ``unknown``.

    ``payload_hex`` is the **data bytes** after ``62 <DID>`` (what the service
    layer returns). Pass ``full_reply=True`` and ``did`` only when handing over a
    complete ``62<DID><data>`` reply; the service layer must not, because a
    legitimate payload can itself begin with those bytes.

    ``did`` (int or 4-hex string) is otherwise unused. ``context`` is an optional
    hint (``"soc"``/``"temperature"``) that raises that shape's confidence when
    the caller already knows what the DID is expected to carry.
    """
    raw = _payload_bytes(payload_hex, did, full_reply)
    if raw is None:
        return Verdict("unknown", "unknown", reason="empty, odd-length or non-hex payload")

    shapes: list[Shape] = []
    cell = _cell_shape(raw)
    if cell:
        shapes.append(cell)
    pack = _pack_shape(raw)
    # A confident cell column suppresses the pack reading: no pack-voltage DID
    # returns 16+ bytes, so the leading-word pack match is a coincidence there.
    if pack and not (cell and cell.count >= CELL_CONFIDENT_WORDS):
        shapes.append(pack)
    for factory in (_soc_shape, _ident_shape, _temp_shape):
        shape = factory(raw)
        if shape and shape.kind not in {existing.kind for existing in shapes}:
            shapes.append(shape)

    if not shapes:
        return Verdict("unknown", "unknown",
                       reason="no shape fits within the conservative windows")
    if context:
        # The caller has told us what this DID is expected to carry; honour it
        # only when that shape actually fits, and drop the competing readings.
        preferred = [shape for shape in shapes if shape.kind == context]
        if preferred:
            shapes = preferred
    shapes.sort(key=lambda shape: _SHAPE_ORDER.index(shape.kind))
    best = shapes[0]
    others = tuple(shape.kind for shape in shapes[1:])

    if others:
        listed = ", ".join(others)
        confidence = "ambiguous"
        reason = (f"fits {best.kind} and also {listed} — "
                  f"reporting ambiguous rather than guessing. {best.reason}")
    else:
        confidence = _confidence_for(best, context)
        reason = best.reason
    return Verdict(best.kind, confidence, best.scale, best.units, best.count,
                   reason, others, tuple(shapes))


def _confidence_for(shape: Shape, context: str | None) -> str:
    if context and context == shape.kind:
        return "high"
    if shape.kind == "cell_voltages":
        return "high" if (shape.count or 0) >= CELL_CONFIDENT_WORDS else "medium"
    if shape.kind == "identification":
        # A part/serial string is weaker evidence than a VIN-shaped one.
        return "high" if "VIN-shaped" in shape.reason or (shape.count or 0) >= 6 else "medium"
    if shape.kind == "soc":
        return "medium"  # overlaps temperature at 0.1 without a context hint
    if shape.kind == "temperature":
        return "high" if shape.scale == 0.1 else "medium"
    return "high"  # pack_voltage


# --------------------------------------------------------------------------- #
# candidates + a proposed preset
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Candidate:
    """One DID proposed for a preset role (derived from a classification)."""

    role: str
    did: str
    kind: str
    confidence: str
    scale: float | None
    units: str | None
    count: int | None
    reason: str

    def as_dict(self) -> dict:
        return {"role": self.role, "did": self.did, "kind": self.kind,
                "confidence": self.confidence, "scale": self.scale,
                "units": self.units, "count": self.count, "reason": self.reason}


def candidates(entries) -> dict:
    """Best candidate per role from sweep ``entries`` (each carrying a verdict).

    Every shape a payload fits is offered as a candidate, so an ambiguous reply
    can back more than one role; the winner per role is the highest confidence,
    with ties broken by the lowest DID (deterministic).
    """
    best: dict[str, Candidate] = {}
    all_: list[Candidate] = []
    for entry in entries or []:
        verdict = entry.get("verdict")
        if not isinstance(verdict, Verdict) or verdict.confidence == "unknown":
            continue
        did_text = str(entry.get("did") or "").upper()
        if not did_text:
            continue
        for shape in verdict.shapes:
            confidence = verdict.confidence if len(verdict.shapes) == 1 else "ambiguous"
            candidate = Candidate(role=shape.kind, did=did_text, kind=shape.kind,
                                  confidence=confidence, scale=shape.scale,
                                  units=shape.units, count=shape.count, reason=shape.reason)
            all_.append(candidate)
            current = best.get(candidate.role)
            if current is None or _beats(candidate, current):
                best[candidate.role] = candidate
    return {"best": best, "all": all_}


def _beats(candidate: Candidate, current: Candidate) -> bool:
    if CONFIDENCE_RANK[candidate.confidence] != CONFIDENCE_RANK[current.confidence]:
        return CONFIDENCE_RANK[candidate.confidence] > CONFIDENCE_RANK[current.confidence]
    return candidate.did < current.did


#: Preset fields filled from each role.
_PRESET_FIELDS = {
    "cell_voltages": ("cells_did", "count", "scale"),
    "pack_voltage": ("pack_voltage_did", "pack_voltage_scale"),
    "soc": ("soc_did", "soc_scale"),
    "temperature": ("temp_did", "temp_scale"),
}

#: Roles that must be confident for a preset to be offered at all.
_GATE_ROLES = ("cell_voltages", "pack_voltage")


def build_preset(result_candidates, *, module: str = "7E7", soc_module: str | None = None,
                 bus: str | None = None) -> dict:
    """Turn the sweep's candidates into a battery-preset dict.

    Only the fields with a candidate are set; the rest keep the screen's
    defaults. ``verified`` is always False — a sweep proposes, it never certifies.
    """
    best = (result_candidates or {}).get("best") or {}
    preset = {
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
        "module": module,
        "soc_module": soc_module or module,
    }
    for role, fields in _PRESET_FIELDS.items():
        candidate = best.get(role)
        if candidate is None:
            continue
        preset[fields[0]] = candidate.did
        if role == "cell_voltages":
            preset["count"] = int(candidate.count or 0)
            preset["scale"] = float(candidate.scale or 1.0)
        else:
            preset[fields[1]] = float(candidate.scale if candidate.scale is not None else 1.0)
    preset["verified"] = False
    where = f" on {bus}" if bus else ""
    preset["source"] = (f"did-sweep{where} module {module} "
                        f"(proposed, unverified — confirm in the Raw console)")
    return preset


def preset_suggestion(result_candidates, *, module: str = "7E7",
                      soc_module: str | None = None, bus: str | None = None) -> dict:
    """A ``available``/``preset``/``confidence``/``notes`` wrapper for the UI.

    ``available`` is False when no *gate* role (cells or pack voltage) is
    confident, in which case the preset is returned empty and the caller must
    leave the owner's saved preset alone.
    """
    best = (result_candidates or {}).get("best") or {}
    gate = [best[role] for role in _GATE_ROLES
            if role in best and best[role].confidence in ("high", "medium")]
    preset = build_preset(result_candidates, module=module, soc_module=soc_module, bus=bus)
    confidence = {role: candidate.confidence for role, candidate in best.items()}
    notes: list[str] = []
    if not gate:
        preset = {key: value for key, value in preset.items()
                  if key in ("module", "soc_module", "verified", "source")}
        notes.append("Nothing was confidently classified (no cells or pack-voltage "
                     "candidate): leaving the saved preset alone.")
        return {"available": False, "preset": None, "confidence": confidence,
                "partial": preset, "notes": notes}
    for role, candidate in best.items():
        if role in _GATE_ROLES and candidate.confidence == "ambiguous":
            notes.append(f"{role}: DID {candidate.did} is ambiguous — "
                         "confirm before trusting it.")
        elif candidate.confidence == "ambiguous":
            notes.append(f"{role}: DID {candidate.did} fits more than one shape — "
                         "offered as a suggestion only.")
    if "cell_voltages" not in best:
        notes.append("No cells DID was confidently classified.")
    if "pack_voltage" not in best:
        notes.append("No pack-voltage DID was confidently classified.")
    if "soc" not in best:
        notes.append("No state-of-charge DID was classified; set it by hand.")
    return {"available": True, "preset": preset, "confidence": confidence,
            "partial": None, "notes": notes}


# --------------------------------------------------------------------------- #
# read-only guard (the sweep's only services)
# --------------------------------------------------------------------------- #
def read_command(did) -> str:
    """The wire request for one DID: ``22 <DID>``."""
    number = int(did, 16) if isinstance(did, str) else int(did)
    return "22%04X" % (number & _DID_MAX)


def session_commands(sweep: SweepPlan) -> tuple[str, ...]:
    """Every command a sweep of ``sweep`` will send, session first."""
    return ("1003",) + tuple(read_command(did) for did in sweep.dids)


def assert_read_only(commands) -> None:
    """Refuse any command that is not a read service, via ``raw_guard``.

    Uses :func:`pacifica_obd.raw_guard.classify` — the one tier table — so a
    sweep can never send a write/clear/flow-control command even if a later edit
    tries to add one. On this sweep only ``10`` (session) and ``22`` (read DID)
    are allowed.
    """
    from pacifica_obd import raw_guard

    for command in commands:
        decision = raw_guard.classify(command)
        if decision.tier != raw_guard.READ or decision.service not in READ_SERVICES:
            raise ValueError(
                f"refusing to send a non-read service: {command!r} ({decision.reason})"
            )
