# Enhanced (AlfaOBD-class) diagnostics — how it works and how we'd add it

This document maps out what it would take to grow `pacifica-obd` from *generic
OBD‑II* into *manufacturer-aware FCA/Stellantis diagnostics* — the class of
things AlfaOBD and Appcar DiagFCA do. It is an investigation + plan, not a claim
that all of it is safe or easy.

## 1. How AlfaOBD actually works

AlfaOBD is not magic; it is a stack:

| Layer | What it does | Standard |
|---|---|---|
| Adapter | ELM327/STN ↔ USB/Bluetooth; does ISO‑TP framing | ELM327 AT set |
| Transport | ISO‑TP (ISO 15765‑2) segmentation/reassembly | ISO 15765‑2 |
| Application | UDS services (0x10, 0x19, 0x22, 0x27, 0x2E, 0x31…) and legacy KWP | ISO 14229 |
| Addressing | Per-ECU request/response CAN IDs | manufacturer |
| Semantics | A database of modules, DIDs, RIDs, DTCs, and adaptation parameters | FCA-specific |
| Security | Seed→key (0x27) for protected writes; SGW bypass physically | FCA-specific |

So "native ECU support" = **a big proprietary database + UDS client**, not a
different physical protocol. The vehicle still speaks UDS over CAN.

## 2. What our hardware can already do

Your vLinker FS reports **`STN1170 v4.3.2`** (from `STI`). That matters:

- STN11xx is **ELM327-compatible** *and* supports **raw CAN** (ISO 11898).
- Per Appcar's docs, **STN1170/2120/2230/2231 natively support CAN‑IHS on pins
  3/11** — no extra cross-adapter needed for the Pacifica's second bus.
- Useful commands we can drive today: `ATSH <hdr>`, `ATSP6` (CAN 11/500),
  `ATCAF0/1` (CAN auto-format off/on), `ATCFC0/1` (flow control),
  `ATAL` (allow >7-byte messages), `ATH1` (headers), `ATCSM`, `ATMA`.

**Conclusion:** the adapter is sufficient for UDS on both CAN‑C (pins 6/14) and
CAN‑IHS (pins 3/11). The only physical blocker is the **Security Gateway** for
writes (see [`chrysler-sgw.md`](chrysler-sgw.md)).

> Note: this is classic CAN. If a module speaks **CAN FD**, the STN1170 cannot
> reach it. A 2021 Pacifica's diagnostics appear to be classic CAN‑C/IHS.

## 3. Pacifica (RU) module map

From NHTSA scan reports for 2022–2023 Pacifica, the modules and their buses are:

| Bus | Modules |
|---|---|
| **CAN‑C** (HS, pins 6/14) | PCM, BCM, ABS/EBCM, ORC (airbag), EPS, IPC (cluster), SCCM, RFH, ESM (shifter), TCM, OCM, FFCM, ACC, PTS, EAC, BPCM, APM, HCP, ESC, ECM, OBCM, AHCP |
| **CAN‑IHS** (MS, pins 3/11) | PLGM (liftgate), ICS (center stack), HVAC, DDM/PDM/DMRL/DMRR (doors), PSDML/PSDMR (sliding doors), MSM/CSWM (seats) |

Generic OBD‑II only talked to the PCM/TCM. Everything else needs UDS to the
module's own CAN ID pair.

## 4. What's missing (the hard parts)

1. **Module ID map** — each ECU's request/response CAN IDs. Partly discoverable
   by scanning (send `10 03` / `22 F1 90` across an ID range), partly needs the
   FCA database.
2. **DID / RID database** — which 0x22 identifiers mean what, and which 0x31
   routines exist. This is the bulk of AlfaOBD's value.
3. **Seed→key algorithms** for protected writes (0x27). Reverse-engineered
   per-ECU generators exist (see §7), but using them is the risky part.
4. **SGW bypass** for any write.
5. **Semantic decoding** — scaling/formulas for each DID.

## 5. Feasibility matrix

