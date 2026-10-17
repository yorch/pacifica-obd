# Technical reference

How the toolkit is put together, which parts of the protocol stack it owns, and
which vehicle facts are baked in. If you plan to change the protocol layer, read
this first — several of the behaviours below are deliberate and shouldn't be
"simplified" away.

## Layers at a glance

```text
front ends        cli.py (argparse + Rich)      tui.py (Textual)
                  └─ render, dispatch, confirm prompts, --json shaping
                        │
domain logic      uds.py      models.py     profiles.py     database.py
                  └─ ISO-TP reassembly, DTC decode, DID reads, typed models,
                     vehicle profiles + the addressing rule, and optional
                     read-only access to an extracted AlfaOBD database
                        │
device layer      adapter.py    elm.py
                  └─ port discovery, bus presets, protocol snapshot/restore,
                     raw ELM327/STN command channel
                        │
persistence       store.py  (SQLite + JSONL, local only)
                        │
hardware          vLinker FS (FTDI FT230X → STN1170-class) → CAN-C / CAN-IHS
```

Two libraries sit underneath: **python-OBD** (`obd`) for standardized generic
OBD-II work, and a hand-rolled raw ELM327/STN transport for everything
manufacturer-specific.

## Module responsibilities

| Module | Owns | Must not do |
|---|---|---|
| `cli.py` | argparse subcommands, Rich rendering, `--json` shaping, confirmation prompts | Talk to the bus directly; re-implement ISO-TP or DTC decoding |
| `adapter.py` | Port discovery, bus selection, protocol snapshot/restore, typed `AdapterError` | Print, call `sys.exit`, or persist adapter EEPROM |
| `uds.py` | ISO-TP frame reassembly, UDS payload validation, DTC decoding, DID reads | Any I/O or printing — it is pure logic, testable with synthetic frames |
| `models.py` | Typed domain objects (`Vehicle`, `Module`, `Dtc`, `Reading`, `CaptureResult`) | Know about argparse or SQL |
| `profiles.py` | Data-driven `VehicleProfile` (years, powertrain, module buses, identification DIDs) and the addressing rule in `response_id()` | Assume one vehicle; hardcode what belongs in data |
| `elm.py` | The raw AT/ST command channel (`Elm327`) | Interpret UDS semantics |
| `database.py` | Optional **read-only** view of an extracted AlfaOBD SQLite database (FCA module/DID data); a no-op when absent | Write to it, or be required for the core tool to work |
| `store.py` | SQLite schema + JSONL log, local to the user | Crash a command: any failure degrades to a no-op |
| `tui.py` | Textual UI, worker threads, single-adapter locking | Block the event loop or own protocol logic |

The device layer raises `AdapterError` rather than exiting, so every front end
decides how to report and exit. Configuration commands are checked with
`must_ok()` and fail loudly on a `?` from the adapter.

## Protocol notes: ISO-TP and UDS

The 2021 Pacifica's diagnostics are **classic CAN** (ISO 11898) with **ISO-TP**
(ISO 15765-2) framing and **UDS** (ISO 14229) services on top. The adapter does
the framing when auto-format is on (`ATCAF1`, the default for UDS commands), but
the code still validates what comes back:

| Rule | Why it matters |
|---|---|
| A **first frame is a full 8-byte classic-CAN frame** | Padding is not optional; the length lives in the low nibble. |
| **Single-frame length is 1–7** | A "single frame" claiming 8+ bytes is malformed, not a short message. |
| **Consecutive frames must be sequence-checked** | A dropped or reordered frame must fail the read, not silently splice two ECUs' data. |
| **Frames are ≤ 8 bytes** | Classic CAN only; the STN1170 cannot reach a CAN-FD-only module. |
| **Responses require spaces** (`ATS1` on init) | `7E8 06 50 03` splits unambiguously into header and payload; the parser also accepts compact 11-bit lines, but spacing stays on. |

Frames are grouped by the CAN header that carried them (`collect_frames`), then
each group is reassembled into one or more complete UDS payloads
(`messages_from_frames`) and validated against the expected service before
anything is decoded. DTC payloads are classified first (`ok`, `negative`,
`malformed`, `unexpected`) so a negative response `7F 19 …` is never mistaken for
a code list.

