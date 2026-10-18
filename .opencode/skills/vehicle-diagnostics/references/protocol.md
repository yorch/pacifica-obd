# Protocol reference (ELM327 / STN · ISO-TP · UDS · FCA addressing)

Everything here is the difference between "the car is nearly dead" and "the car
is fine, the tool was misconfigured." Verified on a 2021 Pacifica Hybrid with a
vLinker FS (STN1170).

## Adapter layer

- The vLinker FS is an **STN1170**; `STI` gives the real chip + firmware
  (`STN1170 v4.3.2`). `ATI` returns the **compatibility** string (`ELM327 v2.3`)
  and is not the chip identity.
- Open the port with `ATZ` (reset), `ATE0` (echo off), `ATL0` (no linefeeds),
  and **`ATS1` (spaces ON)** — response parsing relies on the header/data
  separation.
- **Never write adapter EEPROM.** ELM's `ATSP` saves the protocol; use `ATTP`
  (try protocol, non-saving) or the ST `STP` preset instead. Snapshot the
  protocol with `STPR`/`STPRS` and restore it (auto → `ATTP0`; AT namespace →
  `ATTP n`; ST preset → `STP n`).
- One process owns the port at a time. Two holders (another scan tool, a
  `dotnet`/`python` app) cause failures that look like vehicle faults. Check with
  `lsof /dev/cu.usbserial-*`.

## Bus presets (STN `STP`)

| Preset | Meaning | Pins |
|---|---|---|
| `STP 33` | ISO 15765 HS CAN, 11-bit, 500 k | 6 / 14 |
| `STP 53` | ISO 15765 MS CAN, 11-bit, 125 k | 3 / 11 |

The adapter has **one** CAN peripheral, so only one bus is active at a time —
switch with `STP`, don't assume both are live. Configure framing with `ATH1`
(headers), `ATCAF1` (auto-format), `ATAL` (allow long), and `STPTO <ms>`
(request timeout).

## ISO-TP (ISO 15765-2)

- Single Frame: PCI nibble `0`, length 1–7.
- First Frame: PCI nibble `1`, 12-bit length; **must be a full 8-byte frame**.
- Consecutive Frames: nibble `2`, sequence `1,2,3,…`; reject dupes/reorders.
- Classic CAN carries ≤ 8 bytes per frame.

## Receive filter (ATCRA) — the silent killer

By default the adapter only accepts a reply whose address is `request + 8`.
Most modules answer **elsewhere**: `request − 0x280` on CAN-C chassis and on
CAN-IHS, and `+1` for the SGW. Their replies are **silently dropped**, so the
module looks like "no response". Before probing or reading a module, set the
receive address to the expected responder: **`ATCRA <resp_id>`**.

Derive the responder: `7E0`–`7E7` → `+8`; otherwise `request − 0x280` (covers
CAN-C chassis and CAN-IHS); SGW `47E → 47F`. Prefer the response ID you actually
discovered. A **stale filter hides every other module** (the "first module
works, the rest are silent" bug), so set it per module — or `ATAR` to clear it.

Verified: `742` without a filter → `NO DATA`; with `ATCRA 4C2` → answers.

## Flow control — and its limit

Multi-frame replies (e.g. the 20-byte VIN) need the tester to send an ISO-TP
**Flow Control** frame. The adapter's **automatic** flow control completes these
**only for the `+8` convention** (`7E0`–`7E7`). For `−0x280` and CAN-IHS pairs a
multi-frame reply stalls after the first frame. So:

- **discover with a single-frame probe** — `22F100` is 4 bytes and works for
  every module; `10 03` also works on many. `22F190` is multi-frame and will
  miss the `−0x280` set.
- expect long DIDs (`F190`, `F187`) on `−0x280`/CAN-IHS modules to be truncated
  or unavailable; short ones (`F100`) read fine.
- Explicit `ATFCSM1` + `ATFCSH` + `ATFCSD` did **not** fix it here and *broke
  parsing* (the adapter switched to an indexed `0:`/`1:` frame format). Reach for
  `ATCRA` first, not FC.

## Addressing rules (validated on hardware)

A request ID maps to its response by rule, but **prefer the response ID you
actually discovered**:

| Request | Response | Rule | Notes |
|---|---|---|---|
| `7E0`–`7E7` | `+8` | OBD-II block | PCM `7E0→7E8`, TCM `7E1→7E9`… |
| `7xx` chassis | `−0x280` | PowerNet | IPC `742→4C2`, RFH `740→4C0`, ESM `749→4C9`, SCCM `763→4E3`… |
| `47E` | `47F` | `+1` | Security Gateway (readable) |
| CAN-IHS `78x` | `50x` | `+?` | `783→503 … 788→508` |

`0x7DF` is a **functional broadcast** — a response from it is not a real module.

## Discovery

- Probe **more than one service**. Many modules **reject `10 03` (extended
  session)** but answer reads like `22 F190`. Probe at least `1003` and `22F190`.
- Probe **both buses**; a CAN-C-only scan misses all CAN-IHS modules.
- Record each responder's request/response ID and which probe answered.

## UDS services cheat-sheet

| SID | Service | Use |
|---|---|---|
| `0x10` | DiagnosticSessionControl (`03`=extended) | some reads need it |
| `0x11` | ECUReset | **avoid** unless asked |
| `0x14` | ClearDiagnosticInformation | **destructive — needs consent** |
| `0x19 02` | ReadDTCInformation (by status mask `FF`) | per-module DTCs |
| `0x22` | ReadDataByIdentifier (`F190`=VIN, `F187` part #, `F188/F189` SW) | identification |
| `0x27` | SecurityAccess (seed/key) | unlocks writes; risky |
| `0x2E` | WriteDataByIdentifier | **write — needs consent** |
| `0x31` | RoutineControl | activations/service functions — risky |
| `0x3E` | TesterPresent | keep a session alive during long polls |

**DTC decode:** `0x59 0x02 <statusAvailabilityMask> (<3-byteDTC><status>)*`.
Category bits of byte0: `00=P 01=C 10=B 11=U`; number = `((b0&0x3F)<<8)|b1`
(3rd byte = failure type). A status byte of only `0x40` (`testNotCompleted`)
is **not a confirmed fault**.

## VIN

`09 02` is **not served by every vehicle** (the Pacifica Hybrid is one). Read
UDS `22 F190` instead — it worked on 25 modules. VIN character 10 encodes the
model year (`M`=2021, `N`=2022, `P`=2023, `R`=2024, `S`=2025).

## Security Gateway (SGW)

Present from MY2018. It is a firewall between the OBD port and the network:
**reads pass, writes are blocked** without a hardware bypass. Its address is
`47E→47F`.

## Powertrain detection

The VIN does not distinguish gas from hybrid. If a hybrid-only module answers —
**HCP `0x7E2`** or **BPCM `0x7E7`** — it is a **PHEV**.
