"""Typed domain models shared by the CLI, TUI, and services.

Keep these UI-agnostic: no Rich, no argparse, no printing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Vehicle:
    vin: str | None = None
    make: str | None = None
    model: str | None = None
    year: int | None = None
    nickname: str | None = None
    profile_id: str | None = None
    powertrain: str | None = None


@dataclass
class Module:
    request_id: str
    response_id: str | None = None
    bus: str | None = None
    kind: str | None = None
    vin: str | None = None
    part_number: str | None = None


@dataclass
class Dtc:
    code: str
    failure_type: str | None = None
    status: str | None = None
    flags: list[str] = field(default_factory=list)


@dataclass
class Reading:
    did: str
    module: str
    name: str | None = None
    status: str = "ok"
    hex: str | None = None
    ascii: str | None = None


@dataclass
class CaptureResult:
    """Everything read from a vehicle in one pass."""

    vin: str | None = None
    profile_id: str | None = None
    modules: list[Module] = field(default_factory=list)
    dtc_reads: list[dict] = field(default_factory=list)  # {"bus","request_id","status","dtcs":[Dtc]}
    readings: list[Reading] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)
