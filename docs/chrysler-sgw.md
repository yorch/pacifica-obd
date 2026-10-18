# Chrysler Security Gateway (SGW) & enhanced diagnostics — 2021 Pacifica

Notes for going beyond generic OBD‑II on a 2021 Chrysler Pacifica (RU platform).
This is background/reference; `pacifica-obd` itself stays on the safe, generic
OBD‑II side.

## TL;DR

- **Reading** generic OBD‑II (engine/transmission DTCs, live data, readiness)
  works over the normal OBD port — **no bypass needed**.
- The 2021 Pacifica **has a Security Gateway Module (SGW)**. Any **write**
  (clearing codes, adaptations, activations, configuration) generally requires a
  **12+8 SGW bypass cable**.
- **Configuration/coding is not part of OBD‑II at all.** It needs a
  manufacturer-aware tool: **Appcar DiagFCA** (Windows; your USB
  vLinker FS works) or **JScan** (mobile-only, Bluetooth adapter).
- Full module programming / "proxy alignment" is dealer **wiTECH** territory.

## What the SGW is

A firewall module between the OBD‑II port and the vehicle's internal networks.
Its job is to stop unauthorized writes to modules. Authentication is limited to
Chrysler‑licensed devices, so third‑party tools cannot *write* through it.

- Introduced in 2018 on some FCA models, standard from 2019.
- **Location varies by year/trim** (2021 especially): behind the center
  stack/climate controls, behind the radio (Magnetti Marelli unit), or nearby.
  Check a model-specific guide before pulling trim.

## Read vs. write

| Action | SGW bypass needed? |
|---|---|
| Read generic DTCs (PCM/TCM) via OBD‑II | **No** |
| Read DTCs from *all* modules (ABS, BCM, SRS…) via Appcar/JScan | Usually **no** (reads pass through) |
| Clear codes | **Yes** (per JScan docs, writes are blocked) |
| Live data | No for reads; **yes** for activations/controls |
| Adaptations / configuration (BCM settings, lighting, locks, TPMS…) | **Yes** |
| Module programming / proxy alignment | **Yes** + licensed/dealer tool |

> JScan's own documentation: *"The bypass cable is needed to WRITE any data to
> the vehicle. This includes clearing trouble codes, adaptations and activations.
> If you only need to READ the data … you won't need it."*

## Hardware

- **12+8 SGW bypass cable** (a.k.a. "Chrysler 12+8", Autel-style). Brands seen:
  Autel, Z Automotive, 4D Tech, generic. An **extension cable** makes it far
  easier to reach — the Pacifica's SGW is often deep behind the dash.
- **OBD adapter:**
  - USB **vLinker FS** → works with **Windows** Appcar DiagFCA.
  - **JScan** is mobile-only → needs a **Bluetooth** adapter (vLinker MC+/FD+,
    OBDLink MX+, Vgate iCar Pro BLE).
- A Windows laptop.

## Software options

| Tool | Platform | USB vLinker FS | Strengths | Limits |
|---|---|---|---|---|
| **Appcar DiagFCA** | Windows | ✅ | 500+ editable BCM parameters, module tests | Paid; Windows only |
| **JScan** | iOS / Android | ❌ (BT only) | All modules, adaptations, activations | Mobile only; needs BT adapter |
| **wiTECH** | Windows (dealer) | — | Full programming, proxy alignment | Dealer/licensed |

## Procedure outline (Appcar DiagFCA + USB vLinker FS)

1. Ignition **OFF**. Locate the SGW, unplug its two connectors, and plug them
   into the bypass cable (adapter then connects to the bypass).
2. Connect the vLinker FS.
3. Ignition **ON** (engine off is fine for most reads/adaptations).
4. In the tool: pick the vehicle, connect, then read codes / modules.
5. **Before changing anything:** record the current configuration (Appcar DiagFCA
   and JScan both have trace/restore). Change one thing at a time.

## Risks

- A bad configuration change can set **`U2101` "Control Module Configuration
  Incompatible"**, which needs a **proxy alignment / Restore Vehicle
  Configuration** to clear.
- Some settings are safety-related and may be ignored or have side effects.
- Modifications can affect warranty and emissions compliance — your call.
- Always be able to revert; keep the bypass and a record of changes.

## Where `pacifica-obd` fits

- ✅ Generic reads: `dtc`, `live`, `readiness`, `freeze`, `vin`, `scan`.
- ⚠️ `dtc --clear` sends the standard mode‑04 clear, but on a 2021 the SGW will
  likely block it without a bypass — expect it not to work on the stock port.
- ❌ Configuration/coding: out of scope by design (not an OBD‑II capability).