Services used today are **read** services — `10 03` (session control, to open an
extended session), `19 02` (report DTCs by status mask), `22` (read data by
identifier) — plus `09 02` / `01` / `02` / `03` / `07` / `0A` through python-OBD
for generic OBD-II. Three write/security services are implemented as explicitly
guarded escape hatches, and are only reachable through the commands that demand
`--confirm`: `27` (security access — a seed request only, unless a seed→key
provider is configured), `2E` (write data by identifier) and `31` (routine
control).

## Addressing: `+8` versus `−0x280`

This is the fact that most naive FCA scanners get wrong, so it is enforced rather
than assumed: **use the response ID discovery captured, never derive one.**

| Request range | Response rule | Examples |
|---|---|---|
| `0x7E0`–`0x7E7` | **`request + 8`** — legislated by ISO 15765-4 | `7E0` → `7E8` (PCM), `7E1` → `7E9` (TCM), `7E2` → `7EA` (HCP) |
| Most other 11-bit modules | **`request − 0x280`** | `742` → `4C2` (IPC), `744` → `4C4` (ORC/SRS), `747` → `4C7` (ABS), `75A` → `4DA` (EPS) |

A scanner that assumes `+8` everywhere would look for the instrument cluster at
`0x74A`, get nothing, and under-report the vehicle. `modules` therefore records
whatever ID actually answered, and `read-did` correlates replies to the module it
addressed. The rule lives in exactly one place — `profiles.response_id()`:
`+8` inside `0x7E0`–`0x7E7`, `−0x280` for everything else — and
`profiles.addresses()` supplies the known request/response pairs per bus (what
`module-map` prints). Addresses observed publicly are stable across 2017–2023 and
both powertrains, with known exceptions that belong in profile data — see
[Vehicle support](vehicle-support.md#33-diagnostic-addressing-the-biggest-surprise-and-the-strongest-argument-for-data).

The transmit region is also bounded: the tool scans `0x700`–`0x7FF` by default
(widen it with `--start`/`--end`), and it does not spray requests across the whole
CAN ID space.

## CAN buses and ST presets

| Bus key | Preset | Wiring | Speed | Carries |
|---|---|---|---|---|
| `can-c` | `STP 33` | DLC pins **6 / 14** | 500 kbit/s, 11-bit | Powertrain and chassis: PCM, TCM, ABS, ORC, EPS, IPC, … |
| `can-ihs` | `STP 53` | DLC pins **3 / 11** | 125 kbit/s, 11-bit | Body and comfort: BCM, HVAC, doors, seats, radio, … |

The STN1170-class adapter reaches both natively — no cross-adapter, no second
device. Before a UDS operation the device layer configures:

```text
STP <33|53>    select the bus preset
ATH1           show response headers
ATCAF1         ISO-TP auto-format on
ATAL           allow messages longer than 7 bytes
STPTO <ms>     per-request timeout
```

and on close it restores the protocol that was active when the port opened
(`STPRS` / `STPR` snapshot → `STP` / `ATTP` restore). **`ATSP` is never issued by
the tool** — it persists the protocol to adapter EEPROM; the TUI additionally
warns if you type a persistent `ATSP`/`ATPP` into its raw console. A snapshot
failure is non-fatal by design — it must not stop a diagnostic command.

Two exceptions to "configured, then restored":

- `raw` is an explicit escape hatch: it applies your flags for that one
  invocation (protocol via non-saving `ATTP`) and does **not** snapshot or
  restore the previous state.
- A `--protocol` / `ATSH` you pass to `raw` therefore outlives the process only
  if the adapter itself persists it — which is why `ATSP` is not used even there.

## Storage model

Two local artifacts, written for every non-trivial run:

| File | Contents |
|---|---|
| `pacifica.db` (SQLite) | `vehicles`, `sessions`, `events`, `modules`, `dtcs`, `readings`, `samples` |
| `events.jsonl` (append-only) | One JSON object per interaction — every command, module probe, DTC read and DID read |

| Table | Holds |
|---|---|
| `vehicles` | VIN-keyed record: make, model, year, profile id, powertrain, first/last seen |
| `sessions` | One row per invocation: command, arguments, port, VIN, status, timestamps |
| `events` | Append-only interaction log (probe, dtc-read, …) with a JSON detail blob |
| `modules` | Discovery results: bus, request/response ID, kind, VIN read, part number |
| `dtcs` | Decoded codes with module, bus, failure type, status flags and raw payload |
| `readings` | DID values: hex + ASCII, status, raw payload, category (`did`, `module-info`) |
| `samples` | `monitor` time series: numeric value where the DID is short enough to be one |

Location is resolved in this order:

```text
$PACIFICA_OBD_DB / $PACIFICA_OBD_LOG      explicit file paths
$PACIFICA_OBD_HOME                        directory for both files
$XDG_DATA_HOME/pacifica-obd               Linux convention
~/.local/share/pacifica-obd               default on macOS
```

`--db PATH` and `--no-log` (or `PACIFICA_OBD_NO_LOG=1`) override the environment
for a single run. Two properties are intentional:

1. **Persistence never breaks a command.** If the database cannot be opened,
   `Store.available` becomes `False` and the run continues.
2. **History reads never write.** `history`, `show` and `vehicles` open the
   database read-only, so inspecting history cannot create or mutate files.

Captured vehicle data stays on the machine that produced it. Real VINs and
captures are deliberately not committed to the repository.

## Safety: SGW and read-only discipline

- **The Security Gateway (SGW)** sits between the OBD-II port and the vehicle's
  internal networks on 2018+ vehicles. **Reads pass; writes are blocked** without
  a 12+8 bypass cable. `dtc --clear` sends the standard mode-04 request and may
  simply be refused — that is the vehicle, not the tool.
- **Read-only by default; writes are opt-in and guarded.** The write paths are
  `dtc --clear` (mode `04`, behind a typed confirmation or `--force`) and the
  experimental primitives `write-did` (`2E`) and `routine` (`31`), which refuse to
  run without `--confirm` and print a warning that they are not reversible by this
  tool. `security` (`27`) only requests a seed: no seed→key provider is shipped, so
  the ECU is left locked. Adaptations, coding and module programming remain out of
  scope — that is the domain of AlfaOBD / Appcar DiagFCA / JScan / dealer wiTECH.
- **No adapter EEPROM writes** (see buses, above), and the protocol is always
  restored before the port closes — including on the interactive `tui` exit path.
- **One owner of the adapter at a time.** The CLI is synchronous; the TUI holds a
  lock, so two conversations can never interleave on the same CAN bus.
- **Addressing is bounded.** Scans transmit only within the configured range; a
  module whose address is unknown is probed, never guessed.
- Read/clear operations are decided by the user. Codes are evidence: clearing
  them also discards freeze-frame data and resets readiness monitors.

## Vehicle profiles

`profiles.py` describes *which* modules live on *which* bus and which
identification DIDs to read, selected by model year, powertrain and (optionally)
the VIN — `RU_GAS` / `RU_PHEV` style profiles rather than conditionals scattered
through the code. `uds.BUS_PROTOCOL`, `uds.DID_NAMES` and `uds.INFO_DIDS` hold the
portable defaults that a profile can override.

The design intent, and the gaps in the current shape, are analysed in
[Vehicle support (RU family)](vehicle-support.md) — including the `−0x280`
addressing rule, per-module confidence levels, and why module presence and module
address are separate facts.

## Testing without a vehicle

The adapter answers AT commands with no car attached; bus requests return
`CAN ERROR` / `NO DATA`. That is enough to test everything except the vehicle
half:

```sh
export PACIFICA_OBD_HOME=$(mktemp -d)          # never touch real data
uv run pacifica-obd raw ATI                     # adapter identity
uv run pacifica-obd modules --start 7E0 --end 7E1 --timeout-ms 40 --json
uv run pacifica-obd --json dtc | jq .           # stdout stays valid JSON
```

Protocol and parsing logic is exercised with **synthetic frames** — build spaced
ELM response lines (single-frame, multi-frame, bad sequence numbers, negative
responses, malformed lengths) and feed them through `uds.parse_uds_frame` /
`messages_from_frames`. Nothing in `uds.py` needs a serial port.

## Documentation site

This site is built from the Markdown in `docs/` with **MkDocs Material**; the
Markdown is the source of truth and the deep-dive documents are served as-is.

```sh
# strict = any warning (broken link, missing nav target, bad anchor) fails the build
uv run --with-requirements requirements-docs.txt mkdocs build --strict --site-dir build/docs
uv run --with-requirements requirements-docs.txt mkdocs serve
```

`--site-dir build/docs` keeps the output inside the already-ignored `build/`
directory, so a local build never litters the working tree (the default is
`site/`, which is not ignored). `.github/workflows/pages.yml` runs the same strict
build on every push to `main` and deploys `site/` to GitHub Pages with
`actions/deploy-pages`; in CI that directory is an ephemeral artifact and is never
committed.
