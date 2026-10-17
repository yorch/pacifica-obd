# FCA modules from the AlfaOBD database

How AlfaOBD models Chrysler/FCA control modules, what that yields for a **2021
Chrysler Pacifica (RU)**, and a concrete plan to grow `pacifica-obd` from
generic OBD-II + UDS identification reads into read-then-guarded-write support
for body/chassis modules.

This is a **research document**. Everything below is either (a) a query you can
re-run against the reverse-engineered AlfaOBD database with the output pasted
from a real run, or (b) an inference whose confidence is **labelled**. Nothing
here claims a write has ever been tested on a vehicle — see
[§8.6](#86-risks) and [§9](#9-verification-checklist-on-a-real-pacifica).

Related repo docs: [`enhanced-diagnostics.md`](enhanced-diagnostics.md) (P0–P6
roadmap), [`vehicle-support.md`](vehicle-support.md) (cross-year/hybrid matrix +
profile design), [`chrysler-sgw.md`](chrysler-sgw.md) (Security Gateway).

---

## 0. Confidence legend

Every non-obvious claim is tagged. This is carried through into the profile
schema proposal in [§8.2](#82-profilespy-real-addresses-plus-a-rule-engine).

| Tag | Meaning |
|---|---|
| **[observed]** | Read directly out of the AlfaOBD database or its decompiled source; query/line included. |
| **[corroborated]** | **[observed]** *and* independently matched by an out-of-tree vehicle-observed source (opendbc, Appcar/NHTSA module lists). |
| **[inferred]** | Follows logico-mechanically from an observation (e.g. a numeric column correlates perfectly with a known bus split) but is not stated anywhere. |
| **[unknown]** | Not established. Listed in [§10](#10-open-unknowns); must be settled on a car. |

Source material (read-only, never modified) is a separate worktree:
`/Users/yorch/temp/alfa-obd`, a RE of **AlfaOBD v2.4.2** (Windows exe + Android
APK). All database paths below are relative to that tree.

```sh
# The decrypted DB used throughout this document.
DB=/Users/yorch/temp/alfa-obd/scratchpad/db/alfaobd_plain.db
# A fully layer-2-decoded copy (same schema, obfuscated text columns resolved).
DB2=/Users/yorch/temp/alfa-obd/scratchpad/db/alfaobd_decoded.db
```

Every query in this document is **read-only**. `sqlite3` opens the plain copy;
do not point anything writable at the original
`/Users/yorch/temp/alfa-obd/app-win/AlfaOBD_PC/alfaobd.db`.

---

## 1. What the AlfaOBD database actually is

**[observed]** `findings/04-database.md` fully documents this: the shipped
`alfaobd.db` is 68,224,000 bytes, encrypted with the legacy System.Data.SQLite
*CryptoAPI RC4* codec (page size 1024, same keystream per page). Password
`UX_abdRwRyz031gQ`, `RC4_key = SHA1(password)[0:16]`. A few text columns carry a
second 3DES-CBC layer (key `0987654321BFEBFBFF000206`, IV `16396381`), which is
what `alfaobd_decoded.db` resolves.

**[observed]** DB content version: `ver.version_name = 2.5.7.0`.

```sh
sqlite3 "$DB" "SELECT * FROM ver;"
```

Why this matters for us: **the vendor ships its entire FCA diagnostic model as
data.** Module comms parameters, identification DIDs, live-data PID definitions,
bit-packed adaptation parameters, DTC text and actuator/routine names are all
rows — not code. That is exactly the shape a data-driven `pacifica-obd` wants.

---

## 2. Schema: the tables that matter for modules

**[observed]** 35 tables, no indexes/views/triggers (all lookups are full scans).
Purpose per table is tabulated in `findings/04-database.md`; the ones that drive
module modelling are:

```sh
sqlite3 "$DB" ".tables"
```

```
BODY_PN_CONFIG              ECUUnits                   FGA_PETROL_DATA
CAN_DELPHI_500_CONFIG       FCM_CGW_CONFIG             Faults
CAN_DELPHI_RAM_CONFIG       FGA_ABS_DATA               Param_devices
CAN_MARELLI_CONFIG          FGA_ABS_SNAPSHOT           Param_names
CAN_MARELLI_CUSW_CONFIG     FGA_BCM_DATA               STATES
Devices_Diagnostics         FGA_DIESEL_DYNAMIC         TIPM_CGW_CONFIG
Devices_params_units        FGA_DIESEL_STATIC          TNAMES
Devid_mapping               FGA_ENGINE_DATA            Units
Diag_descriptions           FGA_IPC_DATA               isocodes
Diag_devices                FGA_IPC_ROUTINES           newTable
Diag_names                  FGA_IPC_SNAPSHOT           ver
ECUList                     FGA_IPC_SNAPSHOT_DATA
```

### 2.1 `ECUUnits` — the comms/address table (the important one)

**[observed]** One row per *selectable ECU variant*: the address(es), bus/init
type, and required adapter cable.

```sh
sqlite3 "$DB" "PRAGMA table_info(ECUUnits);"
```

```
ID | unit_name | ECUNAME | inittype | ecuaddress | canid | adapter | baudrate | canidresp
```

| Column | Meaning | Evidence |
|---|---|---|
| `unit_name` | human label shown in the ECU dropdown ("Instrument Panel Cluster") | **[observed]** |
| `ECUNAME` | the **family key** joined against `ECUList.ECUNAME` / `Diag_devices.Device` / config tables (`IPC_PN`) | **[observed]** |
| `inittype` | transport/init scheme selector (see [§4.4](#44-inittype-7-vs-9-correlates-with-can-c-vs-can-ihs)). Range 0–10. | **[observed]** values; semantics **[inferred]** |
| `ecuaddress` | K-line ECU address (0 where CAN addressing is used) | **[observed]** |
| `canid` | request CAN ID, **as hex text, no `0x`** (`747`) | **[observed]** |
| `canidresp` | response CAN ID (`4C7`) | **[observed]** |
| `adapter` | required physical adapter/cable selector (`0`, `6`, `0,5`…) | **[observed]** |
| `baudrate` | K-line baud (4800/10400/7813) | **[observed]** |

```sh
sqlite3 -header -column "$DB" \
  "SELECT ID,unit_name,ECUNAME,inittype,ecuaddress,canid,canidresp,adapter FROM ECUUnits LIMIT 5;"
```

```
ID  unit_name                     ECUNAME   inittype  ecuaddress  canid  adapter  baudrate  canidresp
--  ----------------------------  ---------  --------  ----------  -----  -------  --------  ---------
 1  Bosch Motronic M1.5.2/1.5.4   M152              0          16         5            4800
 2  Bosch Motronic M1.5.5         M155              2          16         0,5         10400
```

Distribution (**[observed]**): `inittype` ∈ {0,1,2,4,5,6,7,8,9,10};
`adapter` ∈ {0,1,2,3,4,5,6,"0,5",…}; 367 of 633 rows have a `canid`.

```sh
sqlite3 -header -column "$DB" "SELECT inittype, count(*) FROM ECUUnits GROUP BY inittype ORDER BY inittype;"
```

### 2.2 `ECUList` — the variant catalogue (and why it does **not** name RU)

**[observed]** 1478 rows. Columns: `ID, ECUNAME, ecutype, Device_ID, Option,
Alfa, Fiat_Fiat_Pro, Lancia, Abarth, Dodge_RAM, Chrysler, Jeep, Peugeot, Citroen,
Group_ID, Function_ID, LandRover, Jaguar`.

`Option` is a **model-year + architecture** label, and it is the single most
useful column in the DB for dating a module:

```sh
sqlite3 -header -line "$DB" "SELECT * FROM ECUList WHERE ID=643;"
```

```
      ID: 643
 ECUNAME: IPC_PN
Device_ID: 8160
  Option: MY2011-14 PowerNet
   Dodge_RAM: ,75,
   Chrysler: ,1,
Group_ID: 20
Function_ID: 7
```

The brand columns carry **comma-delimited model-code lists**. The model-code
table is recoverable from the Android build:

**[observed]** `/Users/yorch/temp/alfa-obd/analysis/jadx_out/sources/com/AlfaOBD/AlfaOBD/j8.java`
line 15 (`f4827c`) is the FCA vehicle-line table:

```
"1"  WK - Grand Cherokee     "15" KJ - Liberty      "16" CS - Pacifica
"17" JK - Wrangler           "18" KA - Nitro        "19" KK - Liberty
"20" RT - Grand Caravan      "45" RU - Pacifica      ...
```

and line 24 (`f4830f`) is the package/trim table:

```
"36" Voyager   "37" Pacifica   "38" Grand Caravan   "100"/"101"/"102" Wrangler
```

**Important negative finding [observed].** Every distinct non-empty `Chrysler`
value in `ECUList` comes from the small set
`{0,1,2,9,10,15,16,17,18,19,20,100,101,102}`:

```sh
sqlite3 "$DB" "SELECT DISTINCT Chrysler FROM ECUList WHERE Chrysler<>'' ORDER BY 1;"
sqlite3 "$DB" "SELECT count(*) FROM ECUList WHERE ','||replace(Chrysler,' ','')||',' LIKE '%,45,%';"
```

```
0
```

So **there is no `RU`/Pacifica-2017+ applicability flag in `ECUList`.** The brand
columns use the *legacy* model table, where `16 = CS - Pacifica` is the
**2004–2008 CS-platform Pacifica**, not our RU van. Consequences:

1. You cannot get a "2021 Pacifica module list" by filtering `ECUList` on model
   code. **[observed]**
2. AlfaOBD picks the module *family* at runtime from what answers on the bus
   (ISO-code → `Device_ID`, **[observed]** in `findings/05-protocol.md` §2.7),
   then loads that family's rows. Applicability is **discovered**, not declared.
3. Therefore the correct way to enumerate RU modules from this DB is by **family
   (`*_PN`) + corroboration against vehicle-observed lists**, not by model flag.
   See [§3](#3-the-pacifica-ru-module-map).

### 2.3 The other tables, in one screen

| Table | Rows | Key columns | Use |
|---|---|---|---|
| `ECUUnits` | 633 | `ECUNAME, inittype, canid, canidresp, adapter` | **addresses / bus** |
| `ECUList` | 1478 | `ECUNAME, Device_ID, Option, <brand model-lists>` | family + MY dating |
| `Param_devices` | 330 | `Device_ID, Device_name` | FGA device names |
| `Diag_devices` | 463 | `Diag_Device_ID, Device` | routines/actuators owner |
| `Devices_Diagnostics` | 9523 | `Diag_Device_ID, Diag_ID, Diag_Desc_ID` | which routine belongs to which ECU |
| `Diag_names` | 3891 | `Diag_Name_ID, Diag_Name_EN, …` (13 langs) | routine/activation names |
| `Diag_descriptions` | 1805 | `Diag_Desc_ID, Diag_Desc_EN, …` | procedure text/warnings |
| `Param_names` | 4757 | `Param_ID, Param_Name_EN, …` | live-data names |
| `Units` | 128 | `Unit_ID, Unit_EN, …` | scaling units |
| `STATES` | 3277 | `ID, EN1/EN2, DE1/DE2, …` | enum label pairs (On/Off) |
| `TNAMES` | 23497 | `ID, EN, DE, …` | generic term translation |
| `Faults` | 22166 | `hexcode, device_id, code_en, code_xx` | DTC text |
| `isocodes` / `newTable` | 3569 / 3120 | `iso_code, device_id, device_type` | identification → device |
| `FGA_*` | 4427…9223 | `request, bit_pos, bit_len, slope, offset, unit_name, device_id` | live/snapshot PIDs |
| `FGA_IPC_ROUTINES` | 232 | `request, request_name, _0.._14, device_id` | actuator/routine templates |
| `BODY_PN_CONFIG` | 1643 | `REQUEST, BIT, LENGTH, SETTING, _0.._15, <Device_ID cols>, config` | **adaptation/config** (PN body) |
| `TIPM_CGW_CONFIG` | 447 | same shape | TIPM/CGW config |
| `CAN_MARELLI_CUSW_CONFIG` | 667 | `byte, bit, length, setting, _0.._16` | CUSW-era config |
| `CAN_DELPHI_500_CONFIG` / `CAN_DELPHI_RAM_CONFIG` / `CAN_MARELLI_CONFIG` / `FCM_CGW_CONFIG` | 458/205/198/259 | byte/bit or REQUEST | legacy config families |

---

## 3. The Pacifica (RU) module map

### 3.1 How AlfaOBD keys a module

**[observed]** A module is identified by a **two-part key**, and it is worth being
explicit because the repo will need both:

1. **`ECUNAME`** — the family string (`ABS_PN`, `IPC_PN`, `BCM`, `SCCM_PN`). Joins
   `ECUUnits` → `ECUList` → `Diag_devices`.
2. **`Device_ID`** (a.k.a. `device_id`) — the *variant* (there are up to 9
   `IPC_PN` rows, one per MY/variant). Joins to the data tables: `FGA_*.device_id`
   (a colon-free comma list), `Devices_params_units.Device_ID`,
   `BODY_PN_CONFIG.<Device_ID>` column names, `isocodes.device_id`.

**[observed]** The bus identity flow (from `findings/05-protocol.md` §2.7,
verified against source): connect → send the identification request
(`22 F1A5` on 29-bit CAN; `22 F100` CUSW; `22 F110` Atlantis) → `62 F1 A5 <5B>` →
`SELECT … FROM isocodes WHERE ISOCODE LIKE '<first 8>%'` → `DeviceID` →
`SelectDevice()` loads the `FGA_*` / config tables for that device.

For the Pacifica the interesting identification reads are therefore
`22 F1A5` (ISO/part code), `22 F110` (**Atlantis** — see [§6.3](#63-what-selects-the-algorithm)),
`22 F100` (CUSW) and `22 F154` (supplier ID).

### 3.2 Which families are *the* RU map?

**[corroborated]** The `*_PN` ("PowerNet") family is FCA's chassis/body diagnostic
naming for the Pacifica-era vans, and it matches the two independent
vehicle-observed lists already cited in [`vehicle-support.md`](vehicle-support.md):

- opendbc `chrysler/fingerprints.py` — IPC `0x742`, ORC `0x744`, ABS `0x747`,
  FFCM `0x753`, EPS `0x75A`, HCP `0x7E2`, ABS-alt `0x7E4`, PCM `0x7E0`, TCM `0x7E1`.
- Appcar DiagFCA's published Pacifica (RU) 2017–2026 module inventory and
  CAN-C / CAN-IHS grouping.

Every opendbc ID appears in `ECUUnits` as a `*_PN` row, at the same address, with
the same response ID. That is the corroboration.

**[inferred]** AlfaOBD's `Option` labels put the family in the MY2017–MY2022+
range (`MY2017 PowerNet` … `MY2022 PowerNet`, `MY2018+ PowerNet`,
`MY2020+ PowerNet`), which spans the RU van. The MY2021 Atlantis architecture
break is **not** visible as a separate `ECUNAME` in this DB version — see
[§10](#10-open-unknowns).

### 3.3 The extracted table

67 `ECUUnits` rows carry a `*_PN` name, and all 67 have a request/response pair.
Grouped by `inittype` (**[inferred]**: 7 = CAN-C / high-speed, 9 = CAN-IHS /
medium-speed — see [§4.4](#44-inittype-7-vs-9-correlates-with-can-c-vs-can-ihs)):

**CAN-C (`inittype = 7`) — powertrain/chassis**

| ECU | Req | Resp | Rule | Notes |
|---|---|---|---|---|
| `PENTASTAR` (PCM) | `0x7E0` | `0x7E8` | `+8` | 3.6L Pentastar V6 |
| `ZF8HP45` / `CHRYSLER_62TE` (TCM) | `0x7E1` | `0x7E9` | `+8` | 9-speed / 62TE |
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
| `SGW_PN` | `0x47E` | `0x47F` | `−1` | **Security Gateway** (see §7.3) |

**PHEV-only on CAN-C (`inittype = 7`)**

| ECU | Req | Resp | Rule |
|---|---|---|---|
| `HCP_PN` (Hybrid Control Processor) | `0x7E2` | `0x7EA` | `+8` |
| `EBCM_PN` (electronic brake booster) | `0x7E3` | `0x7EB` | `+8` |
| `ABSO_PN` (ABS OBD / alt hybrid ABS) | `0x7E4` | `0x7EC` | `+8` |
| `BPCM_PN` (battery pack) | `0x7E7` | `0x7EF` | `+8` |
| `OBCM_PN` (on-board charger) | `0x76E` | `0x4EE` | `−0x280` |
| `AHCP_PN` (aux hybrid control) | `0x76C` | `0x4EC` | `−0x280` |
| `APM_PN` (accessory power module) | `0x76A` | `0x4EA` | `−0x280` |
| `EAC_PN` (electric A/C compressor) | `0x76B` | `0x4EB` | `−0x280` |

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

> The BCM is the notable gap: `ECUUnits` has `BODY_CHRYSLER 620/504` and
> `TIPM_CGW 620/504` and `FCM_CGW 620/504` (**[observed]**) but no
> `BCM_PN` row. `620/504` does **not** follow `−0x280` (it is `−0x11C`). Which of
> these three is the 2021 van's BCM is **[unknown]**; probe both `0x620` and the
> `F1xx`/`2E` config path on a car.

### 3.4 Runnable extraction query

```sh
DB=/Users/yorch/temp/alfa-obd/scratchpad/db/alfaobd_plain.db

# Pacifica-family (PowerNet) module map with the response rule made explicit.
# NB: use GLOB, not LIKE '%[_]PN' — in SQLite LIKE has no bracket classes,
#     so '[_]' would match the literal three characters.
sqlite3 -header -column "$DB" "
WITH u AS (
  SELECT unit_name, ECUNAME, inittype, canid, canidresp,
    (instr('0123456789ABCDEF',substr(canid,1,1))-1)*256 +
    (instr('0123456789ABCDEF',substr(canid,2,1))-1)*16 +
    (instr('0123456789ABCDEF',substr(canid,3,1))-1) AS reqd,
    (instr('0123456789ABCDEF',substr(canidresp,1,1))-1)*256 +
    (instr('0123456789ABCDEF',substr(canidresp,2,1))-1)*16 +
    (instr('0123456789ABCDEF',substr(canidresp,3,1))-1) AS respd
  FROM ECUUnits WHERE canidresp IS NOT NULL AND canidresp<>''
)
SELECT inittype AS bus, ECUNAME, unit_name,
       printf('0x%s',canid) AS req, printf('0x%s',canidresp) AS resp,
       CASE WHEN reqd-respd = 640 THEN '-0x280'
            WHEN reqd-respd = -8  THEN '+8'
            ELSE printf('%+d', reqd-respd) END AS rule
FROM u WHERE ECUNAME GLOB '*_PN'
ORDER BY inittype, canid;"
```

(`CAST('0x747' AS INTEGER)` returns **0** in SQLite — hence the explicit
`instr('0123456789ABCDEF', …)` nibble decode above. Trying to cast silently
yields a table of zeroes, which is an easy way to "confirm" anything.)

---

## 4. Addressing

### 4.1 The `−0x280` rule is real and comes from FCA's own data

**[observed]** Across all 367 addressed `ECUUnits` rows, exactly **58** satisfy
`request − response = 0x280`, and they are the chassis/body diagnostic IDs:

```sh
sqlite3 -header -column "$DB" "
WITH u AS (
  SELECT canid, canidresp,
    (instr('0123456789ABCDEF',substr(canid,1,1))-1)*256 +
    (instr('0123456789ABCDEF',substr(canid,2,1))-1)*16 +
    (instr('0123456789ABCDEF',substr(canid,3,1))-1) AS reqd,
    (instr('0123456789ABCDEF',substr(canidresp,1,1))-1)*256 +
    (instr('0123456789ABCDEF',substr(canidresp,2,1))-1)*16 +
    (instr('0123456789ABCDEF',substr(canidresp,3,1))-1) AS respd
  FROM ECUUnits WHERE canidresp IS NOT NULL AND canidresp<>''
)
SELECT printf('0x%X', reqd-respd) AS delta, count(*) AS n
FROM u GROUP BY delta ORDER BY n DESC LIMIT 4;"
```

```
delta  n
-----  --
0x280  58      <-- the PowerNet/chassis rule
0xFFFFFFFFFFFFFFF8  29   <-- i.e. -8 -> response = request + 8 (OBD-II block)
0xFFFFFFFFFFFFFFFF  13   <-- i.e. -1 -> response = request + 1 (legacy pair)
0x11C   3
```

This **confirms and supersedes** the opendbc-derived note in
[`vehicle-support.md`](vehicle-support.md) §3.3 (`CHRYSLER_RX_OFFSET = −0x280`):
the offset is not a quirk of one adapter, it is the value in FCA's own diagnostic
database. The `+8` set is exactly the legislated OBD-II block, and it is worth
noting that `HCP` (`0x7E2`) and the alternate ABS (`0x7E4`) sit **inside** the
`+8` block, not outside it:

| Request range | Response | Rule | Members in DB |
|---|---|---|---|
| `0x7E0`–`0x7E7` | `0x7E8`–`0x7EF` | `+8` | PCM, TCM, HCP, EBCM, ABSO, BPCM |
| everything else `0x7xx` | `req − 0x280` | `−0x280` | 58 chassis/body ECUs |
| `0x784` | `0x785` | `+1` | `ABS_CHRYSLER` (legacy) |
| `0x788` | `0x789` | `+1` | `ESM_NON_PN` |
| `0x640`/`0x650`/`0x6B0` | `0x508`/`0x50A`/`0x516` | mixed legacy | `*_NON_PN` |
| `0x47E` | `0x47F` | `+1` | `SGW_PN` |

### 4.2 The collisions you must not get wrong

**[observed]** Because the body IDs are low, **the same request ID means different
modules on different buses**:

- `0x784` on CAN-C (`inittype 7`) = `ABS_CHRYSLER`, response `0x785`.
- `0x784` on CAN-IHS (`inittype 9`) = `DDM_PN`, response `0x504`.

Same for `0x788` (`ESM_NON_PN` vs `PSDML_PN`) and `0x791`
(no collision, but `BSM_PN` and `LBSS_PN` legitimately share `0x791/0x511`).

**Design consequence:** an address is only meaningful as a triple
`(bus, request, response)`. Never store a bare request ID. This is already the
instinct in [`profiles.py`](../src/pacifica_obd/profiles.py) (module→bus), but the
address map must keep the bus as part of the key.

### 4.3 11-bit vs 29-bit

**[observed]** The DB stores **11-bit** IDs (`747`, `4C7`). AlfaOBD *also*
implements a 29-bit path for the same ECUs: `CCAN`/`BCAN_7209` use
`18DA<ecu>F1` / `18DAF1<ecu>` (`findings/05-protocol.md` §3.5). The ECU byte is
the low byte of the DB request ID (`0x747` → `18DA47F1`), and the tester address
is `F1` (or `F4` for a hard-coded DeviceID list).

**[corroborated]** opendbc's Cherokee (KL) entries confirm FCA will use 29-bit on
the same vendor code. So the repo's address representation must stay
width-agnostic (it already accepts 3- or 8-hex headers in
`uds.collect_frames`).

### 4.4 `inittype` 7 vs 9 correlates with CAN-C vs CAN-IHS

**[inferred, high confidence]** `inittype = 7` rows have `0x4xx` responses and
cover PCM/TCM/ABS/ORC/EPS/IPC/RFH/ESM/SCCM/ACC — i.e. exactly the repo's
`_COMMON_CAN_C`. `inittype = 9` rows have `0x5xx` responses and cover
HVAC/doors/PSDML/PSDMR/MSM/PLGM/ICS/RADIO/AMP — i.e. exactly the repo's
`_COMMON_CAN_IHS`. The split is perfect (67/67 rows) and matches two independent
vehicle-observed sources.

Supporting **[observed]** evidence: the `adapter` column is `0` for every
`inittype 7` `*_PN` row and `6` for every `inittype 9` one — the DB encodes
"which physical adapter/channel this row needs", and AlfaOBD's own help pages
name pins 6/14 "CAN high speed" and 3/11 "CAN middle speed".

**[unknown]** The numeric `inittype → InitType enum` mapping is not recovered:
`SelectInitType` (source `af.renamed.cs:99014`) is a 1700-line switch on
`eEcutype` that sets a `.d` field, and the dotfuscated enum ordinals have not been
lined up with the numeric column. **Do not hardcode "7 means 500k" from this
document** — treat it as a hint, and settle it by probing (see §9).

---

## 5. DIDs, live data, adaptations and DTC text

### 5.1 Live data — `FGA_*` tables

**[observed]** Every FGA row is a decoding recipe keyed by a request:

```sh
sqlite3 -header -column "$DB" "PRAGMA table_info(FGA_ENGINE_DATA);"
sqlite3 -header -column "$DB" "SELECT * FROM FGA_ENGINE_DATA LIMIT 4;"
```

```
request  group_name   bit_pos  response_name  bit_len  lower_level  upper_level  slope  offset  unit_name  table_value  table_name  hex  param  dynamic  device_id
220123   (6527) 1     36       RC01  (22046)  1                                                            0            (26,1)                           60585,
220123   (6527) 1     36       RC01  (22046)  1                                                            1            (26,0)                           60585,
```

- `request` = `"22" + DID` (UDS `0x22` ReadDataByIdentifier). KWP-on-CAN/K-line
  families use `0x21` (readDataByLocalIdentifier) instead — same table shape.
- `bit_pos` counts **bits from the start of the positive response, SID included**
  (`22 01 23 …`, `bit_pos 36` → byte 4).
- `dynamic` non-empty ⇒ live/PID row (`FGA_DIESEL_DYNAMIC` holds
  `{bit_pos,bit_len,low,high,slope,offset}`); empty ⇒ static/identification row.
- `device_id` is a **comma list** of variant Device_IDs the row applies to.
- `response_name` / `table_name` are `(n)` = `TNAMES` ordinal, or `(row,col)` =
  `STATES` ordinal. `unit_name` goes through the `Units` table.

**[observed]** The decode formula (source `ProcessFGA_DIESEL`, verified in
`findings/05-protocol.md` §5.3):

```
byte  = bit_pos / 8
nbytes = max(1, bit_len / 8)
raw   = big-endian(KLLMessage[byte .. byte+nbytes-1])
raw  &= (1 << bit_len) - 1
value = raw*slope + offset            # signed wrap via lower/upper_level
```

Static rows use MSB-first bit numbering instead:
`shift = 8*nbytes − bit_pos%8 − bit_len`. Enum rows emit `table_name` when the
extracted field equals `table_value`; `hex` selects `B`=BCD, `A`=ASCII, `F`=hex.

**[observed]** Families: `FGA_ENGINE_DATA` (4427), `FGA_PETROL_DATA` (5099),
`FGA_DIESEL_DYNAMIC` (501), `FGA_DIESEL_STATIC` (4095), `FGA_ABS_DATA` (4089),
`FGA_ABS_SNAPSHOT` (678), `FGA_BCM_DATA` (4826), `FGA_IPC_DATA` (1389),
`FGA_IPC_SNAPSHOT` (9223), `FGA_IPC_SNAPSHOT_DATA` (66). This is the single most
valuable table group for a "live manufacturer data" feature.

### 5.2 Adaptations / configuration — the config tables

**[observed]** `BODY_PN_CONFIG` is the PN-era body/vehicle configuration
("proxi") database: 1643 rows, bit-packed fields over a request/DID.

```sh
sqlite3 -header -column "$DB" \
 "SELECT REQUEST,BIT,LENGTH,SETTING,_0,_1,config FROM BODY_PN_CONFIG WHERE REQUEST='0122' LIMIT 6;"
```

```
REQUEST  BIT  LENGTH  SETTING                              _0                 _1                  config
0122     26   6       Model Year
0122     32   8       Vehicle Line
0122     40   5       Country Code
0122     45   2       Left/Right Hand Drive                0: Not Assigned   1: Left Hand Drive  VehConfig 1
0122     47   1       Remote Start Present                 0: No             1: Yes              VehConfig 1
```

- `REQUEST` = the DID/parameter group (`0122`…`0150`, `A003`…`A0B9`, `F010`…`F013`).
  `config` groups them into named blocks: 53 distinct values, e.g. `VehConfig 1`
  … `VehConfig 8`, `ECUConfig 3/4/14/18`, `CBC Features`, `CBC Config I/O`,
  `Alarm - VTA Params`, `Cabin Net`, `2014 CBC Config I/O & Feat.`
- `BIT`/`LENGTH` locate the field; `_0.._15` are per-value labels;
  `SETTING` is the human name.
- The trailing columns named after `Device_ID`s (`_7720`, `_8157`, `_18209`…)
  are per-variant option-label overrides.

```sh
# Which config block is reachable through which DID (VehConfig example):
sqlite3 -header -column "$DB" \
 "SELECT REQUEST, config FROM BODY_PN_CONFIG WHERE config LIKE 'VehConfig%' GROUP BY REQUEST;"
```

```
0122 VehConfig 1   0123 VehConfig 2   0124 VehConfig 3   0125 VehConfig 4
0126 VehConfig 5   0127 VehConfig 6   0145 VehConfig 7   0128 VehConfig 8   014D VehConfig 8
```

**[observed]** The legacy families use the same idea with different keys:
`TIPM_CGW_CONFIG` (`REQUEST 3B0A`, "Vehicle Config 1: …"),
`CAN_MARELLI_CUSW_CONFIG` (`byte`/`bit` — a whole-frame layout, not DID-keyed),
`CAN_DELPHI_*`, `FCM_CGW_CONFIG`.

### 5.3 Routines and actuators — `Diag_*` + `FGA_IPC_ROUTINES`

**[observed]** `FGA_IPC_ROUTINES` rows are **literal request templates**:

```sh
sqlite3 -header -column "$DB" \
 "SELECT id,request,request_name,bit_pos,bit_len,_0,_1,device_id FROM FGA_IPC_ROUTINES LIMIT 4;"
sqlite3 "$DB" "SELECT substr(request,1,2) AS sid, count(*) FROM FGA_IPC_ROUTINES GROUP BY sid;"
```

```
id  request   request_name       desc      bit_pos  bit_len  _0         _1          device_id
1   2F550003  TPMS               (21614)   32       8        0: (3,1)   255: (3,0)  33018,51034,55593,
2   2F552803  (14818) - (2688,1) (21614)   32       8        0: (3,1)   255: (3,0)  54337,

sid  n
2E   23
2F   207
31   2
```

So the routine/actuator vocabulary in the DB is:

| SID | UDS service | Rows | Shape |
|---|---|---|---|
| `0x2E` | WriteDataByIdentifier | 23 | `2E <DID> <data…>` |
| `0x2F` | InputOutputControlByIdentifier | 207 | `2F <DID> 03 <value>` (`03` = shortTermAdjustment) |
| `0x31` | RoutineControl | 2 | `31 01 <RID …>` |

**[observed]** The **named, per-ECU actuator catalogue** is the `Diag_*` trio.
`Diag_devices` maps a diagnostic device ID to an `ECUNAME`
(`241 = ABS_PN`, `286 = IPC_PN`, `288 = TIRE_CONTROL_PN`, `308 = ORC_PN`,
`301 = RFH_PN`, `310 = HVAC_PN`, `311 = ICS_PN`, `312 = DDM_PN`, …):

```sh
sqlite3 -header -column "$DB" "
SELECT dd.Diag_ID, dn.Diag_Name_EN, dsc.Diag_Desc_EN
FROM Devices_Diagnostics dd
JOIN Diag_names dn  ON dn.Diag_Name_ID  = dd.Diag_ID
LEFT JOIN Diag_descriptions dsc ON dsc.Diag_Desc_ID = dd.Diag_Desc_ID
WHERE dd.Diag_Device_ID = 241 LIMIT 6;"
```

```
Diag_ID  Diag_Name_EN                       Diag_Desc_EN
1942     Low Pressure Feed Valve #2 …       You should hear the valve ticking for a few seconds.
1577     Recirculation Pump                 The pump motor will operate for several seconds.
17       ABS warning lamp                   The lamp must flash for a few seconds.
1549     ABS Bleed Brakes                   This procedure should be performed only after …
1947     ABS Initialization                 The routine is to be used to initialize the ABS ECU after …
3122     Reset VIN Values                   The procedure resets the VIN-related data in the control module
```

**[observed]** That is a genuine maintenance-procedure catalogue with the
vendor's own safety warnings — useful both as a feature and as a *hazard list*
for the guarded write path ([§8.6](#86-risks)).

### 5.4 DTC text

**[observed]** `Faults` maps `hexcode` + a colon-list of device IDs to text:

```sh
sqlite3 -header -column "$DB2" "SELECT hexcode,device_id,code_en FROM Faults LIMIT 3;"
sqlite3 -header -column "$DB2" "SELECT hexcode,device_id,code_en FROM Faults WHERE device_id LIKE '%8380%' LIMIT 3;"
```

```
hexcode  device_id                                     code_en
0000     :5304:6747:5303:6700:8548:10400:              Camshaft Position - Camshaft Position Correlation
50CC     :…:8165:8169:8379:8381:14163:8380:…:           ESC Sensors
8010     :…:8381:8380:…:9251:                           Passenger Frontal Squib 1 Control
```

Sources of device IDs come from two spaces — `Device_ID` (FGA/data) and
`Diag_Device_ID` — so a lookup must try both. `code_*` columns are the
layer-2-obfuscated ones; use `alfaobd_decoded.db` (or decrypt with
`tools/decrypt_layer2.py`).

---

## 6. Security access (`0x27`) — where the algorithms live

### 6.1 The algorithms are shipped, named, and countable

**[observed]** `/Users/yorch/temp/alfa-obd/scratchpad/named/AlfaOBDKeys.cs`
(11,291 lines; identical to `scratchpad/dec/ad.cs`, only identifiers renamed)
contains **833 `CalculateKey*` functions**, **388 of them tagged `_PN`**:

```sh
cd /Users/yorch/temp/alfa-obd
grep -o "CalculateKey[A-Za-z0-9_]*" scratchpad/named/AlfaOBDKeys.cs | sort -u | wc -l   # 833
grep -o "CalculateKey.*_PN_[A-Za-z0-9_]*" scratchpad/named/AlfaOBDKeys.cs | sort -u | wc -l  # 388
```

Families and counts (**[observed]**, top of the distribution):

```
TCM 42  PCM 41  IPC 36  ICS 35  ABS 26  BPCM 25  FGA 22  EPS 22  BCM 21
RFH 20  HCP 20  ORC 18  HVAC 17  AFLS 16  AHCP 15  DTCM 13  RADIO 12 …
```

`findings/05-protocol.md` deliberately scoped vehicle security access *out*, so
this document is the first place the repo has this data. Note the size: AlfaOBD
does **not** compute keys from a calibration dump at runtime — it embeds one
C# function per (ECU, supplier, variant, algorithm) and picks one.

### 6.2 The naming convention decodes to FCA's own identifiers

**[observed]** Every name has the shape:

```
CalculateKeyS1_  <ECU>[_<family>]  _Sup<supplierID>  _Var<variant>  [_AlgID<algID>]  _V<rev>
```

Examples directly from the file:

```
CalculateKeyS1_ABS_PN_Sup0003_Var41_V01
CalculateKeyS1_ABS_PN_Sup0027_Var47_AlgID028F_V01
CalculateKeyS1_ABS_PN_Sup00C6_Var45_AlgID0226_V01
CalculateKeyS5_AHCP_PN_Sup00DD_Var51_AlgId02BF_V01
CalculateKeyS1_AFLS_PN_Sup0048_Var43_AlgID0140_V01
```

**[observed]** `Sup<XXXX>` matches FCA's **supplier ID table**, also recoverable
from the Android build — `analysis/jadx_out/sources/com/AlfaOBD/AlfaOBD/j8.java`
line 36 (`f4834j`):

```
0003 Bosch      0015 Hella      0022 Teves     0027 TRW       0030 Magneti Marelli
0032 Alpine     003C Conti Temic 0048 Valeo     0051 Automotive Lighting
0062 Eberspächer 0069 AISIN      0084 Harman/Becker
00C6 Continental 00C9 Mobis     00D2 Nexteer   00D6 Delta Electronics
00DD LG-CPI     00E5 Bitron     00F0 LG-E      00F4 Aptiv
```

So `CalculateKeyS1_ABS_PN_Sup00C6_Var45_AlgID0226_V01` reads as
"ABS/PowerNet, supplier **Continental**, variant `0x45`, algorithm ID `0x0226`,
revision 01". `S1`/`S5` are the two security levels (seed sub-functions); the
`V01`/`V02` suffix is a revision, and there are pairs with consecutive `AlgID`
values (e.g. `…AlgID01D8_V01` / `…AlgID01D9_V02`) — the odd/even pairing
suggests one ID for the *sendKey* sub-function and a derived one for the
follow-up. (**[inferred]**)

### 6.3 What selects the algorithm

**[observed]** The dispatcher is `CalculateKey()` at
`scratchpad/named/af.renamed.cs:78624`. It branches on:

| Field | Set from | Meaning |
|---|---|---|
| `m_aj` | case arms (`= 1`, `= 3`, `= 5`) | **key length** in bytes |
| `m_ge` | case arms (`51`, `55`, `57`, `58`, `41`…) | **seed length** / security level discriminator |
| `m_c0` | identification parse | **algorithm/variant selector** |
| `AlfaOBD.cx`, `AlfaOBD.cy` | identification parse | variant bytes |
| `AlfaOBD.c2` | `GetDeviceID()` | ECU/device identity |

and the identification parser that fills them is **[observed]** at
`af.renamed.cs:124150-124600`, indexing on `array[1] == 0xF1`, `array[2] == DID-low`:

| Response `array[2]` | Full DID | What is extracted |
|---|---|---|
| `165` (`0xA5`) | `F1A5` | `cv` = 5-byte **ISO/part code** (feeds `isocodes` → DeviceID) |
| `0` | `F100` | `cx = a[4]`, `cy = a[5]`; variant name via an embedded `(cx,cy)` table (**CUSW** ID) |
| `16` (`0x10`) | `F110` | `cx = a[3]<<8|a[4]`, `cy = a[5..8]` (4 bytes), **`m_c0 = a[11]<<8|a[12]`** (**Atlantis** ID) |
| `84` (`0x54`) | `F154` | **`m_c0 = a[3]<<8|a[4]`** (supplier ID) |

Separately, in the **KWP** branch (`switch` on the `0x5A` ECU-identification
response, not on a DID), `array[1] == 135` (`0x87` = Fiat identification option)
sets `cx = a[4] & 0x7F`, `cy = a[5]`, `m_c0 = "0" + a[3]`. That is a K-line/KWP
selector path, **not** UDS DID `F187` — worth keeping straight, because the byte
value collides.

This lines up with the three identification variants already recorded in
`findings/05-protocol.md` §2.7 and in the repo's
[`vehicle-support.md`](vehicle-support.md) §3.4: CAN-29-bit default `22 F1A5`,
CUSW `22 F100`, **Atlantis `22 F110`**.

**Consequence for our van [corroborated]:** the 2021 Pacifica is the debut
vehicle of FCA's **Atlantis** architecture, so `22 F110` is the identification
read that yields the algorithm selector. That makes `F110` the most important
single DID to add for the write path.

### 6.4 Is it reproducible offline? **Yes — with two caveats**

**[observed]** The function bodies contain their constants as **obfuscated
UTF-16 literals** decoded by `global::b.h(literal, key)`:

```csharp
public static byte[] CalculateKeyS1_ABS_Sup0003_V01(byte[] A_0)
{
    int a_ = 8;
    a();
    long[] array  = a(global::b.h("躽", a_), 10);
    long[] a_2    = a(global::b.h("辽", a_), 10);
    long[][] array2 = new long[24][]
    {
        a(global::b.h("\udabd\uf8bf\uf4c1ꋃ", a_), 16),
        ...
```

**[observed]** The decoder is fully known and already tooled in the RE tree:

- `scratchpad/dec/b.cs:503` — `int num = 1843161013 + A_1;` i.e.
  `n = 1843161013 + key`, applied per UTF-16 code unit
  (`findings/06-licensing-security.md` §0).
- `tools/decstrings.py` (`h_decode`), and a C# port in the macOS RE tree
  (`macos/tools/SourceDecoderPort/Policy.cs:245`, `Emitter.cs:13`).
- The same extractor pattern already works at scale: `tools/extract_ecu_catalog.py`
  regenerates the whole embedded ECU catalogue by decoding `b.h` literals from
  `af.cs` and verifying row-for-row.

So the offline path is: **decode the literals → the algorithms are plain C#
arithmetic on the seed → port to Python.** The two caveats:

1. **Portability of the algorithm is not the same as usefulness.** To *call* one
   you must supply the same selector inputs the app reads from the ECU
   (`F110`/`F100`/`F154` + `c2`/`cx`/`cy`, seed length). That mapping table
   (the `m_l2`/`m_lx`/`m_ly`/`m_lz`/`m_l0` (cx,cy)→variant tables, plus the
   `switch` arms chosen by `m_ge`/`m_aj`) is embedded but **not yet extracted or
   tabulated** — see §10.
2. **Per-ECU key material.** **[observed]** There is no per-vehicle secret in the
   functions: inputs are `(seed, variant, supplier, algID)` only. Nothing here
   reads a VIN, a calibration blob or a dongle. That is what makes offline
   reproduction possible at all — and it is also exactly why these are
   considered protected information by the vendor. **[inferred]** Some ECUs may
   still require additional read-only inputs recovered from the module itself
   (e.g. a part number); the `Sup`/`Var` naming suggests the *variant* is such an
   input, and it comes from the identification DIDs above, not from a dump.

**Verdict:** seed→key is **offline-reproducible for a large set of FCA ECUs,
including the `_PN` ones this van uses**, gated only by (a) implementing the
known string decoder and (b) recovering the selector table. Practical honesty:
this is a **weeks-to-port, per-ECU testing problem**, not a one-evening script,
and its only use in this repo is the guarded write path.

---

## 7. The write / configuration path

### 7.1 Services actually used

**[observed]** From `AF` send paths and the DB request templates, the write
vocabulary is ordinary UDS/KWP plus one proprietary habit:

| Operation | Service | Evidence |
|---|---|---|
| Read identification | `0x22 <DID>` (CAN), `0x21 <LID>` (KWP), `0x1A 87/97` (older) | `findings/05-protocol.md` §2.7, §5.2 |
| Read a config block | `0x22 <did>` / `0x21` (config `REQUEST`) | `SendECUDataQuery` `af:127533` builds `{0x22,0,0}` / `{0x21,b1,b2}` |
| **Write a config field** | **`0x2E <DID> <data>`** | `FGA_IPC_ROUTINES` 23× `2E…`; literal templates in `af.renamed.cs` e.g. `new byte[7] { 46, 40, 1, 0, 0, 0, 0 }` = `2E 28 01 …` (line 134670) |
| Actuator / output control | `0x2F <DID> 03 <value>` | 207 rows in `FGA_IPC_ROUTINES`; `SendActiveDiagnostic` `af:134610` |
| Routine | `0x31 01 <RID> …` | 2 rows; `ResendProc` polls `0x31`/`0x33` results (`findings/05-protocol.md` §3.6) |
| Write by local id (KWP) | `0x3B` | literals `new byte[4] { 59, 68, 0, 0 }` (line 135234), `{ 59, 33, 0, 0, 0 }` (137665) |
| Actuator index space | proprietary | `ABS_PNActiveDiag = byte[45]`, `IPC_PNActiveDiag = byte[180]`, `ORC_PNActiveDiag = byte[16]` — per-ECU **index→sub-function** tables, not raw frames (`AlfaOBDDefinitions.cs:49004/50164/50417`) |
| DTC clear | `0x14` | standard |

So: **adaptations are `0x2E` writes keyed by the same DID space the reads use**
(`BODY_PN_CONFIG.REQUEST`), **actuators are `0x2F … 03`**, **routines are
`0x31`**. There is no exotic FCA write service required for the parameter path.

### 7.2 Proxy alignment is a separate, app-level object

**[observed]** `findings/05-protocol.md` §3.7: the proxi configuration blob
`Proxy_Conf` is an application payload protected by a **CRC-16** written as 5
ASCII digits into `Proxy_Conf[6..10]` (`enterCRC`, `af:193537`;
`Crc16Mode.CcittKermit` = poly `0x8408`). **[observed]** `SendProxyAlignment()`
exists at `af:126105` and is called from the J2534/CANable reconnect paths and
`af:125396`. This is the "proxi alignment" operation the SGW discussion in
[`chrysler-sgw.md`](chrysler-sgw.md) refers to, and it is the specific procedure
that a bad write can require to recover (`U2101`).

### 7.3 What the SGW blocks

**[observed]** The DB knows the gateway explicitly: `ECUUnits` row 631,
`SGW_PN`, `canid=47E`, `canidresp=47F`, and `ECUList` ID 1458
`SGW_PN`, `Device_ID 99998`, **`Option = MY2018+ PowerNet`**.

```sh
sqlite3 -header -column "$DB" \
 "SELECT ID,unit_name,ECUNAME,inittype,canid,canidresp FROM ECUUnits WHERE ID=631;"
sqlite3 -header -line   "$DB" "SELECT * FROM ECUList WHERE ECUNAME='SGW_PN';"
```

Two useful facts fall out of `Option = MY2018+ PowerNet`:

- The gateway's own name-plate model year matches the published introduction
  window (MY2018, all models MY2019+), **[corroborating]**
  [`vehicle-support.md`](vehicle-support.md) §3.6.
- The SGW is addressed **outside the `0x7xx` range** (`0x47E`/`0x47F`) — so a
  naïve "scan `0x700`–`0x7FF`" will never find it, and **[inferred]** a scanner
  that wants to *report* "SGW present" must probe `0x47E` separately.

**[observed]** The repo's existing rule stands and is unchanged by any of this:
reads pass the gateway; writes (`0x14`, `0x27`, `0x2E`, `0x31`, `10 02`) do not,
on 2018+, and the only un-gating path is a physical **Chrysler 12+8** bypass.

---

## 8. Mapping to `pacifica-obd`

The repo already has the right skeleton: `uds.py` owns bus/protocol/DID
constants, `profiles.py` owns `VehicleProfile`, and `modules`/`read-did`/
`module-info`/`capture` already do read-only UDS. What is missing is the
**addressing rule**, the **data layer**, and a **guarded write path**.

### 8.1 New: `database.py` — load FCA module/DID data as data

Do **not** vendor the AlfaOBD database into this repo (it is proprietary and
licensed; `LICENSE`/`AGENTS.md` deliberately keep OEM data out). Instead make it
**bring-your-own**, exactly like the DB path handling the macOS port already
uses (`ALFAOBD_DB`, explicit path, no silent fallback):

```
$PACIFICA_OBD_FCA_DB=/path/to/alfaobd_plain.db   # optional, read-only
```

`database.py` responsibilities:

1. **Open read-only** (`file:…?mode=ro&immutable=1`) and refuse to migrate/write.
2. **Export to JSON** (`pacifica-obd fca export --out data/fca-ru.json`) so the
   tool works without the DB present — the same "ship data, not code" split the
   repo already uses for `DID_NAMES`/`INFO_DIDS`.
3. **Normalize the primary key** to `(bus, ecu, request, response)` — never a
   bare request ID ([§4.2](#42-the-collisions-you-must-not-get-wrong)).
4. Load, per ECU: identification DIDs, `FGA_*` live-data recipes, config
   (`BODY_PN_CONFIG`) rows, `Diag_*` routine/actuator names + descriptions, DTC
   text for that device, and the address pair.

Sketch of the read side:

```python
# database.py (proposed) — pure, no I/O outside the one read-only connection
def load_modules(db_path: Path, family_glob: str = "*_PN") -> list[ModuleRecord]:
    """ECUUnits rows + ECUList Option dating, keyed by (bus, req, resp)."""
def load_live_pids(db_path: Path, ecu: str) -> list[PidRecipe]:
    """FGA_* rows for the ECU's Device_ID(s): request, bit_pos, bit_len, slope, offset."""
def load_config(db_path: Path, ecu: str) -> list[ConfigField]:
    """BODY_PN_CONFIG rows: request(DID), bit, length, setting, enum labels, group."""
def load_routines(db_path: Path, device_id: int) -> list[Routine]:
    """Devices_Diagnostics ⋈ Diag_names ⋈ Diag_descriptions, plus FGA_IPC_ROUTINES."""
def load_dtcs(db_path: Path) -> dict[tuple[str, int], str]:
    """Faults: hexcode+device → text (use the decoded DB, or decrypt layer 2)."""
```

Reuse the repo's existing constraint: **persistence/DB problems never break a
command** — if the FCA DB is absent, `modules`/`read-did` still work from the
built-in defaults.

### 8.2 `profiles.py` — real addresses plus a rule engine

Evolve the existing `VehicleProfile` (see the YAML already drafted in
[`vehicle-support.md`](vehicle-support.md) §5) with the two things this DB now
lets us fill in with **[observed]** values:

```python
addressing = AddressingRule(default=Offset(-0x280), exceptions=[
    OffsetRange(0x7E0, 0x7E7, +0x8),      # OBD-II block incl. HCP 0x7E2
    Special(0x47E, 0x47F),                # SGW_PN
    Special(0x784, 0x785),                # ABS_CHRYSLER (legacy, per-bus!)
])
```

```python
modules = {
  "can-c": {
    "PCM":  Addr(0x7E0, 0x7E8, conf="verified", src="alfaobd-db+opendbc"),
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
# PHEV delta (per vehicle-support.md): HCP 0x7E2/0x7EA, EBCM 0x7E3/0x7EB,
# ABSO 0x7E4/0x7EC, BPCM 0x7E7/0x7EF, OBCM 0x76E/0x4EE, AHCP 0x76C/0x4EC,
# APM 0x76A/0x4EA, EAC 0x76B/0x4EB.
```

Also fix the module-list gaps [`vehicle-support.md`](vehicle-support.md) §5.7 already
identified, now with concrete evidence: the current `_COMMON_CAN_C` in
[`profiles.py`](../src/pacifica_obd/profiles.py) lists **`APM`**, but the DB's
`APM_PN` (`0x76A/0x4EA`) and Appcar both put the Accessory Power Module on the
**PHEV** side — so `APM` should move to the PHEV delta. (Note: that section's
claim that `_COMMON_CAN_C` contains `HCP`/`EAC` is **stale** — the current list
does not; verify code against docs before "fixing".) And the scan range must admit
`0x47E` so the gateway is reported.

Change `uds.probe_ids`/`cli.cmd_modules` to take the range and the probe from the
profile, and to **report the response ID it actually saw** so the `−0x280` rule
is validated in the field rather than assumed.

### 8.3 New: `seedkey.py` — write-path only, opt-in

- Target: port a **specific, small subset** of the 388 `_PN` functions, not all
  833. Start with the modules a read-only-then-guarded-write roadmap would touch
  first (`IPC_PN`, `BCM`, `RFH_PN`, `EPS_PN`), and only after recovering the
  selector table (§6.4 caveat 1).
- Needs the **string decoder** (`1843161013 + key`, per UTF-16 unit) as its first
  unit-tested piece — the RE tree has a reference implementation and a
  row-for-row verification pattern (`tools/extract_ecu_catalog.py`) to copy.
- Must be **import-guarded**: nothing in the read path may import it, so that the
  read-only tool has no security-access capability at all.

### 8.4 New: `config` / `write` — guarded, backup-first

Follow the shape the repo already uses for `dtc --clear`
(explicit flag + confirmation + `--force` required in `--json` mode), and raise
the bar:

```
uv run pacifica-obd config read  --module BCM --bus can-ihs          # read-only, safe
uv run pacifica-obd config read  --module BCM --group "Cabin Net"    # decode via BODY_PN_CONFIG
uv run pacifica-obd config backup --module BCM --out bcm-backup.json # REQUIRED before any write
uv run pacifica-obd config write --module BCM --did 0122 --field "Country Code" --value 2 \
        --backup bcm-backup.json --i-understand-the-risk
uv run pacifica-obd routine run  --module ABS --routine "ABS Bleed Brakes" --i-understand-the-risk
```

Hard rules (these belong in `AGENTS.md` and in code, **not** in data — see
[`vehicle-support.md`](vehicle-support.md) §6.3):

1. No write without a fresh, complete read of the target block persisted first.
2. No write when the checklist in §9 has not passed on this VIN in this session.
3. `0x27` is attempted, never guessed: if the algorithm is unknown for the
   observed `(Sup, Var, AlgID)`, **refuse** rather than brute-force.
4. Writes stay behind explicit user intent; `security.write_path_enabled` must
   **not** be overridable by profile data.
5. Every attempted and refused write is appended to `events.jsonl`.

### 8.5 Phased plan

| Phase | Deliverable | Depends on | Risk |
|---|---|---|---|
| **P0** | `database.py` read-only loader + `fca export` to JSON; `tests` on the query layer | nothing | none |
| **P1** | Address rule engine + real module map in `profiles.py`; `modules` reports response IDs; probe `0x47E` for SGW | car | none (read) |
| **P2** | Config **read**: `config read` decodes `BODY_PN_CONFIG` fields via `0x22 <DID>`; store snapshots | car | none (read) |
| **P3** | Live FGA PID decode (`FGA_*` recipes) → new `pids`/`monitor` source | car | none (read) |
| **P4** | Routine **run** for the safe subset (lamp tests, pump tests) via `0x2F`/`0x31`; still no parameters | P1, SGW plan | medium (actuation) |
| **P5** | Guarded `config write` (`0x2E`) behind backup + explicit intent | P2 + seed/key + bypass | **high** |
| **P6** | Proxi alignment — *only* if ever needed to recover from P5 | P5 | **very high** |

Everything up to **P4** is available without touching seed/key at all: reads and
simple actuator tests pass the gateway. **P5 is the line**, and on 2018+ it needs
the 12+8 bypass.

### 8.6 Risks

| Risk | Why | Mitigation |
|---|---|---|
| **Bricking a module** | A wrong `0x2E` DID/bit can leave a module unconfigured; a wrong proxi blob sets `U2101` across the bus | backup-first read of the whole block; refuse when the field's `bit`/`length` provenance is `unknown`; never write a field whose `SETTING` is safety-relevant (brakes, restraint, steering) |
| **Restraints / brakes** | `Diag_*` includes `ABS Bleed Brakes`, `Brake Pedal Calibration`, `Sensor Cluster Calibration`, squib tests | keep actuation out of `config`; require an explicit `routine` subcommand with an interactive confirm |
| **SGW** | Blocks `0x14/0x27/0x2E/0x31` on 2018+; "reads pass" is the *only* reason P0–P3 are useful today | treat write capability as blocked-by-default; document the 12+8 hardware requirement; detect the gateway at `0x47E` |
| **Warranty / ToS** | Reverse-engineered seed/key use and adaptation changes can void warranty and may violate the vendor's terms | keep `seedkey.py` out of the default import graph; do not ship the AlfaOBD DB; document intent in `--help` |
| **Data licensing** | The DB is the vendor's product | bring-your-own path; export only what the user extracts; do not commit `*.db` or exported JSON (extend `.gitignore`) |
| **Bus collision** | `0x784` etc. mean different modules per bus (§4.2) | address = `(bus, req, resp)` everywhere; profile carries module→bus and the `modules` scan is per-bus |
| **Low battery** | Flaky NRCs / false "no module" while writing | check `ATRV` ≥ 12.4 V before any write; the repo already has `--check-voltage` |

---

## 9. Verification checklist on a real Pacifica

Run **in this order**. Do not proceed to a later block before the earlier one is
green; record everything locally (never commit VINs — see `.gitignore` and
`vehicle-support.md` §6.2).

### Pre-flight

- [ ] `uv run pacifica-obd ports` finds the vLinker FS; `raw ATI` / `raw STI` shows
      an STN11xx/STN21xx (not a bare ELM327 v1.2).
- [ ] `raw ATRV` ≥ 12.4 V. Ignition **ON**, engine off.
- [ ] Record model year, trim, gas/PHEV locally. Note whether an SGW bypass is fitted.
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
      variant) — does `0x47E` answer at `0x47F`? This validates the SGW row and the
      need for an out-of-range probe.
- [ ] Resolve the **BCM** unknown: does `0x620` answer `0x504`? Does `0x784` on
      CAN-C answer `0x785` (legacy ABS) or nothing?
- [ ] Record any responder outside both lists.

### Session behaviour per responder

- [ ] `10 03` → `50 03`, or an NRC. **This is the highest-value unknown:** does the
      SGW pass extended-session on the stock port?
- [ ] `22 F190` in the **default** session (tests session-free reads).
- [ ] `22 F1A5` → ISO/part code; check it against `isocodes` to learn the
      `Device_ID`.
- [ ] **`22 F110`** → Atlantis identification. Capture the raw bytes; record
      `m_c0` candidates from bytes 11–12. This is the seed/key selector.
- [ ] `22 F100` (CUSW), `22 F154` (supplier ID) — capture, for comparison.
- [ ] `22 F187 F188 F189 F191 F194 F132` → part/software/serial block.
- [ ] `19 02` → DTCs; fall back to `19 01` / `19 0A`.
- [ ] Classify every NRC (`11/12/13/22/31/33/7E/7F/78`) and record the actual
      response ID seen per request.

### Config read path (P2)

- [ ] Pick the BCM and read **`0x0122`** (`VehConfig 1`). Decode `Model Year`,
      `Vehicle Line`, `Country Code` per `BODY_PN_CONFIG` (`BIT`/`LENGTH`).
      **Cross-check the decoded model year against the VIN.**
- [ ] Read `0x0123` (`VehConfig 2`) and `0xF010` (`Cabin Net`) and confirm the
      response length matches the highest `BIT`+`LENGTH` in the table.
- [ ] **Do not write anything.**

### Read-only routine check (only if a safe one exists)

- [ ] If and only if a purely passive lamp/indicator test is selected from
      `Diag_names` for an accessible module, run it once with the vehicle
      stationary, and confirm the pre-flight warnings in `Diag_descriptions` were
      read aloud.

### Ground truth diff

- [ ] Scan the same vehicle with AlfaOBD or Appcar on the same day; diff the module
      list and the `VehConfig 1` decoding. Any module they see and we do not is a
      bug in our map, our rule, or our probe.

---

## 10. Open unknowns

1. **`inittype` → bus/baud semantics.** The 7/9 split matches CAN-C/CAN-IHS
   perfectly across 67 rows and the `adapter` column agrees, but the numeric enum
   mapping (`eEcutype` switch at `af.renamed.cs:99014`) is not recovered.
   **Do not hardcode it** — probe `STPRS` per bus instead
   ([§4.4](#44-inittype-7-vs-9-correlates-with-can-c-vs-can-ihs)).
2. **The seed/key selector table.** 833 named functions exist and their constants
   are decodable, but the mapping from `(m_c0, cx, cy, c2, m_ge, m_aj)` to a
   specific function is embedded in a large switch plus the `m_l2`/`m_lx`/`m_ly`/
   `m_lz`/`m_l0` variant tables and has **not** been extracted or tabulated. Until
   it is, seed→key is *reproducible in principle, unimplemented in practice*
   ([§6.4](#64-is-it-reproducible-offline-yes-with-two-caveats)).
3. **The 2021 Atlantis architecture.** The van's MY2021 change is documented
   (Uconnect 5, TBM, "Atlantis replaces Powernet"), and AlfaOBD implements
   `F110` Atlantis identification — but this DB version still names the modules
   `*_PN` with no Atlantis-specific `ECUNAME`, and no new addresses are visible.
   Whether any 2021+ RU module moved, was added (TBM), or changed DID space is
   **unverified**, and is the main reason P1 must be validated on the actual car
   before P2+ is built on top.

Further, narrower gaps already flagged elsewhere and still open: the exact BCM
identity/address (§3.3), whether a third "Hybrid LAN" bus exists
(`vehicle-support.md` §9), and whether `0x47E` is the only gateway address.

---

## Appendix: provenance

**Database** (read-only, never modified):

- `/Users/yorch/temp/alfa-obd/app-win/AlfaOBD_PC/alfaobd.db` — shipped, encrypted
- `/Users/yorch/temp/alfa-obd/scratchpad/db/alfaobd_plain.db` — RC4-decrypted (used above)
- `/Users/yorch/temp/alfa-obd/scratchpad/db/alfaobd_decoded.db` — + layer-2 3DES decoded
- Decode tooling: `tools/decrypt_db.py`, `tools/decrypt_layer2.py`, `tools/decstrings.py`

**Source** (decompiled / renamed):

- `scratchpad/named/af.renamed.cs` — main app class (~869k lines)
- `scratchpad/named/AlfaOBDKeys.cs` = `scratchpad/dec/ad.cs` — 833 seed→key functions
- `scratchpad/named/AlfaOBDDefinitions.cs` — per-ECU PID/routine byte tables
- `scratchpad/dec/b.cs:503` — the `1843161013 + key` string decoder
- `analysis/jadx_out/sources/com/AlfaOBD/AlfaOBD/j8.java` — FCA vehicle-line,
  package and **supplier** tables
- `tools/extract_ecu_catalog.py` — worked example of decoding `b.h` literals at scale

**Prior findings in the RE tree** (all read):

- `findings/04-database.md` — codec + full schema
- `findings/05-protocol.md` — transport, framing, services, decode formulas
- `findings/06-licensing-security.md` — §0 string obfuscation
- `findings/03-naming-restoration.md` — renaming map / call-site example

**This repo**: `AGENTS.md`, `src/pacifica_obd/{uds,profiles,cli}.py`,
`docs/{enhanced-diagnostics,vehicle-support,chrysler-sgw}.md`.
