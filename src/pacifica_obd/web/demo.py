"""Demo mode: a synthetic vehicle + a seeded database, so the whole UI is
explorable with no car and no adapter (design §6.2).

Everything here is deterministic and obviously fake: the VIN is a test VIN, the
signals are generated from a couple of sine waves, and the timestamps walk
backwards from "now". No real vehicle data is ever involved.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from pacifica_obd.profiles import year_from_vin
from pacifica_obd.store import Store
from pacifica_obd.web.bus import FAKE_VIN, FakeAdapter, FakeBackend

# One honest signpost for screenshots and for a user poking at the API.
DEMO_VIN = FAKE_VIN


def demo_backend() -> FakeBackend:
    """The synthetic adapter used by ``--demo`` (and by the tests)."""
    return FakeBackend(adapter=FakeAdapter(vin=DEMO_VIN))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def seed_store(store: Store | None) -> None:
    """Seed a database with two vehicles, sessions, DTCs and sample series.

    Idempotent: a database that already has a vehicle is left alone, so restarting
    ``--demo`` never duplicates rows.
    """
    if store is None or not store.available:
        return
    if store.vehicles():
        return

    now = datetime.now(timezone.utc)
    rng = random.Random(20211018)

    # -- vehicles ----------------------------------------------------------
    store.upsert_vehicle(
        DEMO_VIN, make="Chrysler", model="Pacifica Hybrid", year=year_from_vin(DEMO_VIN),
        profile_id="chrysler-pacifica-ru-phev", powertrain="phev", nickname="Demo van",
    )
    store.upsert_vehicle(
        "2C4RC1BG0MR000002", make="Chrysler", model="Pacifica", year=2021,
        profile_id="chrysler-pacifica-ru-gas", powertrain="gas", nickname="Loaner",
    )

    # -- sessions ----------------------------------------------------------
    sessions = []
    for index, (command, age_min, status) in enumerate(
        [("dtc-all", 4, "ok"), ("capture", 62, "ok"), ("modules", 190, "ok"),
         ("scan", 640, "ok")]
    ):
        started = now - timedelta(minutes=age_min)
        session_id = store.start_session(command, {"demo": True, "index": index}, "/dev/cu.demo")
        store.finish_session(session_id, status, DEMO_VIN)
        store._execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (_iso(started), _iso(started + timedelta(seconds=42)), session_id),
        )
        sessions.append(session_id)
        store.event(session_id, "command", command, {"demo": True})

    latest = sessions[0]

    # -- modules + readings ------------------------------------------------
    for bus, rid, resp in (("can-c", "7E0", "7E8"), ("can-c", "7E2", "7EA"),
                           ("can-c", "7E7", "7EF"), ("can-ihs", "783", "503")):
        store.add_module(latest, DEMO_VIN, bus,
                         {"request_id": rid, "response_id": resp, "kind": "positive"})
    for rid, did, name, hex_value, ascii_value in (
        ("7E0", "F190", "VIN", DEMO_VIN.encode().hex().upper(), DEMO_VIN),
        ("7E0", "F187", "Spare part number", "504152542D31323334", "PART-1234"),
        ("7E0", "F188", "ECU software number", "535730303031", "SW0001"),
        ("7E2", "F190", "VIN", DEMO_VIN.encode().hex().upper(), DEMO_VIN),
        ("7E7", "F191", "ECU hardware number", "485730303037", "HW0007"),
    ):
        store.add_reading(latest, DEMO_VIN, "can-c", rid, did, name, hex_value,
                          ascii_value, "ok", None, category="module-info")

    # -- DTCs --------------------------------------------------------------
    store.add_dtcs(latest, DEMO_VIN, "can-c", "7E0", [
        {"code": "P0300", "failure_type": "00", "status": "08",
         "flags": ["confirmed", "testFailed"], "description": "Random/Multiple Cylinder Misfire Detected"},
        {"code": "P0420", "failure_type": "00", "status": "04",
         "flags": ["pending"], "description": "Catalyst System Efficiency Below Threshold"},
    ], "5902FF03000008420042000")
    store.add_dtcs(latest, DEMO_VIN, "can-c", "7E7", [
        {"code": "U0100", "failure_type": "00", "status": "08",
         "flags": ["confirmed"], "description": "Lost Communication With ECM/PCM"},
    ], "5902FFC10000 08")

    # -- sample series (the replay buffer, §4.4) ---------------------------
    signals = [
        ("PCM", "0C", "RPM", "rpm", lambda phase: 800 + 220 * math.sin(phase) + rng.uniform(-12, 12)),
        ("PCM", "05", "COOLANT_TEMP", "degC",
         lambda phase: 88 + 5 * math.sin(phase / 3.0) + rng.uniform(-0.4, 0.4)),
        ("PCM", "0D", "SPEED", "km/h", lambda phase: max(0.0, 42 + 42 * math.sin(phase * 0.5))),
        ("PCM", "42", "CONTROL_MODULE_VOLTAGE", "V",
         lambda phase: 14.1 + 0.15 * math.sin(phase * 2.0) + rng.uniform(-0.03, 0.03)),
        ("7E7", "F40D", "BPCM_pack_voltage", "V", lambda phase: 352 + 8 * math.sin(phase / 4.0)),
    ]
    step = timedelta(seconds=2)
    count = 900  # 30 minutes at 2 s
    start = now - step * count
    for module, did, name, unit, fn in signals:
        for index in range(count):
            ts = start + step * index
            phase = index / 30.0
            value = round(fn(phase), 3)
            store.add_sample(latest, DEMO_VIN, "can-c", module, did, name, value,
                             str(value), unit, "ok", None, ts=_iso(ts))
