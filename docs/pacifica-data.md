# Bundled Pacifica dataset

`pacifica-obd` ships its own Pacifica (RU-platform) dataset so the toolkit is
**self-contained**: no external ECU database, no configuration path and no
network access are required at runtime. The data lives in one resource,

```
src/pacifica_obd/data/pacifica.json
```

and is read through `importlib.resources` by
[`pacifica_obd.pacifica_data`](../src/pacifica_obd/pacifica_data.py), so it also
works from an installed wheel or zip.

## Loader API

```python
from pacifica_obd import pacifica_data

pacifica_data.modules()                # list[dict]  — addressed ECUs
pacifica_data.dids("ABS_PN")           # list[dict]  — data items for that ECU
pacifica_data.adaptations("BODY_CHRYSLER")   # list[dict] — writable config
pacifica_data.dtc_text("0CD1")         # "Charge Port Door Control" | None
pacifica_data.dataset()                # the whole parsed mapping
```

ECU lookups are case-insensitive; unknown modules return `[]` and unknown DTC
codes return `None`.

## Schema and row counts

`pacifica.json` is a single JSON object (`schema` = `pacifica-obd.dataset/1`,
`platform` = `RU`):

| Key | Shape | Count |
|---|---|---|
| `modules` | `list` of ECU records | **48** (24 CAN-C, 24 CAN-IHS; 42 gas, 6 PHEV) |
| `dids` | `{ecu: [ {kind, name, unit} ]}` | **960** items across 35 ECUs (347 `pid`, 613 `routine`) |
| `adaptations` | `{ecu: [ {request, bit, size, name, group, array, values} ]}` | **901** entries (880 with enumerated values), all for `BODY_CHRYSLER` |
| `dtc_text` | `{code: short English text}` | **4000** codes |
| `seed_key` | marker | — |

A `modules` record looks like:

```json
{
  "name": "Anti Lock Brakes OBD",
  "ecu": "ABSO_PN",
  "years": "2017-2027",
  "powertrain": "gas",
  "bus": "can-c",
  "inittype": 7,
  "ecu_address": 0,
  "request": "0x7E4",
  "response": "0x7EC",
  "response_rule": "request+8",
  "adapter": "0",
  "baudrate": 0,
  "device_ids": [10276],
  "options": ["MY2017 PowerNet"]
}
```

Bus mapping: `inittype` 7 → `can-c`, 9 → `can-ihs`. `response_rule` records how
the response ID relates to the request ID in the source data: `request+8` for
the OBD-II block (0x7E0–0x7E7), `request-0x280` for the CAN-IHS and most CAN-C
body modules, `request+1` for the Security Gateway, and `explicit` where the
source gives no derivable rule. Both OBD-II-block and `-0x280`/CAN-IHS sets are
included; the dataset stores absolute IDs, so there is no need to derive them.

## How applicability was decided

The upstream family list has **no textual `RU` / Pacifica flag**; it identifies
vehicles only by numeric brand/model ids in per-brand columns. The heuristic is:

> A row is **Pacifica (RU) applicable** when its `Chrysler` brand column carries
> model id **20** — the MY2017+ "PowerNet" (FCA CAN) Chrysler generation.

Justification:

- Id 20 tags exactly the MY2017-and-later "PowerNet" rows, and the set it
  produces is the RU module set: `BODY_CHRYSLER` (MY2017…MY2020-21), `PENTASTAR`
  (3.6 L V6 PCM), the full PowerNet body/chassis cluster (`ABS_PN`, `IPC_PN`,
  `EPS_PN`, `ESM_PN`, `ORC_PN`, `RFH_PN`, `SCCM_PN`, `ACC_PN`, …, `ICS_PN`,
  `HVAC_PN`, the door/sliding-door modules) **plus** the hybrid-only controllers
  `HCP_PN`, `BPCM_PN`, `OBCM_PN`, `AHCP_PN`, `APM_PN`, `EAC_PN`.
