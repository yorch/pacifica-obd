# Vehicle support: other Pacificas, Stellantis minivans, and a data-driven `VehicleProfile`

`pacifica-obd` is hardwired to **one** vehicle: a 2021 Chrysler Pacifica (RU), gas.
This document asks how far the existing transport + UDS approach stretches across
the rest of the Pacifica RU family (2017–2027, gas and PHEV, Voyager, Grand
Caravan), what is genuinely different on those vans, and what has to become
**data** instead of code to support them.

It is an analysis + design proposal. Nothing here is a claim that an untested
model year "works" — see [§7](#7-how-to-verify-with-a-real-car).

Related: [`enhanced-diagnostics.md`](enhanced-diagnostics.md) (UDS roadmap,
P0–P6), [`chrysler-sgw.md`](chrysler-sgw.md) (Security Gateway).

## TL;DR

- The **transport layer does not change at all**. Every vehicle in scope is
  classic CAN (ISO 15765-2) over the same two diagnostic buses: **CAN-C**
  (500 kbit/s, DLC pins 6/14) and **CAN-IHS** (125 kbit/s, DLC pins 3/11). The
  STN1170 adapter already reaches both, and the repo already pins the matching
  presets (`33` / `53`).
- The **module map, bus assignment, request/response IDs and DID set** change —
  by model year, by powertrain (PHEV), and by trim/equipment. Those are exactly
  the things currently hardcoded.
- The **generic OBD-II half** (`dtc`, `live`, `readiness`, `freeze`, `vin`,
  `scan`, `monitor`) should work on every model year in scope, gas or PHEV,
  because OBD-II is federally mandated on all of them. This is the tool's
  strongest cross-year asset.
- The **UDS half** is where the interesting risks live: response IDs that are
  **not** `request + 8` for most modules, SGW-gated writes from 2018, a PHEV-only
  controller family, and no public year-by-year DID matrix.
- **FORScan is irrelevant here** — it explicitly supports only Ford, Mazda,
  Lincoln and Mercury ("Vehicles other than Ford, Mazda, Lincoln, Mercury are not
  supported!"). The Stellantis reference tools are **AlfaOBD**, **Appcar
  DiagFCA**, and **JScan** ([FORScan](https://forscan.org/home.html),
  [AlfaOBD](https://www.alfaobd.com/supported_cars.html),
  [Appcar](https://appcar-diagfca.com/en/supported-vehicles/chrysler-pacifica),
  [JScan](https://jscan.net/fca-security-gateway-module-basic-info-and-location/)).
- **The vehicle half is already the right shape**: `profiles.py` has `RU_GAS` /
  `RU_PHEV` and year/powertrain selection, and `uds.py` centralises the bus and
  DID tables. What is missing is the **addressing layer** (the `−0x280` rule),
  **evidence/provenance**, and a **correct year span** (2017–2027, not 2017–2024)
  — see [§5.7](#57-relation-to-the-current-profilespy).

## 1. Scope: the RU minivan family

The Chrysler Pacifica (RU) entered production in 2016 for **model year 2017** and
is still in production; the 2027 refresh keeps the **same 3.6 L Pentastar V6**,
AWD and Stow 'n Go. Two other nameplates are the **same vehicle**:

| Nameplate | Where / when | Platform |
|---|---|---|
| **Chrysler Pacifica** | US, MY2017–present (gas + PHEV) | RU |
| **Chrysler Voyager** | US, MY2020–2026 (fleet-only 2022–2024, retail 2025; retained pre-facelift styling) | RU |
| **Chrysler Grand Caravan** | Canada, MY2021–present | RU |
| **Dodge Grand Caravan** | US/CA, MY2008–2020 | **RT — different platform** |

So "other Pacificas" and "Stellantis minivans" mostly collapse into one platform
with three nameplates. The RT Dodge Grand Caravan is *not* an RU van and needs
its own profile ([Wikipedia](https://en.wikipedia.org/wiki/Chrysler_Pacifica_(minivan))).

### Powertrains

| Powertrain | Years | Notes |
|---|---|---|
| **3.6 L Pentastar V6 (gas)** | 2017–2027 | ZF 948TE 9-speed automatic; AWD added for MY2020 (Launch Edition) / MY2021 broadly |
| **Pacifica Hybrid (PHEV)** | 2017–2026 | 16 kWh pack, SiEVT + e-CVT, dual electric motors; **FWD only**; discontinued Jan 2026 after a short 2026 run |

### Milestones that matter diagnostically

- **MY2018 — Security Gateway (SGW).** FCA introduced the SGW "in some models in
  the 2018 model year and all models 2019 going forward"; AlfaOBD dates it to
  MY2018. Appcar: "Most FCA vehicle starting from the 2018 model year (produced
  from the summer 2017) have a Security Gateway Module." ([JScan](https://jscan.net/fca-security-gateway-module-basic-info-and-location/),
  [AlfaOBD](https://www.alfaobd.com), [Appcar](https://appcar-diagfca.com/en/how-it-works/))
- **MY2020 — AWD** for gas models (not PHEV).
- **MY2021 — facelift / Uconnect 5, and an architecture change.** New exterior,
  **Android-based Uconnect 5** with a 10.1" display, and a dedicated
  **Telematics Box Module (TBM)**; the TBM is the source of many `B1498`
  communication faults on 2021+ vans. More importantly for us, Uconnect 5 runs on
  FCA's **"Atlantis"** electrical architecture, which — in FCA's own words —
  *"replaces Powernet as the next generation of FCA core in-vehicle electronics
  communication"*, and the 2021 Pacifica is its debut vehicle. So **MY2021 is an
  architecture boundary, not just a styling refresh**, and it lines up with
  where opendbc splits its Pacifica firmware groups (2017–18 / 2019–20 / 2021–23).
  ([Wikipedia](https://en.wikipedia.org/wiki/Chrysler_Pacifica_(minivan)),
  [go-parts B1498](https://www.go-parts.com/garage/obd-b1498-chrysler-pacifica-2021-2026),
  [FCA press release](https://www.prnewswire.com/news-releases/fcas-all-new-uconnect-5-global-platform-is-the-most-advanced-uconnect-system-ever-powerful-personalized-connected-and-easy-to-use-300993133.html),
  [Stellantis Media](https://media.fcanorthamerica.com/newsrelease.do?id=21511&mid=102))
- **MY2022–2024 — Voyager is fleet-only**; **MY2025** returns to retail;
  **MY2027** refresh is cosmetic (V6 retained, styling only). So across the whole
  2017–2027 run there is exactly **one architecture break** — the MY2021
  Atlantis/Uconnect 5 change — which makes the year axis unusually friendly.
- **MY2026 — PHEV discontinued** (January 2026).

Vendor coverage claims broadly agree on the span but differ, and they are
marketing coverage, not guarantees for every controller:

| Tool | Published Pacifica coverage |
|---|---|
| **Appcar DiagFCA** | Chrysler Pacifica **2017–2026** (RU), separate "Hybrid LAN Network" ECU group |
| **AlfaOBD** | `CHRYSLER PACIFICA (RU) 2016+`, recommended adapter OBDLink MX+ / EX |
| **JScan** | Chrysler **Pacifica RU 2017+**; SGW bypass describes it as `RU 2017+` |

## 2. Support matrix by model year and powertrain

Legend: **Supported** = implemented and adapter-tested, with the 2021 gas van as
the intended baseline; **Likely** = same protocol/architecture, no code change
expected, *not yet observed on that vehicle*; **Unverified** = plausibly fine but
a real unknown (new architecture element); **Out of scope** = deliberately not
attempted.

> Honesty note: even in the 2021 column, only the **generic OBD-II** commands are
> genuinely exercised today. The UDS commands (P1–P4) are implemented and
> adapter-tested, but per [`enhanced-diagnostics.md`](enhanced-diagnostics.md)
> they still need a car to populate. Until §7 is run on a vehicle, read every UDS
> cell below as **"implemented, target"** rather than "proven".

| Model years | Powertrain | Generic OBD-II | UDS scan CAN-C | UDS scan CAN-IHS | All-module DTC (`19 02`) | Module info DIDs | PHEV controllers | Clears / writes |
|---|---|---|---|---|---|---|---|---|
| 2017 | gas | **Supported** (std) | Likely | Likely | Likely | Likely | — | Out of scope (but **no SGW** — writes may actually pass) |
| 2017 | PHEV | **Supported** (std) | Likely | Likely | Likely | Likely | Likely (HYBRID @ `7E2`) | Out of scope (**no SGW**) |
| 2018–2020 | gas | **Supported** (std) | Likely (reads pass SGW) | Likely | Likely | Likely | — | Out of scope (SGW) |
| 2018–2020 | PHEV | **Supported** (std) | Likely | Likely | Likely | Likely | Likely | Out of scope (SGW) |
| **2021–2024** | **gas** | **Supported** | **Supported** (target) | **Supported** (target) | **Supported** (target) | **Supported** (target) | — | Out of scope (SGW) |
| 2021–2024 | PHEV | **Supported** (std) | Likely | Likely | Likely | Likely | Likely | Out of scope (SGW) |
| 2025–2026 | gas | Likely | Likely | Likely | Likely | Likely | — | Out of scope (SGW) |
| 2025–2026 | PHEV | Likely | Likely | Likely | Likely | Likely | Likely | Out of scope (SGW) |
| 2027 | gas | Likely | **Unverified** (refresh) | Unverified | Unverified | Unverified | — | Out of scope |
| 2008–2020 **Dodge Grand Caravan (RT)** | gas | Likely (std OBD-II) | **Unverified** — different platform | Unverified | Unverified | Unverified | — | Out of scope |

Reads-only is the design intent. The repo's own rule stands: *"The SGW blocks
writes on 2018+ vehicles; reads pass. Do not assume a write will succeed."*

### Where "Out of scope" is a safety statement, not a capability statement

Clearing DTCs and adaptations are blocked by the SGW on 2018+ vans. On a
**2017** van there is no SGW, so a write *would* likely be accepted by the
vehicle — which makes it more dangerous, not less. `docs/chrysler-sgw.md` already
treats writes as a separate, deliberate project; keep that.

## 3. What actually differs diagnostically

### 3.1 Bus layout — stable

| Bus | Speed | DLC pins | Adapter preset | Repo key |
|---|---|---|---|---|
| **CAN-C** (HS, powertrain/chassis) | 500 kbit/s | **6 / 14** | `STP 33` | `can-c` |
| **CAN-IHS** (MS, body/comfort) | 125 kbit/s | **3 / 11** | `STP 53` | `can-ihs` |
| (CAN-CH) | — | 12 / 13 | needs a *gray* cross-adapter | not used on RU |

Appcar documents the pin/rate mapping and states that STN1170/2120/2230/2231
interfaces support CAN-IHS natively; AlfaOBD's pin table names pins 3/11 "CAN
Middle speed" and 6/14 "CAN High speed"
([Appcar](https://appcar-diagfca.com/en/how-it-works/),
[AlfaOBD](https://www.alfaobd.com/androidhelp.html)).

**Caveat found on the vehicle side:** the two buses are physically star-wired,
and the *star connector* itself is a known failure point on 2017–2018 vans
(loose pin retention → intermittent `U0001`/loss-of-communication across many
modules). If a scan finds half a module map, suspect the star connector before
blaming the tool ([pacificaforums](https://www.pacificaforums.com/threads/failed-can-bus-star-connector-2017-hybrid.53931),
[go-parts U0001](https://www.go-parts.com/garage/obd-u0001-chrysler-pacifica-2017-2018)).

### 3.2 Module inventory — mostly stable, PHEV adds a family, trim adds the rest

Appcar's supported-module list for the RU Pacifica (2017–2026) is the best public
inventory. Grouped by *its* bus annotation:

**CAN-C (powertrain/chassis)** — PCM, TCM, BCM (CAN-C instance), ABS/ABSO,
EBBM, ORC, OCM, EPS, IPC, ESM, RFH, PTS, FFCM, ACC, DTCM, HCP, FDCM (AWD),
plus `SGW` itself.

**CAN-IHS (cabin/body)** — BCM (CAN-IHS instance), HVAC, HVACR, ICS, RADIO, AMP,
ANC, DDM, PDM, DMRL, DMRR, PSDML, PSDMR, PLGM, MSM, MSMP, CSWM, HSM, FSM, DSM,
CVPM, LBSS, RBSS, TBM/TBM2, EOMR, VRM, EDM, AHLM, ITM, clock.

**PHEV-only (Appcar calls this the "Hybrid LAN Network")** — **HCP** (Hybrid
Control Processor), **AHCP** (Auxiliary HCP), **BPCM** (Battery Pack Control
Module), **OBCM** (On Board Charging Module), **IDCM** (Integrated Dual Charger
Module), **EAC** (Electric A/C Compressor), **APM** (Accessory Power Module),
**EBBM** (Electronic Brake Booster Module), plus hybrid-flavour PCM/ABS/ORC/ESM.

([Appcar Pacifica](https://appcar-diagfca.com/en/supported-vehicles/chrysler-pacifica))

Two important readings of that list:

1. **It is a union over all years, trims and powertrains**, not one van's
   inventory. A base Voyager has a fraction of it. Any static map *will* list
   modules that are absent — absence must be a normal, recorded outcome.
2. The **Hybrid LAN** label suggests the PHEV controllers may sit on their own
   network. This is **not confirmed** as a third DLC-reachable bus. At least the
   HCP is reachable on CAN-C at `0x7E2` (see §3.3), and a 2020 PHEV factory scan
   report lists its modules under CAN-C and the interior bus, with no third bus.
   Treat "Hybrid LAN" as *a logical grouping in a third-party tool* until
   verified on a car.

The repo's own CAN-IHS list (PLGM, ICS, HVAC, DDM/PDM/DMRL/DMRR, PSDML/PSDMR,
MSM/CSWM) is consistent with a 2020 PHEV factory scan report that puts PCM, BCM,
ORC, EPS, IPC, SCCM, RFH and ESM on CAN-C and the interior modules on the
interior bus ([NHTSA factory scan report](https://static.nhtsa.gov/odi/inv/2022/INRD-PE22008-13852P1.pdf)).

### 3.3 Diagnostic addressing — the biggest surprise, and the strongest argument for data

This is the single most important finding for the design. Using the
**`comma.ai` openpilot/opendbc** Chrysler database as a real, current,
vehicle-observed source:

- Most FCA modules respond at **`request − 0x280`**, not `request + 8`.
  `CHRYSLER_RX_OFFSET = -0x280` in
  [`opendbc/car/chrysler/values.py`](https://github.com/commaai/opendbc/blob/master/opendbc/car/chrysler/values.py).
- The **OBD-II address block** (`7E0`–`7E7` requests → `7E8`–`7EF` responses)
  *does* use `request + 8`, because it is legislated by ISO 15765-4.

Observed addresses (from
[`fingerprints.py`](https://github.com/commaai/opendbc/blob/master/opendbc/car/chrysler/fingerprints.py)),
stable across **Pacifica 2017–18, 2019–20, 2021–23 gas, and both hybrid
generations** — and also across Durango, Grand Cherokee and Ram 1500:

| ECU | Request | Response | Rule |
|---|---|---|---|
| PCM / engine | `0x7E0` | `0x7E8` | `+8` |
| TCM / transmission | `0x7E1` | `0x7E9` | `+8` |
| HCP / hybrid | `0x7E2` | `0x7EA` | `+8` |
| IPC / combinationMeter | `0x742` | `0x4C2` | `−0x280` |
| ORC / SRS | `0x744` | `0x4C4` | `−0x280` |
| ABS | `0x747` | `0x4C7` | `−0x280` |
| FFCM / fwdRadar | `0x753` | `0x4D3` | `−0x280` |
| EPS | `0x75A` | `0x4DA` | `−0x280` |
| **ABS alternate (hybrid)** | `0x7E4` | `0x7EC` | `+8`, "**not on all hybrid platforms**" |

**The PHEV's address map differs from the gas van's.** opendbc queries the same
gas/hybrid request set (`abs, hybrid, engine, transmission`) at both offset
rules, and the *recorded* hybrid results are narrower: the PHEVs list no
`transmission` firmware at `0x7E1` and no `abs` at `0x747`, while the gas vans
list both. Instead, hybrids have **`hybrid` (`HCP`) at `0x7E2`** and, on some
platforms, the alternate **ABS at `0x7E4`**. That is consistent with the PHEV's
SiEVT/e-CVT being coordinated by the **HCP** (a.k.a. PIM — Hybrid Control
Processor / Power Inverter Module, which drives both motors) rather than a
standalone TCM at the gas address. Caveat: opendbc only records ECUs it actively
queries, so absence there is evidence, not proof — confirm on a car (§7).
([opendbc fingerprints](https://github.com/commaai/opendbc/blob/master/opendbc/car/chrysler/fingerprints.py),
[NHTSA TSB — HCP/AHCP(PIM)/PCM/BPCM flashed together](https://static.nhtsa.gov/odi/tsbs/2022/MC-10214604-9999.pdf),
[go-parts P2797 — HCP/AHCP software](https://www.go-parts.com/garage/obd-p2797-chrysler-pacifica-hybrid-2017-2019-phev))

Three consequences:

1. **`request + 8` is wrong for most modules.** A scanner that assumes the OBD
   offset would look for IPC at `0x74A`, get nothing, and under-report the van.
   The current repo avoids this by *not* filtering — `_collect_frames` groups by
   whatever header answered. That is the right instinct; make it explicit.
2. **The addresses are stable across the years opendbc covers (2017–2023) and
   across both powertrains.** Good news: the address map is small and highly
   reusable. Bad news for hardcoding: it is *almost* stable, and the exceptions
   (`0x7E4` hybrid ABS, AWD-only FDCM, no-TCM-on-PHEV) are exactly the cases that
   need per-profile data.
3. **The family can switch addressing style.** On Jeep Cherokee (KL, 5th gen) the
   same vendor code uses **29-bit** addresses (`0x18DA10F1` for engine). FCA can
   and does move. Any address representation must not assume 11-bit.

### 3.4 DIDs — standard block is reusable, proprietary block is missing

The repo already reads the ISO 14229 identification block `F186`–`F191`
(`_DID_NAMES` in `cli.py`). Those are **standard** and portable across all years
and modules. Beyond that:

- opendbc uses a **non-standard FCA identification DID `0xF132`** plus the
  standard *System Supplier ECU Software Number* (`0xF194`), for
  `abs / eps / srs / fwdRadar / combinationMeter / hybrid / engine /
  transmission` ([values.py](https://github.com/commaai/opendbc/blob/master/opendbc/car/chrysler/values.py)).
  `F132` is a useful, cheap cross-check to add.
- The **live-data / adaptation DID map** (the bulk of AlfaOBD's value) is
  **not public**. `enhanced-diagnostics.md` §7 already flags the reverse-engineered
  `fca-ecu-diagnostics` dataset as a lead (repos now 404).

So: identification reads are portable and testable; proprietary DID semantics
are per-ECU and must remain a loadable, replaceable database.

### 3.5 Sessions, padding, timing

No verified public RU year-by-year matrix exists. What we know:

- The repo currently **probes with `10 03` (extended session)**. That is the
  conventional choice, but on 2018+ vans it is a *session-change request* flowing
  into SGW-protected territory. It is not guaranteed to be accepted, and its
  acceptance may differ by module and year. This is a **must-verify** item (§7).
- opendbc's identification queries send **no session control** — plain
  `22 F132` in the default session. So at least *some* reads work in the default
  session on these vehicles.
- Multi-frame responses (17-byte VIN, 10-char part numbers) rely on ISO-TP
  flow-control, which the adapter handles. `uds.messages_from_frames` enforces
  classic-CAN framing (≤8 bytes, single-frame length 1–7, sequenced consecutive
  frames) — correct and unaffected by vehicle choice.
- Padding: ELM/STN `ATCAF1` auto-formats; do not disable spacing (`ATS1`).
  Padding/`STmin` differences are the usual cause of "works on my car, not
  yours" and belong in profile data, not constants.

### 3.6 Security Gateway — a year-gated, reads-pass firewall

- Firewall between the **public** sector (DLC + telematics/radio) and the
  **private** sector (everything else); authentication is restricted to
  Chrysler-licensed devices ([JScan](https://jscan.net/fca-security-gateway-module-basic-info-and-location/)).
- **Reads pass; writes are blocked**: clears, adaptations, activations.
- The standard bypass is a **"Chrysler 12+8" cable** that replaces the SGW's two
  connectors — so it un-gates *everything* downstream, both buses.
- **Location varies by year/trim**: behind the glove box, lower centre stack, or
  behind the radio. OBD Genie's walkthrough notes "2018 and newer Chrysler
  Pacifica **and 2020 and newer Chrysler Voyager**", confirming the Voyager
  carries the SGW too ([OBD Genie video](https://www.youtube.com/watch?v=wq_q_32UXhE),
  [AE Tools](https://knowledgebase.aetools.us/how-to-get-past-the-gateway-on-chrysler-vehicles-using-autel)).
- **AlfaOBD warns that no SGW bypass exists** for some newest vehicles (Jeep GC
  WL, 2025/26 Ram, Tonale). Worth re-checking if the intent ever expands beyond RU.

### 3.7 CAN FD

**No evidence that any RU minivan needs CAN FD.** Appcar's continued 2017–2026
two-bus/ELM-STN coverage and AlfaOBD's continued pin-based coverage both imply
classic CAN. The plan's own note stands: the STN1170 cannot reach a CAN-FD-only
module, and that would be a hard hardware wall, not a software one. Treat CAN FD
as a **profile-level flag to assert `false`** for RU, so that a future
non-classic vehicle fails loudly instead of mysteriously.

### 3.8 Tool landscape (why this is worth doing at all)

| Tool | Stellantis? | Notes |
|---|---|---|
| **FORScan** | **No** | "Vehicles other than Ford, Mazda, Lincoln, Mercury are not supported!" |
| **AlfaOBD** | Yes | Windows/Android; SGW bypass (hardware only) |
| **Appcar DiagFCA** | Yes | Windows; 500+ BCM parameters, routines |
| **JScan** | Yes | Mobile only, needs a BLE adapter |
| **wiTECH** | Dealer | Programming / proxy alignment |

An open, read-only, data-driven UDS scanner for these vans has real space:
AlfaOBD/Appcar are the only capable options and both are closed, Windows-first
and SGW-bypass-centric.

## 4. What must be data (not code)

The dividing line: **anything that varies with the vehicle is data; only the
protocol is code.**

| Concern | Data or code? | Why |
|---|---|---|
| ISO-TP framing, ELM/STN commands, `uds.parse_uds_frame` | **Code** | Bus-independent, vendor-neutral |
| Bus *definitions* (name → preset, pins, bitrate) | **Data** | RU needs 2; other platforms add CAN-CH or 29-bit |
| Address decoder (`request → response`) | **Code** (the *rule engine*) with **data rules** | Rules differ (`−0x280`, `+8`, `+0x6A`, 29-bit) |
| Module inventory, bus assignment, presence | **Data** | Varies by year, powertrain, trim |
| Module request/response IDs | **Data** | Almost stable; exceptions matter |
| DID map (IDs, names, encodings, scaling) | **Data** | The bulk of the value; proprietary |
| Standard DIDs `F186`–`F199`, `F190` | **Data**, but ship as a shared default set | Portable, reusable |
| DTC read/clear service numbers | **Data** (with sane defaults) | Some ECUs differ |
| Session/padding/`STmin`/timeout per module | **Data** | Vehicle- and module-specific |
| SGW present? which ops blocked? | **Data** | Year-gated (2018+) |
| PID scaling for mode `01`/`02` | **Code** (`python-OBD` already does it) | Standardized |
| Safety gates on writes | **Code** | Must not be data-overridable silently |

Concretely: the `0x700–0x7FF` scan range and the `10 03` probe (in
`uds.probe_ids` / `cli.py`) become profile fields, joining `module_buses` in
`profiles.py`; `BUS_PROTOCOL`, `DID_NAMES` and `INFO_DIDS` are already
centralised in `uds.py`. `elm.py` and the framing/parser code stay as they are.

## 5. Proposed `VehicleProfile` shape

A first cut of this design already exists in
[`src/pacifica_obd/profiles.py`](../src/pacifica_obd/profiles.py) — see
[§5.7](#57-relation-to-the-current-profilespy) for how the shape below maps onto
it, and which gaps the research surfaces.

Design goals: declarative, diffable, additive-only overrides, and **explicit
about what is known vs guessed**. Every fact carries `conf` and `src`.

### 5.1 Envelope and selection fields

```yaml
schema: pacifica-obd/vehicle-profile@1
profile_id: chrysler-pacifica-ru-2021-gas
title: "2021–2023 Chrysler Pacifica (RU) — 3.6L Pentastar V6, gas"
extends: null            # PHEV profile extends this one

match:                   # how a discovered vehicle is matched to this profile
  platform: RU
  make: Chrysler
  models: [Pacifica, Voyager, Grand Caravan]
  model_years: [2021, 2023]
  powertrains: [gas]
  vin_wmi: ["2C4"]       # Windsor, ON; conf: inferred — confirm per model
  require_any_module: [0x7E0]
  exclude_any_module: [0x7E2]   # HCP present => hybrid, reject this profile
  priority: 100          # higher wins among equally-matching profiles
```

### 5.2 Environment: buses, addressing, security, timing

```yaml
buses:
  can-c:
    label: "CAN-C (HS powertrain/chassis)"
    preset: 33           # STN STP; ISO 15765 11-bit 500k
    pins: [6, 14]
    bitrate: 500000
    framing: classic     # classic | canfd
  can-ihs:
    label: "CAN-IHS (MS body/comfort)"
    preset: 53
    pins: [3, 11]
    bitrate: 125000
    framing: classic

addressing:
  functional_broadcast: 0x7DF
  scan_range: [0x700, 0x7FF]
  response_rule:
    default: {op: sub, value: 0x280}
    exceptions:
      - {range: [0x7E0, 0x7E7], op: add, value: 0x8}
  state: [0x600, 0x7FF]  # region the tool may transmit into at all

session:
  default: 0x01
  extended: 0x03
  probe_request: "1003"          # probe used by `modules`
  identify_without_session: true # ping with plain 22 F190 first
  dids_without_session: [0xF190, 0xF187, 0xF188, 0xF189, 0xF191, 0xF132]
  padding_byte: null             # null = no explicit pad (ATCAF1 default)
  stmin_ms: null
  p2_timeout_ms: 40
  conf: inferred
  src: "repo-2021-baseline"

security:
  sgw:
    present: true
    since_model_year: 2018
    reads_pass: true
    blocked_services: [0x14, 0x27, 0x2E, 0x31, "10 02"]  # 10 02 = prog. session
    bypass: "Chrysler 12+8 (Autel-style)"
  write_path_enabled: false      # hard gate; not data-overridable at runtime

services:
  dtc_read:    {sid: 0x19, subfunction: 0x02}
  dtc_clear:   {sid: 0x14, enabled: false}
  read_did:    {sid: 0x22}
```

### 5.3 Module inventory

Presence and addressing are **separate facts with separate confidence**, because
for many modules we know a module exists (bus + name) but not its address.

```yaml
modules:
  PCM:
    name: PCM
    aliases: [ECM, engine]
    bus: can-c
    presence: required
    presence_conf: verified
    addr: {req: 0x7E0, resp: 0x7E8, addr_conf: verified, src: opendbc}
    did_sets: [iso14229-id, fca-id]
  TCM:
    name: TCM
    aliases: [transmission]
    bus: can-c
    presence: required
    presence_conf: verified
    addr: {req: 0x7E1, resp: 0x7E9, addr_conf: verified, src: opendbc}
  ABS:
    name: ABS
    aliases: [EBCM]
    bus: can-c
    presence: required
    presence_conf: verified
    addr: {req: 0x747, resp: 0x4C7, addr_conf: verified, src: opendbc}
  ORC:
    name: ORC
    aliases: [SRS]
    bus: can-c
    presence: required
    presence_conf: verified
    addr: {req: 0x744, resp: 0x4C4, addr_conf: verified, src: opendbc}
  IPC:
    name: IPC
    aliases: [combinationMeter, cluster]
    bus: can-c
    presence: required
    presence_conf: verified
    addr: {req: 0x742, resp: 0x4C2, addr_conf: verified, src: opendbc}
  EPS:
    name: EPS
    bus: can-c
    presence: required
    presence_conf: verified
    addr: {req: 0x75A, resp: 0x4DA, addr_conf: verified, src: opendbc}
  FFCM:
    name: FFCM
    aliases: [fwdRadar, ACC radar]
    bus: can-c
    presence: optional
    presence_conf: inferred
    addr: {req: 0x753, resp: 0x4D3, addr_conf: verified, src: opendbc}
  BCM:
    name: BCM
    bus: can-c
    presence: required
    presence_conf: verified
    addr: null              # unknown -> discover on vehicle
    note: "Appcar lists BCM on BOTH CAN-C and CAN-IHS; treat the second
           instance as a separate entry once its address is known."
  HVAC:
    name: HVAC
    bus: can-ihs
    presence: optional
    presence_conf: verified
    addr: null
  PSDML:
    name: PSDML
    aliases: [power sliding door left]
    bus: can-ihs
    presence: optional
    presence_conf: inferred
    addr: null
  # ... DDM, PDM, DMRL, DMRR, MSM, CSWM, PLGM, ICS, RADIO, TBM
  #     (can-ihs, addr null, presence inferred)
  # ... RFH, ESM, SCCM, OCM, PTS, SGW (can-c, addr null, presence inferred)
```

A module with `addr: null` is **not** an error — it is a discovery instruction.
The scanner finds its ID by probing the range; the result is written back to the
local override layer (§6), which is how the map becomes self-improving.

### 5.4 DID databases

```yaml
dids:
  inline:                          # small, portable, always available
    F186: {name: "Active diagnostic session", type: u8,  conf: standard}
    F187: {name: "Spare part number",         type: ascii, conf: verified}
    F188: {name: "ECU software number",       type: ascii, conf: verified}
    F189: {name: "ECU software version",      type: ascii, conf: verified}
    F18A: {name: "System supplier identifier",type: ascii, conf: standard}
    F18C: {name: "ECU serial number",         type: ascii, conf: standard}
    F190: {name: "VIN",                       type: ascii, len: 17, conf: verified}
    F191: {name: "ECU hardware number",       type: ascii, conf: verified}
    F194: {name: "Supplier ECU software number", type: ascii, conf: standard}
    F132: {name: "FCA identification/version", type: ascii, conf: inferred,
           src: opendbc}
  databases:                       # large, loadable, replaceable
    - fca-ru-identification.yaml   # keyed by ECU + software number
    - fca-ru-livedata.yaml         # proprietary DIDs + scaling (sparse!)
```

### 5.5 Example B — 2021 Pacifica Hybrid (delta over the gas profile)

```yaml
schema: pacifica-obd/vehicle-profile@1
profile_id: chrysler-pacifica-ru-2021-phev
title: "2021–2026 Chrysler Pacifica Hybrid (RU) — PHEV"
extends: chrysler-pacifica-ru-2021-gas   # inherit everything, then delta

match:
  powertrains: [phev]
  require_any_module: [0x7E2]            # HCP
  exclude_any_module: []                 # supersedes the gas profile's exclude
  priority: 110

vehicle:
  drive: [FWD]                           # PHEV is FWD-only
  notes: "16 kWh pack; no Stow 'n Go; PHEV discontinued Jan 2026."

modules_add:
  HCP:
    name: HCP
    aliases: [Hybrid Control Processor]
    bus: can-c
    presence: required
    presence_conf: verified
    addr: {req: 0x7E2, resp: 0x7EA, addr_conf: verified, src: opendbc}
  AHCP:
    name: AHCP
    bus: can-c
    presence: optional
    presence_conf: inferred
    addr: null
  BPCM:
    name: BPCM
    aliases: [Battery Pack Control Module, BECM]
    bus: can-c
    presence: required
    presence_conf: inferred
    addr: null
  OBCM:
    name: OBCM
    aliases: [On Board Charging Module]
    bus: can-c
    presence: required
    presence_conf: inferred
    addr: null
  IDCM:
    name: IDCM
    aliases: [Integrated Dual Charger Module]
    bus: can-c
    presence: optional
    presence_conf: inferred
    addr: null
  EAC:
    name: EAC
    aliases: [Electric A/C Compressor]
    bus: can-c
    presence: optional
    presence_conf: inferred
    addr: null
  APM:
    name: APM
    aliases: [Accessory Power Module]
    bus: can-c
    presence: optional
    presence_conf: inferred
    addr: null
  EBBM:
    name: EBBM
    aliases: [Electronic Brake Booster Module]
    bus: can-c
    presence: optional
    presence_conf: inferred
    addr: null

modules_remove: [TCM]               # PHEV e-CVT is coordinated by the HCP;
                                    # no TCM firmware observed at 0x7E1

modules_override:
  ABS:
    # on hybrids, ABS is queried at 0x7E4; 0x747 was not observed on PHEVs.
    # Keep both: "not on all hybrid platforms" (opendbc).
    addr: {req: 0x7E4, resp: 0x7EC, addr_conf: inferred, src: opendbc}
    addr_alternatives:
      - {req: 0x747, resp: 0x4C7, addr_conf: inferred, src: opendbc,
         note: "gas address; probe as fallback"}

unverified:
  - "Whether BPCM/OBCM/EAC/APM/EBBM are on CAN-C or a separate Hybrid LAN"
  - "Whether the HCP is the only gateway to the remaining hybrid controllers"
  - "Whether a standalone TCM exists on any PHEV year (none observed at 0x7E1)"
  - "Whether ABS is at 0x7E4 or 0x747 on a given PHEV (opendbc: 'not on all')"
  - "Whether 0x10 03 is accepted by PHEV controllers through the SGW"
```

Equivalent JSON is a direct encoding: `yaml.safe_load` ⇄ `json.loads`. Keep
YAML as the authoring format for comments and diffability, and load through a
single `load_profile()` that validates against a schema.

### 5.6 Resolved-profile output (what the tool should print)

```json
{
  "selected_profile": "chrysler-pacifica-ru-2021-gas",
  "match": {"vin": "<redacted>", "vin_hash": "<sha256>", "model_year": 2021,
            "powertrain": "gas"},
  "matched_by": ["vin.model_year", "positive_module:0x7E0", "negative_module:0x7E2"],
  "confidence": "high",
  "overrides_applied": ["profiles.d/2021-pacifica.local.yaml"],
  "modules_expected": 24,
  "modules_discovered": 21,
  "modules_missing": ["TBM2", "MSMP", "VRM"],
  "modules_unexpected": ["0x7D5"],
  "open_questions": ["SGW acceptance of 0x10 03 unverified on this VIN"]
}
```

`modules_missing` and `modules_unexpected` are the feedback loop: missing
required modules usually mean a trim/equipment difference, a wrong profile, or a
**bus fault** (see the star-connector note); unexpected responders usually mean a
module the map does not know yet.

### 5.7 Relation to the current `profiles.py`

A first cut of this design already exists in
[`src/pacifica_obd/profiles.py`](../src/pacifica_obd/profiles.py), and its
docstring points here for the full analysis. It defines a frozen
`VehicleProfile` dataclass (`id, make, model, years, powertrain, platform,
module_buses, info_dids, notes`), two instances (`RU_GAS`, `RU_PHEV`),
`year_from_vin()` and a best-effort `resolve()`, and `uds.py` now owns
`BUS_PROTOCOL`, `DID_NAMES` and `INFO_DIDS`. That is the right shape — the YAML
above is its evolution, not a replacement. Mapping:

| Proposed (this doc) | Today in `profiles.py` |
|---|---|
| `profile_id`, `models`, `model_years` | `id`, `model`, `years` (flat tuple) |
| `match.powertrains` | `powertrain` |
| `buses.*` | implicit in `uds.BUS_PROTOCOL` |
| `addressing.response_rule` | **absent** |
| `modules.*.addr.{req,resp}` | **absent** — `module_buses` is bus-only |
| `modules.*.presence` + `presence_conf` | **absent** — every listed module is assumed present |
| `dids.inline` / `databases` | `info_dids` (falls back to `uds.INFO_DIDS`) |
| `security.sgw` | free text in `notes` |
| `extends` / `modules_add` / `modules_override` | **absent** (no inheritance) |
| `conf` / `src` / `verified_by` | **absent** |

#### Gaps this research surfaces

These are concrete, evidence-backed divergences — worth fixing whether or not
the richer schema lands:

| Where | Issue | Evidence |
|---|---|---|
| `RU_GAS.years` / `RU_PHEV.years` | Stop at **2024**; the RU platform runs to **2027** (PHEV to 2026). `_VIN_YEAR_CODES` also omits **`V` = 2027**. | [Wikipedia](https://en.wikipedia.org/wiki/Chrysler_Pacifica_(minivan)), [Appcar (2017–2026)](https://appcar-diagfca.com/en/supported-vehicles/chrysler-pacifica) |
| `_COMMON_CAN_C` | Includes **`HCP`** and **`EAC`**, which are **PHEV-only** — so the *gas* profile currently claims them. | Appcar hybrid group; opendbc `hybrid` @ `0x7E2` |
| `_PHEV_CAN_C` | Missing **`IDCM`** and **`EBBM`**; no place to express that PHEV **ABS** is `0x7E4`, or that no **TCM** answers at `0x7E1`. | [Appcar](https://appcar-diagfca.com/en/supported-vehicles/chrysler-pacifica), [opendbc](https://github.com/commaai/opendbc/blob/master/opendbc/car/chrysler/fingerprints.py) |
| `resolve()` / `applies()` | With `powertrain=None` the guard short-circuits and **`RU_GAS` always wins** (it is first in `all_profiles()`), so an unknown-powertrain 2021 PHEV silently resolves to gas. Powertrain should be *discovered* (probe `0x7E2`), not assumed. | §6.1 step 4 |
| `model_buses` | Model names are Pacifica-only; the **Voyager** and **Grand Caravan** are the same RU vehicle with a thinner equipment mix. | [Wikipedia](https://en.wikipedia.org/wiki/Chrysler_Pacifica_(minivan)) |
| `applies()` / `platform` | `platform` is stored but never checked, and `model`/VIN WMI/VDS are not matched. | — |
| `notes` | SGW is prose only; the tool cannot tell a user *why* a clear was refused. | §3.6 |

An honest read of the current code against this doc: the **selection skeleton is
right**, the **addressing and evidence layers are the missing pieces**, and the
**year span is simply out of date**.

## 6. Profile selection, uncertainty, and overrides

### 6.1 Selection algorithm

```
1. Read VIN (mode 09 PID 02, falling back to UDS 22 F190 on PCM @7E0).
2. Decode: WMI(1-3) VDS(4-8) check(9) year-code(10) plant(11) serial(12-17).
   Year codes: 2017=H 2018=J 2019=K 2020=L 2021=M 2022=N
               2023=P 2024=R 2025=S 2026=T 2027=V   (I,O,Q,U,Z unused)
3. Filter candidate profiles: platform(WMI/VDS) ∩ model_year ∈ model_years.
4. Disambiguate powertrain by *evidence*, not by badge:
      - probe 0x7E2 (HCP) on CAN-C with a plain 22 F194
      - probe 0x7E0 (PCM) likewise
      - a responding 0x7E2 selects the PHEV profile
5. Resolve inheritance: apply `extends` chains; later deltas win;
   `modules_add` unions, `modules_override` patches, `exclude_any_module`
   eliminates non-matching candidates.
6. Scan both buses over `addressing.scan_range` with `session.probe_request`.
7. Reconcile discovery against the profile -> resolved profile (§5.6).
8. Persist profile_id, VIN-derived keys, discovery set and any overrides.
```

Step 4 is deliberately *discovery-based*: year + powertrain from VIN/badge is a
guess about **equipment**, and equipment is the thing that actually determines
which modules answer. VIN narrows the search; the bus settles it.

### 6.2 How uncertainty is recorded

Four confidence levels, on every fact, attached to every assertion:

| `conf` | Meaning | Trust |
|---|---|---|
| `standard` | ISO 14229 / ISO 15765 / OBD-II | High, year-independent |
| `verified` | Observed in a public vehicle-derived dataset **or** on our own van | Use directly |
| `inferred` | Derived from a vendor list / forum / manual; plausible, untested | Use, but flag |
| `unknown` | Placeholder; requires discovery (`addr: null`) | Probe, never assume |

Plus provenance: every fact may carry `src` (URL or dataset id) and, for
vehicle-observed facts, `verified_by` (VIN hash + date). Repo rules already say
not to commit VINs — so store a **hash**, or just `{date, note}`, never the VIN.

**Never silently widen a query because a fact is unknown.** An unknown address
means "probe", not "send to a broad range and hope".

### 6.3 Override mechanism

Layered, lowest priority first:

```
built-in profiles/*.yaml                     (shipped, read-only)
$PACIFICA_OBD_HOME/profiles.d/*.yaml         (user, merged in filename order)
CLI flags                                    (per-invocation, highest)
```

Deliberately narrow CLI overrides, each of which is **recorded**:

```sh
uv run pacifica-obd profile show                  # resolved profile + provenance
uv run pacifica-obd profile select --vin <VIN>    # or --year 2022 --powertrain phev
uv run pacifica-obd modules --bus both --profile chrysler-pacifica-ru-2017-gas
uv run pacifica-obd modules --bus can-c --assume-module 7D5=unknown-new-ecu
uv run pacifica-obd read-did --module 744 --did F132 --bus can-c
uv run pacifica-obd modules --bus both --record-addresses   # write-back discoveries
```

**Rules for overrides** (they are safety-relevant, not cosmetics):

1. Overrides may **add or narrow**; they may not silently *widen* a transmit range.
2. `security.write_path_enabled` and all write services are **not** overridable by
   profile data. Writes stay behind explicit user intent, per `AGENTS.md`.
3. Every override is appended to `events.jsonl` with its origin, and the session
   row records which profiles were in force.
4. A discovered address that contradicts a `verified` entry is recorded as a
   **conflict**, surfaced to the user, and *not* auto-applied.
5. `--record-addresses` writes to `profiles.d/`, never to the shipped profiles.

## 7. How to verify with a real car

Run this against each vehicle before claiming support. Record results in the
local store (never in the repo — VINs and captures stay out of git).

### Pre-flight

- [ ] `uv run pacifica-obd ports` finds the adapter; `raw ATI` / `raw STI`
      confirms `STN1170 v4.3.2` or better.
- [ ] Battery healthy: `raw ATRV` ≥ 12.0 V (12.4–12.6 V ideal). Low voltage
      causes flaky negative responses and false "no module" results.
- [ ] Ignition **ON**, engine **OFF** (steady state; not during a keep-alive cycle).
- [ ] Snapshot VIN + **record the model year, trim, and gas/PHEV** locally.
- [ ] Identify whether an **SGW** is fitted (2018+ should be). Determine whether a
      **12+8 bypass** is installed; note which connectors are in the adaptor path.
- [ ] Freeze the vehicle: no doors/hatch being cycled, HVAC off, no charging
      session on a PHEV (a charging PHEV will be busy and may NRC).

### Bus sanity

- [ ] `STP 33` → `STPRS` reports HS CAN 500K/11-bit (CAN-C).
- [ ] `STP 53` → `STPRS` reports MS CAN 125K/11-bit (CAN-IHS).
- [ ] Functional broadcast works: `ATSH 7DF` + `0100` returns at least the PCM's
      `41 00 …` supported-PID bitmask. This proves the physical layer before any
      UDS work.
- [ ] Restore the protocol snapshot afterwards (never `ATSP`; never persist EEPROM).

### Module discovery

- [ ] `modules --bus can-c --identify` and `modules --bus can-ihs --identify`.
- [ ] Confirm each **verified** address in §3.3 answers on CAN-C:
      `7E0→7E8`, `7E1→7E9`, `742→4C2`, `744→4C4`, `747→4C7`, `753→4D3`, `75A→4DA`.
      The `4xx` responses are the acid test of the `−0x280` rule.
- [ ] Confirm the **BCM's** request ID (currently `addr: null`) and record it.
- [ ] Confirm **which CAN-IHS modules** answer and record their IDs.
- [ ] Note responders **outside** the profile's expected list.

### Per-module UDS behaviour

For each responder, record:

- [ ] `10 03` (extended session) → `50 03`, or NRC. **This is the key unknown:
      does the SGW pass session control on the stock port?**
- [ ] `22 F190` in the **default** session → VIN (tests session-free reads).
- [ ] `22 F187`, `22 F188`, `22 F189`, `22 F191`, `22 F194`, and `22 F132`.
- [ ] `19 02` (report DTCs by status mask) → `59 02 …`; also try `19 01` and
      `19 0A` if `19 02` is refused.
- [ ] Record the **response ID** actually used, and the request→response delta.
- [ ] Classify every NRC seen:
      `11` service not supported · `12` subfunction not supported ·
      `13` incorrect length · `22` conditions not correct ·
      `31` request out of range · `33` security access denied ·
      `7E` subfunction not supported in active session ·
      `7F` service not supported in active session · `78` response pending.
- [ ] Note whether **padding** beyond the payload was required, and any
      multi-frame timing problems (missing/incomplete consecutive frames).

### PHEV-only

- [ ] Does `0x7E2` (HCP) answer? Does `0x7E4` (alternate ABS) answer?
- [ ] Which of BPCM / OBCM / IDCM / EAC / APM / EBBM / AHCP answer, and **on
      which bus**? This settles the "Hybrid LAN" question.
- [ ] Repeat with the charge cable **disconnected** (and, if safe, once charging)
      to see whether the HV system's state changes reachability.

### Ground truth

- [ ] Scan the same vehicle with **AlfaOBD** or **Appcar DiagFCA** on the same day
      and diff the module list against ours. Any module they see and we do not is
      a bug in our map, our addressing rule, or our probe.
- [ ] Save the resolved profile (§5.6) alongside the diff as evidence.

### Coverage sweep

- [ ] Run the above on as many of these as can be reached, in this order:
      **2021 gas (baseline) → 2021+ PHEV → 2017 gas (no SGW) → 2018–2020 →
      2025/2026 → 2027 refresh**.
- [ ] For each, explicitly record: `10 03` accepted? SGW blocks reads? module
      count on each bus? PHEV controllers found?

## 8. Recommendations

1. **Keep the transport and framing layers untouched.** `elm.py` plus the
   ISO-TP/frame-reassembly code now in `uds.py` (`parse_uds_frame`) are already
   vehicle-agnostic, already accept 11-bit *and* 8-hex (29-bit) headers, and
   already enforce correct classic-CAN framing.
2. **Finish extracting the remaining constants.** `BUS_PROTOCOL`, `DID_NAMES` and
   `INFO_DIDS` have already moved into `uds.py`; what is still hardcoded is the
   `0x700–0x7FF` scan range and the `10 03` probe in `probe_ids` / `cli.py`.
   Those belong on the profile next to `module_buses`.
3. **Make addressing rule-based, not arithmetic.** `−0x280` by default, `+8` for
   `7E0–7E7`, and a place for 29-bit later. This alone is what makes the existing
   scanner correct on these vans.
4. **Separate presence from addressing**, with independent confidence. Most of
   the module map is known-but-unaddressed.
5. **Build on the existing `RU_GAS` / `RU_PHEV` split** rather than replacing it,
   and add the PHEV delta (drop `TCM`, move `ABS` to `0x7E4`, add `IDCM`/`EBBM`,
   remove `HCP`/`EAC` from the gas list) as the first real `extends` case.
6. **Add `0xF132`** to `_INFO_DIDS` — it is a cheap, FCA-specific cross-check
   already observed on these ECUs (mark it non-standard in the DID map).
7. **Do not chase writes.** The SGW gates them from 2018; the 2017 path being
   open makes writes *more* dangerous, not more attractive.

## 9. Open questions

- **Does the SGW pass UDS `0x10 03` on the stock port?** If not, the `modules`
  probe strategy (extended session) may need a session-free fallback
  (`22 F194` / `22 F132`) — which opendbc's usage suggests is viable. This is the
  highest-impact unknown in the whole plan.
- **Is there really a third "Hybrid LAN" bus?** Appcar groups hybrid ECUs that
  way; no confirmed DLC-reachable third bus has been found. If it exists and is
  not on 3/11 or 6/14, the STN1170 alone may not reach it.
- **Exactly how does the PHEV address map differ?** Evidence says no TCM at
  `0x7E1` and ABS at `0x7E4` rather than `0x747` — but "not on all hybrid
  platforms" leaves room for year-to-year variation. Probe both, record which
  answered.
- **BCM, RFH, ESM, SCCM, OCM, PTS, and the whole CAN-IHS set** have no public
  request/response IDs. All are `addr: null` today. This is discovery work.
- **No public year-by-year DID matrix.** Year-specific DIDs, especially live data
  and adaptations, remain the gap that keeps this tool behind AlfaOBD.
- **VIN → platform mapping is assumed, not verified.** `2C4` (Windsor) is
  plausible for all three nameplates but has not been confirmed against a VIN
  decoder, and Voyager/Grand Caravan may use different WMI/VDS patterns.
- **Does the 2027 refresh change anything electrical?** Reported as cosmetic
  (V6 retained), but no diagnostic architecture information found.
- **Canada/US/Mexico market differences** in module content (e.g. MY2021 Grand
  Caravan shipped **Uconnect 4, 7-inch** rather than Uconnect 5) mean the TBM and
  infotainment modules will differ across nameplates of the same model year.
  Infotainment is a profile boundary, not a platform boundary.
- **What NRC/behaviour does a PHEV give while charging?** Untested; may make
  PHEV scans state-dependent.

## Sources

**Vehicle / platform**
- Chrysler Pacifica (minivan) — RU platform, Voyager & Grand Caravan badging, 2021 refresh (Uconnect 5), 2027 refresh: <https://en.wikipedia.org/wiki/Chrysler_Pacifica_(minivan)>
- Appcar DiagFCA — Chrysler Pacifica (RU) 2017–2026: full module list, CAN-C/CAN-IHS, 2018+ SGW, hybrid LAN grouping: <https://appcar-diagfca.com/en/supported-vehicles/chrysler-pacifica/>
- Appcar DiagFCA — How it works: SGW since MY2018, CAN-C pins 6/14, CAN-IHS pins 3/11, STN1170/2120/2230/2231 support both buses: <https://appcar-diagfca.com/en/how-it-works/>
- AlfaOBD — supported vehicles (`CHRYSLER PACIFICA (RU) 2016+`, OBDLink MX+/EX): <https://www.alfaobd.com/supported_cars.html>
- AlfaOBD — SGW from MY2018; STN11xx/21xx for HS 6/14 + MS 3/11: <https://www.alfaobd.com>
- AlfaOBD — OBD socket pin table (3/11 "CAN Middle speed", 6/14 "CAN High speed"): <https://www.alfaobd.com/androidhelp.html>
- JScan — FCA Security Gateway: introduced 2018, "some models in the 2018 model year and all models 2019 going forward"; bypass needed for writes only: <https://jscan.net/fca-security-gateway-module-basic-info-and-location/>
- FCA / Stellantis — Uconnect 5 press release: "The Atlantis architecture replaces Powernet as the next generation of FCA core in-vehicle electronics communication"; debut on the 2021 Pacifica: <https://www.prnewswire.com/news-releases/fcas-all-new-uconnect-5-global-platform-is-the-most-advanced-uconnect-system-ever-powerful-personalized-connected-and-easy-to-use-300993133.html>
- Stellantis Media — Uconnect 5 "What's New" (Atlantis vs Powernet wording): <https://media.fcanorthamerica.com/newsrelease.do?id=21511&mid=102>
- Mopar Insiders — Uconnect 5 / Atlantis architecture detail: <https://moparinsiders.com/podcast-we-dig-deeper-uconnect-5-infotainment-system>
- OBD Genie — SGW bypass location, "2018 and newer Chrysler Pacifica and 2020 and newer Chrysler Voyager": <https://www.youtube.com/watch?v=wq_q_32UXhE>
- AE Tools — SGW overview and per-vehicle locations: <https://knowledgebase.aetools.us/how-to-get-past-the-gateway-on-chrysler-vehicles-using-autel>

**Diagnostic addresses, DIDs and behaviour**
- comma.ai opendbc — Chrysler values: `CHRYSLER_RX_OFFSET = −0x280`, non-standard FCA identification DID `0xF132`, supplier software DID `0xF194`, alternate hybrid ABS `0x7E4`, ECU whitelists: <https://github.com/commaai/opendbc/blob/master/opendbc/car/chrysler/values.py>
- comma.ai opendbc — Chrysler fingerprints: per-model-year ECU request IDs (`742`, `744`, `747`, `753`, `75A`, `7E0`, `7E1`, `7E2`, `761`; 29-bit on Jeep Cherokee 5th gen): <https://github.com/commaai/opendbc/blob/master/opendbc/car/chrysler/fingerprints.py>
- NHTSA — factory vehicle scan report (2020 Pacifica PHEV), module/bus inventory: <https://static.nhtsa.gov/odi/inv/2022/INRD-PE22008-13852P1.pdf>
- NHTSA TSB — BPCM diagnostic/reprogramming (HCP ↔ BPCM lost-communication codes): <https://static.nhtsa.gov/odi/tsbs/2023/MC-10248015-9999.pdf>
- NHTSA TSB — transmission update requiring PCM + HCP + AHCP (PIM) + BPCM to be flashed together: <https://static.nhtsa.gov/odi/tsbs/2022/MC-10214604-9999.pdf>
- go-parts — `P2797` on 2017–2019 Pacifica PHEV: HCP / AHCP software: <https://www.go-parts.com/garage/obd-p2797-chrysler-pacifica-hybrid-2017-2019-phev>
- go-parts — `U0594` HCP data fault on 2017–2018 Pacifica PHEV (BPCM software): <https://www.go-parts.com/garage/obd-u0594-chrysler-pacifica-plug-in-hybrid-2017-2018>
- go-parts — `B1498` on 2021–2026 Pacifica: Uconnect 5's dedicated Telematics Box Module (TBM): <https://www.go-parts.com/garage/obd-b1498-chrysler-pacifica-2021-2026>
- go-parts — `U0001` HS CAN fault on 2017–2018 Pacifica: <https://www.go-parts.com/garage/obd-u0001-chrysler-pacifica-2017-2018>
- Pacifica Forums — CAN-C / CAN-IHS star-connector failure (module drop-out): <https://www.pacificaforums.com/threads/failed-can-bus-star-connector-2017-hybrid.53931>
- CSS Electronics — UDS tutorial (physical vs functional addressing, `+8` pairing convention): <https://www.csselectronics.com/pages/uds-protocol-tutorial-unified-diagnostic-services>

**Tool landscape**
- FORScan — "Vehicles other than Ford, Mazda, Lincoln, Mercury are not supported!": <https://forscan.org/home.html>

**In-repo references**
- [`src/pacifica_obd/profiles.py`](../src/pacifica_obd/profiles.py) — current `VehicleProfile` dataclass, `RU_GAS`/`RU_PHEV`, `year_from_vin()`, `resolve()`
- [`src/pacifica_obd/uds.py`](../src/pacifica_obd/uds.py) — `BUS_PROTOCOL`, `DID_NAMES`, `INFO_DIDS`, `parse_uds_frame`, `probe_ids`
- [`docs/enhanced-diagnostics.md`](enhanced-diagnostics.md) — module map, feasibility matrix, data sources, P0–P6
- [`docs/chrysler-sgw.md`](chrysler-sgw.md) — SGW behaviour, 12+8 bypass, tool comparison
- [`AGENTS.md`](../AGENTS.md) — adapter facts, protocol presets, safety rules