| Capability | Feasible? | Depends on |
|---|---|---|
| Discover responding modules (`10 03`, `22 F190`) | **Yes** | scan + pinned protocol |
| Read all-module DTCs (0x19 02) | **Yes** | module map; SGW usually allows reads |
| Clear codes (0x14) | Likely blocked stock | SGW bypass |
| Module info (part #, SW version: F187/F188/F189/F190) | **Yes** | DID map |
| Live manufacturer data (periodic 0x22) | **Yes** | DID map + formulas |
| Activations / routines (0x31) | With bypass | RID map |
| Adaptations / configuration (0x2E / routines) | **Hard** | bypass + seed-key + parameter DB + backups |
| Module programming / flashing | **No** | J2534 + OEM files |
| Key/fob programming | **No / very risky** | licensed data, PIN, dealer |

Reads are the realistic target. Writes are a separate, higher-risk project.

## 6. Proposed architecture for this repo

```
src/pacifica_obd/
  cli.py        # existing commands (+ new ones)
  elm.py        # raw ELM327/STN transport          <-- implemented
  isotp.py      # ISO-TP framing (only if we bypass ELM's own framing)
  uds.py        # UDS client: 0x10, 0x19, 0x22, 0x27, 0x2E, 0x31
  modules.py    # Pacifica ECU map (IDs, buses, names)
  database.py   # DID/RID/DTC tables (data-driven, loadable JSON)
  seedkey.py    # FCA seed->key (optional, write path only)
```

Two implementation strategies:

- **A — leverage the adapter's ISO‑TP** (recommended first): `ATSP6`, `ATSH <req>`,
  `ATCAF1`, `ATCRA <resp>`; send the UDS payload as hex and read the response.
  This is exactly how AlfaOBD drives an ELM327 and needs no extra libraries.
- **B — standards stack**: `udsoncan` (MIT) + `can-isotp` + a custom
  `python-can` bus that tunnels frames through `elm.py`. Cleaner for large
  multi-frame DIDs, more moving parts.

Start with A; move to B if multi-frame handling gets painful.

## 7. Data sources

- **FCA ECU/CAN map, Mode‑22 PIDs, DTC descriptions** — previously published in
  `grantUser/fca-ecu-diagnostics` (recovered from an FCA diagnostic app's local
  database). The repo is currently 404, so treat it as a lead to re-derive or
  mirror, not a dependency.
- **FCA seed→key generators** — `grantUser/seedkey-fca-stellantis` (≈374 per‑ECU
  generators recovered from AlfaOBD bytecode). Also currently 404. Same caveat.
- **`grantUser/obd2-pids`** — large JSON PID database (Mode 01 + Mode 22), useful
  for DID/formula scaffolding.
- **NHTSA public scan reports** — module lists and bus assignments (used in §3).
- **Community forums** — real-world module IDs and known-good procedures.
- **Reverse engineering your own car** — scan IDs, brute-force DIDs, capture
  responses. Slow but self-contained and legal for personal use.

> Legal/ethical: much of the above is reverse-engineered proprietary material.
> Use it for your own vehicle; don't redistribute OEM data, and respect the
> licenses/terms of any source you consult.

## 8. Phased plan

| Phase | Deliverable | Needs a car? |
|---|---|---|
| **P0** ✅ | Raw ELM327/STN transport + `pacifica-obd raw` | No (AT tested) |
| **P1** ✅ | `modules` — scan CAN‑C/CAN‑IHS for responders (`10 03`, `22 F190`) | Needs car for data |
| **P2** ✅ | `dtc-all` — read DTCs (UDS `0x19 02`) from every responder, decode + JSON | Needs car for data |
| **P3** ✅ | `read-did` (UDS `0x22`) + `module-info` (F186–F191), reply-correlated | Needs car for data |
| **P2** | `dtc-all` — read DTCs (0x19 02) from every responding module, JSON | Yes |
| **P3** | `read-did <module> <did>` + module info (F187–F190) | Yes |
| **P4** | Live manufacturer PIDs (periodic 0x22) from the database | Yes |
| **P5** | Routines/activations (0x31) for an explicit allow-list | Yes + bypass |
| **P6** | Adaptations (write path) — opt-in, backup-first, seed-key | Yes + bypass |

## 9. Risks

- **Writes can brick modules** or set `U2101` (needs proxy alignment).
- **SGW** blocks writes before any of the above matters.
- **Wrong DID/RID** on a write can change safety behaviour.
- **Warranty/emissions** implications.
- Reverse-engineered seed-key use may violate terms of service.

Reads (P1–P4) are low-risk and where the value is. Treat P5–P6 as a separate,
deliberate project with backups and a bypass.

## 10. What exists now

`pacifica-obd raw` and `pacifica-obd modules` (P1) are implemented and tested
against the adapter:

```sh
# adapter-level
uv run pacifica-obd raw ATI ATRV --json

# point at a module and ask for the VIN DID over UDS
uv run pacifica-obd raw 22F190 --header 7E0 --protocol 6 --json

# raw CAN mode (no ISO-TP auto-framing)
uv run pacifica-obd raw 1003 --header 7E0 --protocol 6 --no-auto-format --json

# module discovery (P1): CAN-C (HS, pins 6/14), then CAN-IHS (MS, pins 3/11)
uv run pacifica-obd modules --bus can-c   --identify
uv run pacifica-obd modules --bus can-ihs --identify --json

# all-module DTCs (P2): discover, then read 0x19 02 from each responder
uv run pacifica-obd dtc-all --bus both --json

# module identification (P3): read DIDs (0x22), incl. VIN / part / software
uv run pacifica-obd read-did --module 7E0 F190 F187 F189
uv run pacifica-obd module-info --bus both
```

`modules` pins the STN protocol preset (33 = HS 500K/11B, 53 = MS 125K/11B),
probes each request ID with `10 03`, and optionally reads VIN/part number
(`22 F1 90` / `22 F1 87`). Both presets were confirmed against the adapter:

```
STP 33 -> OK   STPRS -> HS CAN (ISO 15765, 500K/11B)
STP 53 -> OK   STPRS -> MS CAN (ISO 15765, 125K/11B)
```

With no car attached the scan finds nothing; with the ignition on (and the SGW
bypass for modules behind it) it populates the module map. Next: **P2**
(`dtc-all`, reading `0x19` from every responder).