- Those hybrid controllers exist on no other Chrysler-badged model, which pins
  id 20 to the Pacifica/RU family rather than to a sedan or truck.
- A sibling id group (`100/101/102`, "MY2016+ / MY2019+ PowerNet") was **not**
  used: it does not form the RU set (it adds, for example, a non-RU gasoline
  engine ECU) and appears to be a different Chrysler PowerNet generation.

Model years are derived from the upstream `MYxxxx`/`MY>xxxx`/`MYxxxx+` option
labels and clamped to the 2017–2027 RU span; rows with no year label default to
the full span.

## What is and isn't included

Included (factual identifiers, kept compact):

- **Module addressing** — request/response CAN IDs, bus, adapter, raw ECU
  address, and the applicability labels.
- **Per-module data items** — live-data parameter names with units (`pid`) and
  actuator/diagnostic routine names (`routine`) the source keys to each ECU.
- **BCM configuration** — `BODY_CHRYSLER` `PowerNet` settings: request opcode,
  bit offset, bit width, setting name, group, and the enumerated allowed values.
- **DTC text** — short English descriptions for the codes the Pacifica module
  set can report.

Not included (deliberately):

- **No DID hex identifiers, byte sizes or byte→value formulas** for the RU
  module set. The upstream source stores only parameter *names/units* for these
  ECUs; the formula-bearing tables it does have are for other market ECUs and do
  not key to any RU module, so they are omitted rather than guessed.
- **No seed/key algorithm or constants *in this dataset*.** `seed_key` records
  that per-ECU identifiers exist; the built-in PowerNet seed→key algorithms and
  their selector live in `pacifica_obd.fca_keys` (see `fca-keys.md`), and the
  pluggable provider model (`seedkey.py`) remains for anything else.
- **No long prose** — descriptions are truncated to short phrases.
- Later RU model years that the upstream tagging does not carry a Pacifica flag
  for (some MY2021+ body rows are unflagged) are not matched by the heuristic;
  the `years` field therefore tops out at MY2021 for `BODY_CHRYSLER` while the
  vehicle span runs to 2027.

## Regenerating the dataset

The dataset is a deterministic projection of the reference FCA ECU database
used during development. To regenerate it, rebuild `pacifica.json` from that
database with the following recipe (no absolute paths are stored anywhere in the
repo):

1. **Applicable ECUs.** `SELECT ECUNAME, Device_ID, Option FROM <ecu-list>`
   where the Chrysler brand column contains model id `20`. This gives the ECU
   names and their upstream device ids.
2. **Integrity check.** Require every applicable `ECUNAME` to resolve in the
   ECU-address table (`<ecu-units>`); abort if any is missing.
3. **Modules.** For each applicable name, copy `unit_name, ECUNAME, inittype,
   ecuaddress, canid, canidresp, adapter, baudrate`, parse `canid`/`canidresp`
   as hex, and map `inittype` 7→`can-c`, 9→`can-ihs`. Compute `response_rule`
   (`request+8` / `request-0x280` / `request+1` / `explicit`). Deduplicate on
   `(ecu, request, response, inittype)`.
4. **DIDs.** Join the parameter-device table by `Device_name` equal to the ECU
   name or `"<ECU>_<Device_ID>"`, then the parameter link and name/unit tables
   → `kind = "pid"`. Join the diagnostic-device table by device name, then the
   device-diagnostics and diagnostic-name tables → `kind = "routine"`.
   Deduplicate, preserving order.
5. **Adaptations.** Keep the BCM `PowerNet` configuration rows whose support
   column for any applicable `BODY_CHRYSLER` device id is set; emit `request`
   (opcode, hex), `bit`, `size`, `name`, `group`, `array`, and the non-empty
   allowed-value labels.
6. **DTC text.** Keep fault rows whose (colon-delimited) device list intersects
   the applicable device ids; for each code keep the shortest non-empty English
   description.

Finally, run the checks: the JSON must parse, `pacifica_data.modules()` /
`pacifica_data.dids("ABS_PN")` must be non-empty, and no shipped file may
reference a machine-specific path.
