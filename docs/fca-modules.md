# FCA module map — 2021 Chrysler Pacifica (RU)

How Chrysler/FCA **PowerNet** control modules are addressed and modelled for a
**2021 Chrysler Pacifica (RU)**, and a concrete plan to grow `pacifica-obd` from
generic OBD-II + UDS identification reads into read-then-guarded-write support
for body/chassis modules.

This is a **research document** compiled from the project's own sources: the
bundled Pacifica dataset (see [`pacifica-data.md`](pacifica-data.md)), bus
captures, and public vehicle-observed inventories. Every non-obvious claim is
tagged with a confidence level ([§0](#0-confidence-legend)). Nothing here claims
a write has ever been tested on a vehicle — see [§7.6](#76-risks) and
[§8](#8-verification-checklist-on-a-real-pacifica).

Related repo docs: [`enhanced-diagnostics.md`](enhanced-diagnostics.md) (P0–P6
roadmap), [`vehicle-support.md`](vehicle-support.md) (cross-year/hybrid matrix +
profile design), [`chrysler-sgw.md`](chrysler-sgw.md) (Security Gateway),
[`technical.md`](technical.md) (`+8` versus `−0x280`).

---

## 0. Confidence legend

Every non-obvious claim is tagged. This is carried through into the profile
schema in [§7.2](#72-profilespy-addresses-plus-a-rule-engine).

| Tag | Meaning |
|---|---|
| **[dataset]** | Shipped in `src/pacifica_obd/data/pacifica.json` and read through `pacifica_obd.pacifica_data`; see [`pacifica-data.md`](pacifica-data.md). |
| **[observed]** | Captured from the vehicle bus (or the adapter), and recorded here. |
| **[corroborated]** | **[dataset]**/*[observed]* **and** independently matched by a public vehicle-observed source (opendbc, NHTSA/published module inventories). |
| **[inferred]** | Follows logico-mechanically from an observation (a numeric column correlates with a known bus split, say) but is not stated anywhere. |
| **[unknown]** | Not established. Listed in [§9](#9-open-unknowns); must be settled on a car. |

---

## 1. What this map is, and where it lives

The project ships its own Pacifica dataset:

```
src/pacifica_obd/data/pacifica.json     # schema "pacifica-obd.dataset/1", platform RU
```

read through `importlib.resources` by
[`pacifica_obd.pacifica_data`](../src/pacifica_obd/pacifica_data.py), so it works
from a source checkout, an installed wheel or a zip, with no external database
and no network access:

```python
from pacifica_obd import pacifica_data

pacifica_data.modules()                       # list[dict] — addressed ECUs
pacifica_data.dids("ABS_PN")                  # list[dict] — pid/routine items
pacifica_data.adaptations("BODY_CHRYSLER")    # list[dict] — writable config
pacifica_data.dtc_text("0CD1")                # "Charge Port Door Control" | None
pacifica_data.dataset()                       # the whole parsed mapping
```

Sizes (full schema in [`pacifica-data.md`](pacifica-data.md)): **48 modules**
(24 CAN-C, 24 CAN-IHS; 42 gas, 6 PHEV); **960 per-module data items** (347
live-data `pid`, 613 `routine`) across 35 ECUs; **901 BCM adaptation entries**
(49 groups over 51 request opcodes, 880 with enumerated values); **4000 DTC
texts**. The dataset is a deterministic projection of the project's own
reference model — regenerable from the recipe in
[`pacifica-data.md`](pacifica-data.md) §"Regenerating the dataset".

Two properties of the dataset matter here:

- It **does** carry what a data-driven tool needs: module addresses, per-module
  data-item names, the writable BCM configuration space, and DTC text.
- It **does not** carry DID hex identifiers for the RU module set, byte→value
  formulas, any external database, or any seed→key algorithm — those gaps are
  explicit in [`pacifica-data.md`](pacifica-data.md) §"What is and isn't
  included".

The family tables in [§2.2](#22-the-consolidated-family-table) are the project's
collated **PowerNet (`*_PN`) family** map. The bundled dataset is the
RU-applicable subset of that family; the tables also list sibling PowerNet
modules that share the addressing scheme. Anything that comes from the shipped
dataset is tagged **[dataset]** and links back to
[`pacifica-data.md`](pacifica-data.md).

### 1.1 How a module is keyed

A module is identified by a **two-part key**, and the repo needs both parts:

1. **Family** — the `*_PN` ("PowerNet") family string (`ABS_PN`, `IPC_PN`,
   `SCCM_PN`, or `BODY_CHRYSLER` for the body controller). Joins the
   module/address, data-item and routine tables.
2. **Variant / device id** — the concrete variant within the family. **[dataset]**
   each `modules` row carries its upstream device ids in `device_ids`; the `dids`
   and `adaptations` entries are keyed by **ECU name**, not by variant, so
   variant-level selection is not part of the shipped data.

Applicability is **discovered on the bus, not declared**: the tool selects a
module *family* from what answers (ISO/part code → device) and then loads that
family's data items. The dataset encodes the applicability labels it was
projected from as `options`/`years` on each row; see
[`pacifica-data.md`](pacifica-data.md) §"How applicability was decided".

---

## 2. The Pacifica (RU) module map

### 2.1 Which families are the RU map

**[corroborated]** The `*_PN` ("PowerNet") family is FCA's chassis/body
diagnostic naming for the Pacifica-era vans, and it matches two independent
vehicle-observed lists already cited in [`vehicle-support.md`](vehicle-support.md):

- opendbc `chrysler/fingerprints.py` — IPC `0x742`, ORC `0x744`, ABS `0x747`,
  FFCM `0x753`, EPS `0x75A`, HCP `0x7E2`, ABS-alt `0x7E4`, PCM `0x7E0`,
  TCM `0x7E1`.
- Appcar DiagFCA's published Pacifica (RU) 2017–2026 module inventory and
  CAN-C / CAN-IHS grouping.

Every opendbc ID appears in the family map at the same address with the same
response ID. That is the corroboration. **[dataset]** The `years`/`options`
labels put the family in the MY2017–MY2022+ range, spanning the RU van.

**[inferred]** The MY2021 Atlantis architecture break is **not** visible as a
separate family name in the dataset — see [§9](#9-open-unknowns).

### 2.2 The consolidated family table

**[dataset]/[observed]** Grouped by bus. Request/response IDs are 11-bit hex.
The rule column is the relationship the dataset records between the two
(`response_rule`): `+8` on the OBD-II block, `−0x280` for most chassis/body
modules, `+1` for the Security Gateway; `explicit` marks a row carried without a
derivable rule. These tables are the project's **family reference**; the rows
actually shipped in `pacifica_data.modules()` are called out below.

**CAN-C (`inittype = 7`) — powertrain/chassis**

| ECU | Req | Resp | Rule | Notes |
|---|---|---|---|---|
| `PENTASTAR` (PCM) | `0x7E0` | `0x7E8` | `+8` | 3.6 L Pentastar V6 |
| `ZF8HP45` / `CHRYSLER_62TE` (TCM) | `0x7E1` | `0x7E9` | `+8` | 9-speed / 62TE |
| `BODY_CHRYSLER` (BCM) | `0x620` | `0x504` | `explicit` | body control; **[dataset]** keys the adaptations and dids |
| `RFH_PN` | `0x740` | `0x4C0` | `−0x280` | Radio Frequency Hub (keys) |
| `IPC_PN` | `0x742` | `0x4C2` | `−0x280` | cluster |
| `TIRE_CONTROL_PN` | `0x743` | `0x4C3` | `−0x280` | TPMS / WCM |
| `ORC_PN` | `0x744` | `0x4C4` | `−0x280` | airbag (SRS) |
| `ABS_PN` | `0x747` | `0x4C7` | `−0x280` | ABS/ESC |
| `ESL_PN` | `0x748` | `0x4C8` | `−0x280` | electronic steering lock |
| `ESM_PN` | `0x749` | `0x4C9` | `−0x280` | rotary e-shifter |
| `DTCM_PN` | `0x74B` | `0x4CB` | `−0x280` | drivetrain control (AWD) |
| `FDCM_PN` | `0x74C` | `0x4CC` | `−0x280` | final drive (AWD) |
| `ACC_PN` | `0x753` | `0x4D3` | `−0x280` | adaptive cruise |
| `ITBM_PN` | `0x754` | `0x4D4` | `−0x280` | trailer brake |
| `ADCM_PN` | `0x757` | `0x4D7` | `−0x280` | active damping |
| `EPS_PN` | `0x75A` | `0x4DA` | `−0x280` | electric power steering |
| `OCM_PN` | `0x75E` | `0x4DE` | `−0x280` | occupant classification |
| `EPPM_PN` | `0x75F` | `0x4DF` | `−0x280` | pedestrian protection (EU) |
| `PTS_PN` | `0x762` | `0x4E2` | `−0x280` | parktronics |
| `SCCM_PN` | `0x763` | `0x4E3` | `−0x280` | steering column control |
| `FFCMHALF_PN` | `0x764` | `0x4E4` | `−0x280` | forward-facing camera (radar) |
| `ASCM_PN` | `0x766` | `0x4E6` | `−0x280` | air suspension |
| `AFLS_PN` | `0x768` | `0x4E8` | `−0x280` | adaptive front lighting |
| `AHLM_PN` | `0x769` | `0x4E9` | `−0x280` | headlamp levelling |
| `VSIM_PN` | `0x78E` | `0x50E` | `−0x280` | vehicle system interface |
| `SGW_PN` | `0x47E` | `0x47F` | `+1` | **Security Gateway** (see [§6.3](#63-what-the-sgw-blocks)) |

**Hybrid/PHEV controllers on CAN-C (`inittype = 7`)**

**[dataset]** the PHEV-flagged rows in the dataset are `HCP_PN`, `BPCM_PN`,
`OBCM_PN`, `AHCP_PN`, `APM_PN` and `EAC_PN`; `EBCM_PN`/`ABSO_PN` are carried as
gas rows but are exercised by the hybrid brake system. All of these live on the
same CAN-C wire.

| ECU | Req | Resp | Rule | Notes |
|---|---|---|---|---|
| `HCP_PN` | `0x7E2` | `0x7EA` | `+8` | Hybrid Control Processor |
| `EBCM_PN` | `0x7E3` | `0x7EB` | `+8` | electronic brake booster |
| `ABSO_PN` | `0x7E4` | `0x7EC` | `+8` | ABS OBD / alternate hybrid ABS |
| `BPCM_PN` | `0x7E7` | `0x7EF` | `+8` | battery pack |
| `OBCM_PN` | `0x76E` | `0x4EE` | `−0x280` | on-board charger |
| `AHCP_PN` | `0x76C` | `0x4EC` | `−0x280` | auxiliary hybrid control |
| `APM_PN` | `0x76A` | `0x4EA` | `−0x280` | accessory power module |
| `EAC_PN` | `0x76B` | `0x4EB` | `−0x280` | electric A/C compressor |

**CAN-IHS (`inittype = 9`) — cabin/body/comfort**

| ECU | Req | Resp | ECU | Req | Resp |
|---|---|---|---|---|---|
| `HVAC_PN` | `0x783` | `0x503` | `BSM_PN` / `LBSS_PN` | `0x791` | `0x511` |
| `DDM_PN` | `0x784` | `0x504` | `HSM_PN` | `0x792` | `0x512` |
| `PDM_PN` | `0x785` | `0x505` | `CVPM_PN` | `0x794` | `0x514` |
| `DMRL_PN` | `0x786` | `0x506` | `MSMP_PN` | `0x795` | `0x515` |
| `DMRR_PN` | `0x787` | `0x507` | `HVACR_PN` | `0x798` | `0x518` |
| `PSDML_PN` | `0x788` | `0x508` | `RBSS_PN` | `0x799` | `0x519` |
| `PSDMR_PN` | `0x789` | `0x509` | `ANC_PN` | `0x7A5` | `0x525` |
| `MSM_PN` | `0x78A` | `0x50A` | `DCSD_PN` | `0x7AC` | `0x52C` |
| `PLGM_PN` | `0x78C` | `0x50C` | `VRM_PN` | `0x7AD` | `0x52D` |
| `FSM_PN` | `0x78D` | `0x50D` | `EOMR_PN` | `0x7AE` | `0x52E` |
| `ITM_PN` | `0x78F` | `0x50F` | `DTV_PN` | `0x7B4` | `0x534` |
| `EDM_PN` | `0x7B6` | `0x536` | `VES2_PN` | `0x7B7` | `0x537` |
| `HFM_PN` | `0x7B9` | `0x539` | `VES3_PN` | `0x7BB` | `0x53B` |
| `ICS_PN` | `0x7BC` | `0x53C` | `AMP_PN` | `0x7BE` | `0x53E` |
| `RADIO_PN` / `TGW_PN` | `0x7BF` | `0x53F` | | | |

**[dataset]** The bundled dataset ships a **subset** of the rows above: exactly
the 48 records returned by `pacifica_data.modules()` (24 CAN-C, 24 CAN-IHS). The
sibling rows kept here as family reference — `ZF8HP45`/`CHRYSLER_62TE` (TCM),
`TIRE_CONTROL_PN`, `DTCM_PN`, `FDCM_PN`, `ITBM_PN`, `ADCM_PN`, `EPPM_PN`,
`ASCM_PN`, `AFLS_PN`, `VSIM_PN`, `BSM_PN`, `ITM_PN`, `HFM_PN`, `DTV_PN`,
`VES2_PN`, `VES3_PN` and `TGW_PN` — are **not** queryable through the loader;
they come from other observed sources and share the addressing scheme.

> The BCM is the notable gap: the dataset carries `BODY_CHRYSLER` at
> `0x620`/`0x504` (rule `explicit`, **[dataset]**) but no `BCM_PN` row. The
> `0x620/0x504` pair does **not** follow `−0x280` (it is `−0x11C`), and the
> legacy family's `TIPM_CGW`/`FCM_CGW` variants also nominally use `620/504`.
> Which of these is the 2021 van's BCM is **[unknown]**; probe both `0x620` and
> the `F1xx`/`2E` config path on a car ([§8](#8-verification-checklist-on-a-real-pacifica)).

### 2.3 Querying the map

Everything the **dataset ships** is reachable as data — the 48 shipped records
above, plus their `dids` and `adaptations` — with no SQL and no external file:

```python
from pacifica_obd import pacifica_data

for m in pacifica_data.modules():
    # the 48 shipped ECUs, gas + PHEV, both buses
    print(m["bus"], m["ecu"], m["request"], m["response"], m["response_rule"])
```

---

## 3. Addressing

### 3.1 The `−0x280` rule is real and comes from FCA's own data

**[dataset]** The dataset records the request→response relationship per module
(`response_rule`), and the relationship groups cleanly:

| Request range | Response | Rule | Members |
|---|---|---|---|
| `0x7E0`–`0x7E7` | `0x7E8`–`0x7EF` | `+8` | PCM, TCM, HCP, EBCM, ABSO, BPCM |
| everything else `0x7xx` | `req − 0x280` | `−0x280` | the chassis/body ECUs (IPC `742`→`4C2`, ABS `747`→`4C7`, …, HVAC `783`→`503`) |
| `0x784` | `0x785` | `+1` | `ABS_CHRYSLER` (legacy) |
| `0x788` | `0x789` | `+1` | `ESM_NON_PN` |
| `0x640` / `0x650` / `0x6B0` | `0x508` / `0x50A` / `0x516` | mixed legacy | `*_NON_PN` |
| `0x47E` | `0x47F` | `+1` | `SGW_PN` |

This **confirms and supersedes** the opendbc-derived note in
[`vehicle-support.md`](vehicle-support.md) §3.3 (`CHRYSLER_RX_OFFSET = −0x280`):
the offset is not a quirk of one adapter — it is the addressing the modules
actually use. The `+8` set is exactly the legislated OBD-II block, and it is
worth noting that `HCP` (`0x7E2`) and the alternate ABS (`0x7E4`) sit **inside**
the `+8` block, not outside it.

The practical consequence is enforced at the call sites: callers must use the
response ID that discovery *captured*. `profiles.response_id()` is only a static
table plus a fallback derivation, so it is a convenience, not a guarantee — see
[`profiles.py`](../src/pacifica_obd/profiles.py).

### 3.2 The collisions you must not get wrong

**[observed]** Because the body IDs are low, **the same request ID means
different modules on different buses**:

- `0x784` on CAN-C (`inittype 7`) = `ABS_CHRYSLER` (legacy), response `0x785`.
- `0x784` on CAN-IHS (`inittype 9`) = `DDM_PN`, response `0x504`.

Same shape for `0x788` (`ESM_NON_PN` on CAN-C vs `PSDML_PN` on CAN-IHS), and
`0x791` legitimately shares `0x791`/`0x511` between `BSM_PN` and `LBSS_PN`.

**Design consequence:** an address is only meaningful as a triple
`(bus, request, response)`. Never store a bare request ID. This is already the
instinct in [`profiles.py`](../src/pacifica_obd/profiles.py) (module→bus), but the
address map must keep the bus as part of the key.

### 3.3 11-bit versus 29-bit

**[dataset]** The dataset stores **11-bit** IDs (`747`, `4C7`). A **29-bit** path
also exists for the same ECUs: `18DA<ecu>F1` / `18DAF1<ecu>`. The ECU byte is the
low byte of the 11-bit request ID (`0x747` → `18DA47F1`), and the tester address
is `F1` (or `F4` in one observed variant).

**[corroborated]** opendbc's Cherokee (KL) entries confirm FCA will use 29-bit on
the same family. So the address representation must stay width-agnostic (the repo
already accepts 3- or 8-hex headers in `uds.collect_frames`).

### 3.4 `inittype` 7 versus 9 correlates with CAN-C versus CAN-IHS

**[dataset]** The loader maps `inittype = 7 → can-c` and `inittype = 9 → can-ihs`,
and the split lines up with the two observed wire groupings: `inittype = 7`
carries powertrain/chassis modules (PCM/TCM/ABS/ORC/EPS/IPC/RFH/ESM/SCCM/ACC),
`inittype = 9` the body/comfort group (HVAC/doors/sliding doors/seats/ICS/AMP). It
is a strong correlation, not an exact response-range rule: CAN-C also includes the
BCM (`0x504`) and the OBD-II block (`0x7E8`–`0x7EF`), and the profile lists in
[`profiles.py`](../src/pacifica_obd/profiles.py) are *not* identical to the
dataset's membership (e.g. `_COMMON_CAN_IHS` lacks `RADIO`/`AMP` and adds `CSWM`).

Supporting **[dataset]** evidence: the `adapter` column is `0` for every
`inittype 7` row and `6` for every `inittype 9` row — the dataset encodes "which
physical adapter/channel this row needs".

**[unknown]** The exact *bit rate* implied by each `inittype` is not itself
stored: what the presets mean in practice (`STP 33` = HS 500 kbit/s 11-bit,
`STP 53` = MS 125 kbit/s 11-bit) must be confirmed with `STPRS` on the car
([§8](#8-verification-checklist-on-a-real-pacifica)). **Do not hardcode
"7 means 500k" from this document** — treat it as a hint and settle it by
probing.

---

## 4. DIDs, live data, adaptations and DTC text

### 4.1 Identification DIDs

**[unknown]/[observed]** The dataset deliberately ships **no DID hex
identifiers** for the RU module set ([`pacifica-data.md`](pacifica-data.md)
§"What is and isn't included"), so the identifiers below are protocol-level
**suggestions to confirm on a car**, not dataset facts. The standard
identification block is reusable across the family; the FCA-proprietary
identifiers are the ones worth probing:

| Read | Meaning |
|---|---|
| `22 F190` | VIN |
| `22 F187` | part number |
| `22 F188` | software version |
| `22 F189` | software version (alternate) |
| `22 F191` | hardware version |
| `22 F1A5` | ISO/part code |
| `22 F100` | CUSW architecture id |
| `22 F110` | **Atlantis** architecture id |
| `22 F154` | supplier id |
| `22 F194` / `22 F132` | supplier software / non-standard FCA id |

The identification reads worth capturing **first**, for the write path, are the
three architecture selectors: `22 F1A5` (ISO/part code), `22 F100` (CUSW) and
`22 F110` (**Atlantis** — see [§5.2](#52-what-selects-the-algorithm)), plus
`22 F154` (supplier id). How those bytes map to a module family or variant is
**not** shipped in the dataset and must be established on the vehicle
([§8](#8-verification-checklist-on-a-real-pacifica)).

**[inferred]** In the older KWP path the same selector is reached through the
`0x5A` ECU-identification response with option `0x87` (Fiat identification),
**not** UDS DID `F187` — the byte value collides, so keep the two paths straight.

### 4.2 Live-data recipes

**[dataset]** The dataset ships live-data *names* and units per module
(`pacifica_data.dids(ecu)` where `kind == "pid"`), but **no byte→value
formulas**. The decoding shape below is an **unverified working model** recorded
for the formula layer (P3); validate it against a capture before relying on it:

- A `ReadDataByIdentifier` reply is `62 <DID hi> <DID lo> …`, so offsets are
  measured from the payload's service byte.
- Locate the field: it starts at bit `bit_pos` and spans `bit_len` bits, so the
  bytes it touches are `floor(bit_pos/8)` … `ceil((bit_pos%8 + bit_len)/8) - 1`.
- Read that span big-endian, shift off the leading bits, mask to `bit_len` bits,
  then `value = raw*slope + offset` (with a signed wrap when the field declares
  one).
- Static/identification fields use MSB-first bit numbering; some dynamic fields
  use LSB-first. Enumerated fields map the extracted value to a label; string
  fields use BCD/ASCII/hex formatting.

**[inferred]** This is the decoder design the formula layer should implement, not
a description of shipped data. Filling the formulas for the RU set is a
data-collection task ([§7.5](#75-phased-plan), P3).

### 4.3 Adaptations / configuration

**[dataset]** `pacifica_data.adaptations("BODY_CHRYSLER")` is the Pacifica
body/vehicle configuration ("proxi") space: **901** bit-packed fields, each
`{request, bit, size, name, group, array, values}`.

```python
from pacifica_obd import pacifica_data

for field in pacifica_data.adaptations("BODY_CHRYSLER"):
    if field["group"] == "VehConfig 1":
        print(field)
```

```
request  bit  size  name                  group        values
0x122    26   6     Model Year            VehConfig 1  None
0x122    32   8     Vehicle Line          VehConfig 1  None
0x122    40   5     Country Code          VehConfig 1  None
0x122    45   2     Left/Right Hand Drive VehConfig 1  ['0: Not Assigned', '1: Left Hand Drive', '2: Right Hand Drive']
0x122    47   1     Remote Start Present  VehConfig 1  ['0: No', '1: Yes']
```

- `request` is the config DID/parameter group; `bit`/`size` locate the field;
  `name` is the human label; `values` are the enumerated allowed labels
  (`None` when numeric/free-form); `group` names a block.
- The **49 groups** include `VehConfig 1`…`VehConfig 8`, `ECUConfig 3/4/14/18`,
  `CBC Features`, `CBC Config I/O`, `Alarm - VTA Params`, `Cabin Net`,
  `2014 CBC Config I/O & Feat.`, `Seats A/B`, `Hybrid LAN Net` and more.

**[dataset]** The `VehConfig` block is reachable through specific config DIDs:

| Request | Group | | Request | Group |
|---|---|---|---|---|
| `0x122` | VehConfig 1 | | `0x127` | VehConfig 6 |
| `0x123` | VehConfig 2 | | `0x128` | VehConfig 8 |
| `0x124` | VehConfig 3 | | `0x145` | VehConfig 7 |
| `0x125` | VehConfig 4 | | `0xF010` | Cabin Net |
| `0x126` | VehConfig 5 | | `0xF011`–`0xF013` | P/T Chassis Net, Audio/Telemat Net, Hybrid LAN Net |

### 4.4 DTC text

**[dataset]** `pacifica_data.dtc_text(code)` returns a **short English
description** for 4000 codes:

```python
pacifica_data.dtc_text("0CD1")   # "Charge Port Door Control"
pacifica_data.dtc_text("50CC")   # ...
```

The same code may be reported by several modules (the dataset kept a code when
its device list intersects the RU set), so treat the text as module-agnostic and
keep the reporting module attached to the code. Descriptions are deliberately
short phrases, not procedure prose.

---

## 5. Security access (`0x27`) — the seed→key approach

### 5.1 The approach

**[inferred]** Protected writes need UDS security access (`27`). The working
model — an **unverified hypothesis** to confirm on a car — is that, for a given
ECU family, the key is a **derived function of the seed plus identity**
(`seed`, variant, supplier id, algorithm id at a security level) rather than a
fixed per-vehicle secret. If that holds, seed→key is reproducible offline for an
ECU whose function is known. In the observed model there is no VIN, calibration
blob or dongle among the inputs; confirm this before relying on it.

The project ships **no** manufacturer algorithms (see
[`seedkey.py`](../src/pacifica_obd/seedkey.py)): it keeps a pluggable provider
model (`key_for(seed, **ctx) -> int`) plus a short-secret recovery helper, all
behind the write path. `security` only requests a seed unless a provider is
configured, so by default nothing is unlocked.

### 5.2 What selects the algorithm

**[unknown]/[inferred]** The selector inputs are **hypothesised** to come from
the identification reads in [§4.1](#41-identification-dids); none of this is
validated by the shipped dataset. Capture and record them first, then test the
hypothesis:

| Input | Hypothesised source | Meaning |
|---|---|---|
| security level | `27` sub-function (odd/even) | seed sub-function |
| key length (bytes) | variant-dependent | number of key bytes |
| seed length / discriminator | variant-dependent | seed length selector |
| algorithm/variant selector | `22 F110` response (unverified offset) | candidate algorithm id |
| variant bytes | `22 F100` (CUSW) / `22 F110` | candidate variant |
| ECU / device identity | `22 F1A5` ISO/part code | candidate device identity |
| supplier id | `22 F154` response (unverified offset) | candidate supplier |

The naming that *would* emerge is a triple of **`(supplier, variant, algorithm
id)`** at a security level and revision — for example `ABS_PN`, supplier `00C6`
(Continental), variant `0x45`, algorithm `0x0226`, revision 01. `S1`/`S5` are
the two usual security levels; consecutive algorithm ids suggest one for
*sendKey* and a derived one for the follow-up. All **[inferred]**.

**Consequence (hypothesis):** the 2021 Pacifica is the debut vehicle of FCA's
**Atlantis** architecture (**[corroborated]** from public sources). *If* `22 F110`
carries the algorithm selector, it is the most important single DID to add for
the write path — but that link is exactly what [§5.4](#54-reproducibility-and-the-selector-table-gap)
and [§8](#8-verification-checklist-on-a-real-pacifica) must establish.

### 5.3 Supplier identifiers

**[observed]** FCA supplier identifiers appear in identification responses (and
on parts). The values seen on this family include:

```
0003 Bosch        0015 Hella        0022 Teves        0027 TRW
0030 Magneti Marelli  0032 Alpine   003C Conti Temic  0048 Valeo
0051 Automotive Lighting  0062 Eberspächer  0069 AISIN  0084 Harman/Becker
00C6 Continental  00C9 Mobis        00D2 Nexteer      00D6 Delta Electronics
00DD LG-CPI       00E5 Bitron       00F0 LG-E         00F4 Aptiv
```

So a selector reads as, e.g., "ABS/PowerNet, supplier **Continental**, variant
`0x45`, algorithm id `0x0226`, revision 01".

### 5.4 Reproducibility and the selector-table gap

**[inferred]** Seed→key is **offline-reproducible in principle for a set of FCA
ECUs, including the `_PN` ones this van uses**, with two conditions:

1. **The algorithm must be known and ported.** The project ships no manufacturer
   algorithms; it exposes a provider interface only. Any real algorithm has to
   be supplied by the operator and validated per ECU. Practical honesty: this is
   a **weeks-per-ECU testing problem**, not a one-evening script.
2. **The selector mapping must be established.** To *call* a routine you must
   supply the same inputs the ECU was matched on (`F110`/`F100`/`F154` plus any
   variant and seed/key lengths). The mapping from those observed bytes to a
   specific routine has **not** been established — see
   [§9](#9-open-unknowns). Until it is, seed→key is *reproducible in principle,
   unimplemented in practice*.

Some ECUs may additionally require read-only inputs read directly from the
module itself (e.g. a part number); the supplier/variant naming suggests the
*variant* is such an input, and it would come from the identification reads
above, not from a dump. **[inferred]**

---

## 6. The write / configuration path

### 6.1 Services used

**[observed]** The write vocabulary is ordinary UDS/KWP:

| Operation | Service |
|---|---|
| Read identification | `0x22 <DID>` (CAN), `0x21 <LID>` (KWP), `0x1A 87/97` (older) |
| Read a config block | `0x22 <did>` / `0x21` (config DID) |
| **Write a config field** | **`0x2E <DID> <data>`** |
| Actuator / output control | `0x2F <DID> 03 <value>` (`03` = short-term adjustment) |
| Routine | `0x31 01 <RID> …` |
| Write by local id (KWP) | `0x3B` |
| DTC clear | `0x14` |

So: **adaptations are `0x2E` writes keyed by the same DID space the reads use**
(`pacifica_data.adaptations()` requests), **actuators are `0x2F … 03`**, and
**routines are `0x31`**. There is no exotic FCA write service required for the
parameter path.

### 6.2 Proxy alignment

**[observed]** The proxi configuration blob is an application payload protected
by a **CRC-16** written as 5 ASCII digits into a fixed offset within the blob;
**CRC-16/CCITT** (Kermit variant, polynomial `0x8408`) reproduces it.
Proxy/vehicle configuration alignment is the specific procedure a bad write can
require in order to recover, and it is what the SGW discussion in
[`chrysler-sgw.md`](chrysler-sgw.md) refers to (`U2101`).

### 6.3 What the SGW blocks

**[dataset]** The dataset knows the gateway explicitly:
`SGW_PN`, `request 0x47E`, `response 0x47F`, rule `request+1`, option
`MY2018+ PowerNet`.

Two useful facts fall out of the MY2018+ label:

- The gateway's own name-plate model year matches the published introduction
  window (MY2018, all models MY2019+), **[corroborating]**
  [`vehicle-support.md`](vehicle-support.md) §3.6.
- The SGW is addressed **outside the `0x7xx` range** (`0x47E`/`0x47F`) — so a
  naïve "scan `0x700`–`0x7FF`" will never find it, and **[inferred]** a scanner
  that wants to *report* "SGW present" must probe `0x47E` separately.

**[dataset]/[observed]** The repo's existing rule stands: reads pass the gateway;
writes (`0x14`, `0x27`, `0x2E`, `0x31`, `10 02`) do not, on 2018+, and the only
un-gating path is a physical **Chrysler 12+8** bypass.

---

## 7. Mapping to `pacifica-obd`

The repo already has the right skeleton: `uds.py` owns bus/protocol/DID
constants, `profiles.py` owns `VehicleProfile`, `pacifica_data.py` owns the
bundled dataset, and `modules`/`read-did`/`module-info`/`capture` already do
read-only UDS. What is missing is the **rule engine**, the **formula layer**, and
a **guarded write path**.

### 7.1 Data layer

Two layers, both optional-safe:

- **Bundled dataset** — `pacifica_obd.pacifica_data` (always available; no I/O
  beyond the packaged resource). This is the project's own compact projection;
  see [`pacifica-data.md`](pacifica-data.md).
- **Optional external ECU database** — `pacifica_obd.database.EcuDatabase`, a
  strictly read-only view of a user-supplied SQLite database reached through
  `PACIFICA_OBD_ECU_DB` (no built-in default). It opens `mode=ro&immutable=1`,
  blocks writes with `PRAGMA query_only`, and degrades to a **no-op** when the
  path is unset or unreadable, so persistence/DB problems never break a command.

`database.py` today: opens read-only, queries the module/address table, and
exposes `pn_modules()` (the `*_PN` family) plus a raw `query()` escape hatch. It
returns rows as plain dicts; it does **not** construct or deduplicate keys. The
`(bus, request, response)` identity is therefore a **caller requirement** the
rule engine must enforce — never a bare request ID
([§3.2](#32-the-collisions-you-must-not-get-wrong)).

Do **not** commit an external ECU database or an export of one into this repo:
the bundled dataset is the project's own, and third-party data stays
bring-your-own. Extend `.gitignore` for any local export.

### 7.2 `profiles.py` — addresses plus a rule engine

Evolve the existing `VehicleProfile` (see the YAML in
[`vehicle-support.md`](vehicle-support.md) §5) with the two things the dataset
now fills in with concrete values:

```python
addressing = AddressingRule(default=Offset(-0x280), exceptions=[
    OffsetRange(0x7E0, 0x7E7, +0x8),      # OBD-II block incl. HCP 0x7E2
    Special(0x47E, 0x47F),                # SGW_PN
    Special(0x620, 0x504),                # BODY_CHRYSLER (BCM), explicit
    Special(0x784, 0x785),                # ABS_CHRYSLER (legacy, per-bus!)
])
```

```python
modules = {
  "can-c": {
    "PCM":  Addr(0x7E0, 0x7E8, conf="verified", src="dataset+opendbc"),
    "TCM":  Addr(0x7E1, 0x7E9, conf="verified"),
    "ABS":  Addr(0x747, 0x4C7, conf="verified"),
    "ORC":  Addr(0x744, 0x4C4, conf="verified"),
    "IPC":  Addr(0x742, 0x4C2, conf="verified"),
    "EPS":  Addr(0x75A, 0x4DA, conf="verified"),
    "RFH":  Addr(0x740, 0x4C0, conf="inferred"),
    "ESM":  Addr(0x749, 0x4C9, conf="inferred"),
    "SCCM": Addr(0x763, 0x4E3, conf="inferred"),
    "OCM":  Addr(0x75E, 0x4DE, conf="inferred"),
    "PTS":  Addr(0x762, 0x4E2, conf="inferred"),
    "FFCM": Addr(0x764, 0x4E4, conf="inferred"),
    "ACC":  Addr(0x753, 0x4D3, conf="inferred"),
    "SGW":  Addr(0x47E, 0x47F, conf="verified", note="outside 0x7xx scan range"),
    "BCM":  Addr(0x620, 0x504, conf="unknown", note="620/504 vs 784/504; confirm"),
  },
  "can-ihs": {
    "HVAC":  Addr(0x783, 0x503), "DDM":  Addr(0x784, 0x504),
    "PDM":   Addr(0x785, 0x505), "DMRL": Addr(0x786, 0x506),
    "DMRR":  Addr(0x787, 0x507), "PSDML":Addr(0x788, 0x508),
    "PSDMR": Addr(0x789, 0x509), "MSM":  Addr(0x78A, 0x50A),
    "PLGM":  Addr(0x78C, 0x50C), "FSM":  Addr(0x78D, 0x50D),
    "ITM":   Addr(0x78F, 0x50F), "BSM":  Addr(0x791, 0x511),
    "HSM":   Addr(0x792, 0x512), "CVPM": Addr(0x794, 0x514),
    "MSMP":  Addr(0x795, 0x515), "HVACR":Addr(0x798, 0x518),
    "RBSS":  Addr(0x799, 0x519), "ANC":  Addr(0x7A5, 0x525),
    "ICS":   Addr(0x7BC, 0x53C), "AMP":  Addr(0x7BE, 0x53E),
    "RADIO": Addr(0x7BF, 0x53F), "TGW":  Addr(0x7BF, 0x53F),
  },
}
# Dataset-defined PHEV delta: HCP 0x7E2/0x7EA, BPCM 0x7E7/0x7EF,
# OBCM 0x76E/0x4EE, AHCP 0x76C/0x4EC, APM 0x76A/0x4EA, EAC 0x76B/0x4EB.
# EBCM 0x7E3/0x7EB and ABSO 0x7E4/0x7EC are dataset-tagged gas but belong to
# the hybrid brake system; keep them available on both powertrains.
```

Also fix the module-list gaps [`vehicle-support.md`](vehicle-support.md) §5.7
already identified, now with concrete evidence:

- The current `_COMMON_CAN_C` in
  [`profiles.py`](../src/pacifica_obd/profiles.py) lists **`APM`**, but
  **[dataset]** `APM_PN` (`0x76A`/`0x4EA`) is flagged **PHEV** and is on the
  hybrid side — so `APM` should move to the PHEV delta. (Note: that section's
  claim that `_COMMON_CAN_C` contains `HCP`/`EAC` is **stale** — the current list
  does not; verify code against docs before "fixing".)
- The scan range must admit `0x47E` so the gateway is reported.

Change `uds.probe_ids`/`cli.cmd_modules` to take the range and the probe from the
profile, and to **report the response ID it actually saw**, so the `−0x280` rule
is validated in the field rather than assumed.

### 7.3 `seedkey.py` — write-path only, opt-in

- Target: port **specific, small subsets** of per-ECU functions, not a bulk
  corpus. Start with the modules a read-then-guarded-write roadmap would touch
  first (`IPC_PN`, the BCM, `RFH_PN`, `EPS_PN`), and only after establishing the
  selector mapping ([§5.4](#54-reproducibility-and-the-selector-table-gap)).
- Keep the pluggable provider model already in
  [`seedkey.py`](../src/pacifica_obd/seedkey.py): a user supplies
  `key_for(seed, **ctx)` or a short-secret `algorithm(seed, secret)`; the project
  ships no manufacturer algorithm.
- Must be **import-guarded**: nothing in the read path may import it, so the
  read-only tool has no security-access capability at all.

### 7.4 `config` / `write` — guarded, backup-first

Follow the shape the repo already uses for `dtc --clear` (explicit flag +
confirmation + `--force` required in `--json` mode), and raise the bar:

```
uv run pacifica-obd config read  --module BCM --bus can-ihs          # read-only, safe
uv run pacifica-obd config read  --module BCM --group "Cabin Net"    # decode via adaptations
uv run pacifica-obd config backup --module BCM --out bcm-backup.json # REQUIRED before any write
uv run pacifica-obd config write --module BCM --did 0122 --field "Country Code" --value 2 \
        --backup bcm-backup.json --i-understand-the-risk
uv run pacifica-obd routine run  --module ABS --routine "ABS Bleed Brakes" --i-understand-the-risk
```

Hard rules (these belong in `AGENTS.md` and in code, **not** in data — see
[`vehicle-support.md`](vehicle-support.md) §6.3):

1. No write without a fresh, complete read of the target block persisted first.
2. No write when the checklist in [§8](#8-verification-checklist-on-a-real-pacifica)
   has not passed on this VIN in this session.
3. `0x27` is attempted, never guessed: if the algorithm is unknown for the
   observed `(supplier, variant, algorithm id)`, **refuse** rather than
   brute-force.
4. Writes stay behind explicit user intent; `security.write_path_enabled` must
   **not** be overridable by profile data.
5. Every attempted and refused write is appended to `events.jsonl`.

### 7.5 Phased plan

| Phase | Deliverable | Depends on | Risk |
|---|---|---|---|
| **P0** | Dataset loader + optional external DB read-only; `tests` on the query layer | nothing | none |
| **P1** | Address rule engine + real module map in `profiles.py`; `modules` reports response IDs; probe `0x47E` for SGW | car | none (read) |
| **P2** | Config **read**: `config read` decodes the adaptation fields via `0x22 <DID>`; store snapshots | car | none (read) |
| **P3** | Live-data formula layer (byte/bit/slope/offset) → new `pids`/`monitor` source | car | none (read) |
| **P4** | Routine **run** for the safe subset (lamp tests, pump tests) via `0x2F`/`0x31`; still no parameters | P1, SGW plan | medium (actuation) |
| **P5** | Guarded `config write` (`0x2E`) behind backup + explicit intent | P2 + seed/key + bypass | **high** |
| **P6** | Proxi alignment — *only* if ever needed to recover from P5 | P5 | **very high** |

Everything up to **P4** needs no seed/key: **reads** (P0–P3) pass the gateway.
**P4 actuation does not** — the same gateway rule blocks `0x2F`/`0x31`, so P4
requires a verified SGW path and should be treated as actuation, not a read.
**P5 is the line**, and on 2018+ it needs the 12+8 bypass.

### 7.6 Risks

| Risk | Why | Mitigation |
|---|---|---|
| **Bricking a module** | A wrong `0x2E` DID/bit can leave a module unconfigured; a wrong proxi blob sets `U2101` across the bus | backup-first read of the whole block; refuse when the field's `bit`/`size` provenance is `unknown`; never write a field whose `name` is safety-relevant (brakes, restraint, steering) |
| **Restraints / brakes** | The routine catalogue includes `ABS Bleed Brakes`, `Brake Pedal Calibration`, `Sensor Cluster Calibration`, squib tests | keep actuation out of `config`; require an explicit `routine` subcommand with an interactive confirm |
| **SGW** | Blocks `0x14/0x27/0x2E/0x31` on 2018+; "reads pass" is the *only* reason P0–P3 are useful today | treat write capability as blocked-by-default; document the 12+8 hardware requirement; detect the gateway at `0x47E` |
| **Warranty / ToS** | Seed/key use and adaptation changes can void warranty and may violate terms | keep `seedkey.py` out of the default import graph; ship no algorithms; document intent in `--help` |
| **Data handling** | An external ECU database and any exported data are not the project's to redistribute | keep `database.py` bring-your-own (read-only); the bundled dataset is the project's own projection; do not commit external DBs or exports (extend `.gitignore`) |
| **Bus collision** | `0x784` etc. mean different modules per bus ([§3.2](#32-the-collisions-you-must-not-get-wrong)) | address = `(bus, req, resp)` everywhere; profile carries module→bus and the `modules` scan is per-bus |
| **Low battery** | Flaky NRCs / false "no module" while writing | check `ATRV` ≥ 12.4 V before any write; the repo already has `--check-voltage` |

---

## 8. Verification checklist on a real Pacifica

Run **in this order**. Do not proceed to a later block before the earlier one is
green; record everything locally (never commit VINs — see `.gitignore` and
[`vehicle-support.md`](vehicle-support.md) §6.2).

### Pre-flight

- [ ] `uv run pacifica-obd ports` finds the vLinker FS; `raw ATI` / `raw STI`
      shows an STN11xx/STN21xx (not a bare ELM327 v1.2).
- [ ] `raw ATRV` ≥ 12.4 V. Ignition **ON**, engine off.
- [ ] Record model year, trim, gas/PHEV locally. Note whether an SGW bypass is
      fitted.
- [ ] Vehicle quiescent: HVAC off, no door/hatch cycling, PHEV not charging.

### Addressing (the acid test)

- [ ] `STP 33` then `STPRS` → HS CAN 500k / 11-bit (CAN-C).
- [ ] `STP 53` then `STPRS` → MS CAN 125k / 11-bit (CAN-IHS).
- [ ] Functional broadcast sanity: `ATSH 7DF` + `0100` → `41 00 …`.
- [ ] `modules --bus can-c --identify` → **confirm the `−0x280` rule**:
      `7E0→7E8`, `7E1→7E9`, `742→4C2`, `744→4C4`, `747→4C7`, `75A→4DA`.
      The `4xx` responses are the whole point.
- [ ] `modules --bus can-ihs --identify` → the `0x5xx` half (`783→503`, `784→504`,
      `785→505`, `7BC→53C`, `7BF→53F`).
- [ ] Probe **SGW separately**: `raw 22F190 --header 47E` (or the ID-discovery
      variant) — does `0x47E` answer at `0x47F`? This validates the SGW row and
      the need for an out-of-range probe.
- [ ] Resolve the **BCM** unknown: does `0x620` answer `0x504`? Does `0x784` on
      CAN-C answer `0x785` (legacy ABS) or nothing?
- [ ] Record any responder outside both lists.

### Session behaviour per responder

- [ ] `10 03` → `50 03`, or an NRC. **This is the highest-value unknown:** does
      the SGW pass extended-session on the stock port?
- [ ] `22 F190` in the **default** session (tests session-free reads).
- [ ] `22 F1A5` → ISO/part code; record it. (The dataset ships **no**
      ISO-code → family mapping, so this is raw evidence, not a lookup.)
- [ ] **`22 F110`** → Atlantis identification. Capture the raw bytes; record any
      candidate algorithm-selector bytes for review. Whether `F110` *is* the
      seed/key selector is the hypothesis [§5.2](#52-what-selects-the-algorithm)
      asks you to test — not a fact.
- [ ] `22 F100` (CUSW), `22 F154` (supplier id) — capture, for comparison.
- [ ] `22 F187 F188 F189 F191 F194 F132` → part/software/serial block.
- [ ] `19 02` → DTCs; fall back to `19 01` / `19 0A`.
- [ ] Classify every NRC (`11/12/13/22/31/33/7E/7F/78`) and record the actual
      response ID seen per request.

### Config read path (P2)

- [ ] Pick the BCM and read **`0x0122`** (`VehConfig 1`). Decode `Model Year`,
      `Vehicle Line`, `Country Code` per the adaptation fields (`bit`/`size`).
      **Cross-check the decoded model year against the VIN.**
- [ ] Read `0x0123` (`VehConfig 2`) and `0xF010` (`Cabin Net`) and confirm the
      response length matches the highest `bit`+`size` in the field list.
- [ ] **Do not write anything.**

### Actuation check (only if the SGW path is verified)

- [ ] Only with a verified SGW path (e.g. the 12+8 bypass) and a genuinely
      safe, passive lamp/indicator test selected from the module's `routine`
      items: run it once with the vehicle stationary. This is **actuation**, not
      a read — `0x2F`/`0x31` are blocked on 2018+ without the bypass.

### Ground truth diff

- [ ] Scan the same vehicle with a manufacturer-aware tool (e.g. Appcar DiagFCA)
      on the same day; diff the module list and the `VehConfig 1` decoding. Any
      module it sees and we do not is a bug in our map, our rule, or our probe.

---

## 9. Open unknowns

1. **`inittype` → bus/baud semantics.** The 7/9 split aligns with
   CAN-C/CAN-IHS and the `adapter` column agrees, but the numeric bit-rate
   implied is not stored. **Do not hardcode it** — probe `STPRS` per bus instead
   ([§3.4](#34-inittype-7-versus-9-correlates-with-can-c-versus-can-ihs)).
2. **The seed/key selector mapping.** `seedkey.py` accepts a provider, but the
   mapping from `(selector, variant, supplier, algorithm id, seed length, key
   length)` to a key is **not established by this project**. Until it is,
   seed→key is *reproducible in principle, unimplemented in practice*
   ([§5.4](#54-reproducibility-and-the-selector-table-gap)).
3. **The 2021 Atlantis architecture.** The van's MY2021 change is documented
   (Uconnect 5, TBM, "Atlantis replaces Powernet"), and the `F110` Atlantis
   identification read is real — but the dataset still names the modules `*_PN`
   with no Atlantis-specific family, and no new addresses are visible. Whether
   any 2021+ RU module moved, was added (TBM), or changed DID space is
   **unverified**, and is the main reason P1 must be validated on the actual car
   before P2+ is built on top.

Further, narrower gaps still open: the exact BCM identity/address
([§2.2](#22-the-consolidated-family-table)), whether a third "Hybrid LAN" bus
exists ([`vehicle-support.md`](vehicle-support.md) §9), and whether `0x47E` is
the only gateway address.

---

## Appendix: sources and provenance

**In this repo**

- `src/pacifica_obd/data/pacifica.json` — the bundled dataset (module addresses,
  data items, adaptive config, DTC text); schema in
  [`pacifica-data.md`](pacifica-data.md).
- `src/pacifica_obd/pacifica_data.py` — the loader
  (`modules`/`dids`/`adaptations`/`dtc_text`).
- `src/pacifica_obd/database.py` — optional read-only external ECU database.
- `src/pacifica_obd/{uds,profiles,seedkey}.py` and `docs/` as cited above.

**Public vehicle-observed sources** (corroboration only)

- opendbc Chrysler fingerprints/values — ECU request IDs, the `−0x280` response
  offset, non-standard FCA identification DIDs.
- Appcar DiagFCA — published Pacifica (RU) 2017–2026 module inventory, CAN-C /
  CAN-IHS grouping, hybrid group, 2018+ SGW.
- NHTSA scan reports — module/bus assignments.

No external database, OEM data file or machine-specific path is stored in this
repository; captured vehicle data stays local and is never committed.
