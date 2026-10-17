"""Vehicle profiles: data-driven definitions per Pacifica/Stellantis variant.

Profiles describe *which* modules live on *which* bus and which identification
DIDs to read. Selection is a best-effort function of model year, powertrain, and
(optionally) the VIN; discovered ECUs and a manual override win over defaults.
See docs/vehicle-support.md for the full analysis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pacifica_obd.uds import INFO_DIDS

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def normalize_vin(vin: str | None) -> str | None:
    """Upper-case/strip a VIN; returns None for empty input."""
    if not vin:
        return None
    return vin.strip().upper() or None


def looks_like_vin(text: str | None) -> bool:
    """True for a plausible 17-char VIN (rejects blanks/all-zeros)."""
    if not text or not _VIN_RE.match(text.upper()):
        return False
    return any(char not in "0" for char in text)

# Module name -> bus, distilled from NHTSA scan reports for Pacifica (RU).
_COMMON_CAN_C = [
    "PCM", "TCM", "BCM", "ABS/EBCM", "ORC", "EPS", "IPC", "SCCM", "RFH", "ESM",
    "OCM", "FFCM", "ACC", "PTS", "APM", "ESC", "ECM",
]
_COMMON_CAN_IHS = [
    "PLGM", "ICS", "HVAC", "DDM", "PDM", "DMRL", "DMRR", "PSDML", "PSDMR",
    "MSM", "CSWM",
]
# PHEV-only modules (HCP/EAC are hybrid, not on the gas vans).
_PHEV_CAN_C = ["BPCM", "OBCM", "AHCP", "HCP", "EAC", "IDCM", "EBBM"]


@dataclass(frozen=True, eq=False)
class VehicleProfile:
    id: str
    make: str
    model: str
    years: tuple[int, ...]
    powertrain: str  # "gas" | "phev" | "any"
    platform: str = "RU"
    module_buses: dict[str, list[str]] = field(default_factory=dict)
    info_dids: tuple[int, ...] = tuple(INFO_DIDS)
    notes: str = ""

    def applies(self, year: int | None, powertrain: str | None) -> bool:
        if year is not None and self.years and year not in self.years:
            return False
        if powertrain is not None and self.powertrain not in ("any", powertrain):
            return False
        return True


def _years(*spans: tuple[int, int]) -> tuple[int, ...]:
    out: list[int] = []
    for start, end in spans:
        out.extend(range(start, end + 1))
    return tuple(out)


RU_GAS = VehicleProfile(
    id="chrysler-pacifica-ru-gas",
    make="Chrysler",
    model="Pacifica",
    years=_years((2017, 2027)),
    powertrain="gas",
    module_buses={"can-c": _COMMON_CAN_C, "can-ihs": _COMMON_CAN_IHS},
    notes="3.6L V6 gasoline (RU platform; incl. Voyager/Grand Caravan). SGW from 2018+.",
)

RU_PHEV = VehicleProfile(
    id="chrysler-pacifica-ru-phev",
    make="Chrysler",
    model="Pacifica Hybrid",
    years=_years((2017, 2027)),
    powertrain="phev",
    module_buses={"can-c": _COMMON_CAN_C + _PHEV_CAN_C, "can-ihs": _COMMON_CAN_IHS},
    notes="Pacifica Hybrid (PHEV). Adds charger/battery modules on CAN-C.",
)

DEFAULT_PROFILE = RU_GAS

# VIN position 10 (index 9) encodes the model year.
_VIN_YEAR_CODES = {
    "H": 2017, "J": 2018, "K": 2019, "L": 2020, "M": 2021,
    "N": 2022, "P": 2023, "R": 2024, "S": 2025, "T": 2026, "V": 2027,
}


def year_from_vin(vin: str | None) -> int | None:
    if not vin or len(vin) < 10:
        return None
    return _VIN_YEAR_CODES.get(vin[9].upper())


def all_profiles() -> list[VehicleProfile]:
    return [RU_GAS, RU_PHEV]


def resolve(year: int | None = None, powertrain: str | None = None,
            vin: str | None = None, override: str | None = None) -> VehicleProfile:
    """Best-effort profile selection. `override` is a profile id."""
    if override:
        for profile in all_profiles():
            if profile.id == override:
                return profile
    for profile in all_profiles():
        if profile.applies(year, powertrain):
            return profile
    return DEFAULT_PROFILE
