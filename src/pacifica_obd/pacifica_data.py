"""Bundled Pacifica (RU-platform) diagnostic dataset.

This package ships one JSON resource, ``data/pacifica.json``, that IS the
project's dataset: module addressing, per-module data items, writable
configuration and a DTC text table for the Chrysler Pacifica (RU) family in
gas and hybrid/PHEV form.  It is read through :mod:`importlib.resources`, so it
works from a source checkout or an installed wheel/zip.  There is no external
database, no user-supplied path and no network access.

Schema
------
``pacifica.json`` is one JSON object with these top-level keys:

``modules``
    ``list[dict]``.  One entry per addressed ECU:

    * ``name``          human-readable unit name
    * ``ecu``           stable ECU identifier (e.g. ``"BODY_CHRYSLER"``)
    * ``years``         applicable model-year span, clamped to 2017-2027
    * ``powertrain``    ``"gas"`` or ``"phev"``
    * ``bus``           ``"can-c"`` or ``"can-ihs"``
    * ``inittype``      raw upstream bus code (7 = CAN-C, 9 = CAN-IHS)
    * ``ecu_address``   raw ECU byte address
    * ``request``       request CAN ID, ``"0xNNN"`` hex
    * ``response``      response CAN ID, ``"0xNNN"`` hex
    * ``response_rule`` ``"request+8"``, ``"request-0x280"``, ``"request+1"``
                        or ``"explicit"`` (how ``response`` relates to
                        ``request``)
    * ``adapter``       required adapter index
    * ``baudrate``      bus bit rate
    * ``device_ids``    upstream device identifiers for this ECU
    * ``options``       upstream applicability labels (e.g. ``"MY2017 PowerNet"``)

``dids``
    ``dict[str, list[dict]]`` keyed by ECU.  Data items the dataset ties to a
    module: ``{"kind", "name", "unit"}`` where ``kind`` is ``"pid"`` (live-data
    parameter) or ``"routine"`` (actuator/diagnostic routine).  ``unit`` may be
    ``None``.

``adaptations``
    ``dict[str, list[dict]]`` keyed by ECU.  Writable configuration entries:
    ``{"request", "bit", "size", "name", "group", "array", "values"}`` where
    ``size`` is the field width in bits and ``values`` is the list of allowed
    labels (``None`` when the field is numeric/free-form).

``dtc_text``
    ``dict[str, str]`` mapping an uppercase hex DTC code (e.g. ``"0CD1"``) to a
    short English description.

``seed_key``
    Marker only: per-ECU identifiers exist, but no seed/key algorithm is
    shipped (the project keeps a pluggable provider model elsewhere).

Public API
----------
:func:`dataset`, :func:`modules`, :func:`dids`, :func:`adaptations`,
:func:`dtc_text`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

_RESOURCE = "pacifica.json"

__all__ = ["dataset", "modules", "dids", "adaptations", "dtc_text"]


@lru_cache(maxsize=1)
def dataset() -> dict:
    """Return the whole bundled dataset (parsed once, then cached).

    The returned structures are shared, cached and treated as read-only; do not
    mutate them in place.
    """
    resource = resources.files("pacifica_obd").joinpath("data", _RESOURCE)
    return json.loads(resource.read_text(encoding="utf-8"))


def modules() -> list[dict]:
    """Every addressed Pacifica ECU (gas and hybrid; OBD-II block and CAN-IHS)."""
    return dataset()["modules"]


def _lookup(mapping: dict, module: str) -> list[dict]:
    """Case-insensitive ECU lookup; returns ``[]`` for an unknown module."""
    if module in mapping:
        return mapping[module]
    wanted = module.strip().upper()
    for key, value in mapping.items():
        if key.upper() == wanted:
            return value
    return []


def dids(module: str) -> list[dict]:
    """Data items for ``module`` (case-insensitive ECU id); ``[]`` if none."""
    return _lookup(dataset()["dids"], module)


def adaptations(module: str) -> list[dict]:
    """Writable configuration entries for ``module``; ``[]`` if none."""
    return _lookup(dataset()["adaptations"], module)


def dtc_text(code: str) -> str | None:
    """Short English text for ``code`` (hex, optional ``0x``); ``None`` if unknown."""
    key = code.strip().upper()
    if key.startswith("0X"):
        key = key[2:]
    return dataset()["dtc_text"].get(key)
