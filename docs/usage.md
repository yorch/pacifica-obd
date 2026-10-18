# Command reference

```text
uv run pacifica-obd [GLOBAL FLAGS] <command> [COMMAND FLAGS]
```

Every command accepts the global flags below, and every command that reads data
supports `--json` — before **or** after the subcommand, both work. Configuration
commands (`ports`, `history`, `show`, `vehicles`, `tui`) take no adapter flags.

## Global flags

| Flag | Default | Meaning |
|---|---|---|
| `--port PATH` | auto-detect | Serial device, e.g. `--port /dev/cu.usbserial-1420`. Auto-detect prefers `usbserial` / `vlinker` / `ftdi` names. |
| `--baud N` | `115200` | Host UART baud rate (the vLinker FS runs at 115.2 kbaud). |
| `--timeout F` | `1.0` | Per-query timeout in seconds. |
| `--check-voltage` | off | Verify battery voltage during python-OBD init. Leave **off** when bench-testing. |
| `--json` | off | Machine-readable output. |
| `--db PATH` | data dir | SQLite database to write/read (`PACIFICA_OBD_DB`). |
| `--no-log` | off | Do not persist this run (`PACIFICA_OBD_NO_LOG=1`). |

### Machine-readable output

`--json` keeps **stdout** clean: data goes to stdout, status messages go to
**stderr**.

```sh
uv run pacifica-obd --json dtc        # same as: dtc --json
uv run pacifica-obd scan --json | jq .vin
uv run pacifica-obd dtc-all --bus both --json | jq '.modules[] | select(.dtcs | length > 0)'
```

Streaming commands emit **newline-delimited JSON** (one object per sample), so
they pipe cleanly into `jq`, `tee`, or a file:

```sh
uv run pacifica-obd watch --pids RPM,SPEED --json | tee drive.jsonl
uv run pacifica-obd monitor --module 7E0 --did F190 --json | jq -c .values
```

`--csv PATH` (on `watch`) writes a header row plus one row per sample, into a new
file.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (an empty result is still success). |
| `1` | Nothing to read: no local database yet (`history`, `show`, `vehicles`), or no module answered (`monitor`). |
| `2` | Adapter/usage failure: no serial port, port busy, adapter rejected a command, bad arguments, or a guarded write attempted without `--confirm`. |
| `3` | `dump` started but the adapter failed mid-scan. The files it had collected are still written and marked `aborted` (JSON field) / "Partial capture" (HTML banner). |

### Partial captures

`dump` collects the generic OBD‑II pass and the UDS pass separately and commits
results as it goes. If the adapter drops mid‑scan (USB dropout, another app
grabbing the port), the command:

1. keeps everything already read (the module it was on included, with whatever
   DTCs/DIDs it had returned),
2. sets `"aborted"` in the JSON, shows a **Partial capture** banner in the HTML —
   and the field is `null` when the scan completed,
3. writes the files anyway, then exits `3`.

So a dropout no longer discards a multi‑minute scan, and scripts can tell a
partial result (`3`) from a complete one (`0`).

## Command map

| Command | Bus | What it does |
|---|---|---|
| [`ports`](#ports) | — | List candidate serial ports. |
| [`info`](#info) | OBD-II | Adapter identity, status, protocol, voltage, VIN. |
| [`vin`](#vin) | OBD-II | Read the VIN (mode `09 02`). |
| [`scan`](#scan) | OBD-II | One-shot: VIN + stored DTCs + supported PID count. |
| [`dtc`](#dtc) | OBD-II | Stored / pending / permanent codes, optionally clear. |
| [`live`](#live) | OBD-II | Current value of every supported PID. |
| [`watch`](#watch) | OBD-II | Live PID dashboard, CSV or JSONL logging. |
| [`readiness`](#readiness) | OBD-II | Readiness / I-M monitors (pre-emissions-test). |
| [`freeze`](#freeze) | OBD-II | Freeze-frame snapshot behind a DTC (mode `02`). |
| [`modules`](#modules) | UDS | Discover responding modules on a bus. |
| [`module-map`](#module-map) | — | Known module addresses (built-in, plus an optional external ECU database if one is given). |
| [`dtc-all`](#dtc-all) | UDS | Read DTCs (`19 02`) from every responding module. |
| [`read-did`](#read-did) | UDS | Read DIDs (`22`) from one module. |
| [`module-info`](#module-info) | UDS | Identification block (`F186`–`F191`) per module. |
| [`capture`](#capture) | UDS | Full snapshot (modules + DTCs + DIDs), stored locally. |
| [`monitor`](#monitor) | UDS | Poll UDS DIDs on modules and store every sample. |
| [`security`](#security) | UDS | ⚠️ Request a `0x27` security seed from a module. |
| [`write-did`](#write-did) | UDS | ⚠️ Write a DID (`2E`). Guarded, experimental. |
| [`routine`](#routine) | UDS | ⚠️ Run a routine (`31`). Guarded, experimental. |
| [`raw`](#raw) | raw | Send arbitrary AT/ST/OBD/UDS commands to the adapter. |
| [`history`](#history) | — | Stored vehicles and recent sessions. |
| [`show`](#show) | — | Everything stored for one VIN. |
| [`vehicles`](#vehicles) | — | Vehicles with stored records. |
| [`tui`](#tui) | both | Interactive Textual UI over the same helpers. |
| [`web`](#web) | both | Local web UI (read-only; optional `[web]` extra). |

## ports

List serial ports python-OBD can see, newest first.

```sh
uv run pacifica-obd ports
uv run pacifica-obd ports --json
```

## info

Open the adapter and report port, connection status, protocol name, adapter
version (`ATI`), battery voltage (`ATRV`) and the VIN.

```sh
uv run pacifica-obd info
uv run pacifica-obd info --json
```

With the ignition off you will see a live adapter and an empty protocol — that is
the normal "adapter up, car asleep" state, not an error.

## vin

Read the VIN through generic OBD-II (mode `09`, PID `02`). The VIN is also how
the tool knows which vehicle a stored capture belongs to.

```sh
uv run pacifica-obd vin
uv run pacifica-obd vin --json
```

If the PCM refuses `09 02`, try the UDS route:
`read-did --module 7E0 F190 --json`.

## scan

One shot: connection status, protocol, VIN, stored DTCs, and how many PIDs the
vehicle reports as supported. This is the fastest "is it working?" check, and it
stores the VIN when persistence is enabled.

```sh
uv run pacifica-obd scan
uv run pacifica-obd scan --json
```

## dtc

Read stored (mode `03`), pending (mode `07`) and — when the installed
python-OBD exposes it — permanent (mode `0A`) trouble codes for the PCM/TCM.
python-OBD 0.7.3 has no permanent-DTC command, so on that version the mode `0A`
table always reads "none"; stored and pending are always read.

```sh
uv run pacifica-obd dtc
uv run pacifica-obd dtc --json
```

Clearing is a **write** and needs an explicit decision:

```sh
uv run pacifica-obd dtc --clear              # asks you to type 'yes'
uv run pacifica-obd dtc --clear --force      # no prompt
uv run pacifica-obd dtc --clear --json --force   # JSON mode has no prompt
```

| Flag | Meaning |
|---|---|
| `--clear` | Clear codes after reading. Erases freeze-frame data and resets readiness monitors. |
| `--force` | Skip the confirmation prompt (required for `--clear` together with `--json`). |

!!! warning "The SGW usually blocks this on a 2021"

    Clearing is a write, and the Security Gateway gates writes on 2018+ vehicles
    without a 12+8 bypass cable. See
    [Chrysler Security Gateway](chrysler-sgw.md#read-vs-write).

## live

Query every PID the vehicle reports as supported and print the current value —
one sweep, no polling. Useful to see what this van actually exposes.

```sh
uv run pacifica-obd live
uv run pacifica-obd live --json | jq '.pids[] | select(.value != null)'
```

## watch

A live dashboard, refreshed twice a second, until you press ++ctrl+c++.

```sh
uv run pacifica-obd watch                                    # default PID set
uv run pacifica-obd watch --pids RPM,SPEED,COOLANT_TEMP
uv run pacifica-obd watch --interval 0.25
uv run pacifica-obd watch --csv drive.csv                    # log every sample
uv run pacifica-obd watch --pids RPM,SPEED --json | tee drive.jsonl
```

| Flag | Default | Meaning |
|---|---|---|
| `--pids A,B,C` | see below | Comma-separated python-OBD PID names. |
| `--interval F` | `0.5` | Seconds between polls. |
| `--csv PATH` | — | Write every sample to a CSV file. The file is **created fresh** (truncated if it exists), so use a new name per capture. |

The default set is `RPM`, `SPEED`, `COOLANT_TEMP`, `ENGINE_LOAD`,
`THROTTLE_POS`, `INTAKE_TEMP`, `MAF`, `FUEL_LEVEL`, `ELM_VOLTAGE`. In JSON mode
each line looks like:

```json
{"timestamp": "2026-10-17T18:04:11.512Z",
 "values": {"RPM": {"value": 812.5, "unit": "revolutions_per_minute"}, "SPEED": 0}}
```

## readiness

Readiness / I-M monitors (mode `01`, PID `01`) — the "will it pass an emissions
test?" view. Reports ignition type, MIL state, stored DTC count, and each
monitor as complete or incomplete.

```sh
uv run pacifica-obd readiness
uv run pacifica-obd readiness --json
```

## freeze

The freeze-frame snapshot (mode `02`, frame 0) captured when an emissions fault
set: engine load, coolant, RPM, speed, timing, fuel trims, MAF, intake temp and
friends. A snapshot only exists **because** a fault was stored — "no
freeze-frame data stored" is the normal answer on a clean van.

```sh
uv run pacifica-obd freeze
uv run pacifica-obd freeze --json
```

## modules

Probe an address range on a CAN bus and report every module that answers UDS
session control. Because a responding module may answer from an unexpected ID,
discovery records the **actual** request/response pair rather than assuming one.

```sh
uv run pacifica-obd modules                                  # CAN-C, 700–7FF
uv run pacifica-obd modules --bus can-ihs
uv run pacifica-obd modules --start 7E0 --end 7E1 --timeout-ms 40 --json
uv run pacifica-obd modules --bus can-c --identify            # + VIN and part number
```

| Flag | Default | Meaning |
|---|---|---|
| `--bus can-c\|can-ihs` | `can-c` | `can-c` = HS 500 kbit/s (pins 6/14); `can-ihs` = MS 125 kbit/s (pins 3/11). |
| `--start ID` / `--end ID` | `700` / `7FF` | Inclusive 11-bit request-ID range (hex). |
| `--session HEX` | `1003` | Probe request — the extended-session request by default. |
| `--identify` | off | After a hit, also read `22 F190` (VIN) and `22 F187` (part number). |
| `--timeout-ms N` | `100` | Per-request adapter timeout (`STPTO`). |
| `--vin VIN` | — | Attach the stored result to this VIN. |

## module-map

Show the module addresses this tool already knows, for both buses, without
touching the vehicle — useful before a scan to know what to expect.

```sh
uv run pacifica-obd module-map
uv run pacifica-obd module-map --json
uv run pacifica-obd module-map --ecu-db ~/ecu-db.sqlite
```

| Flag | Meaning |
|---|---|
| `--ecu-db PATH` | Optional path to an external ECU SQLite database (`PACIFICA_OBD_ECU_DB`). It is opened **read-only** and only used to print the full FCA PowerNet ECU map. |

Without the database, the built-in profile addresses are listed and a note says
so. The database is opt-in: the tool ships no default and never requires one.

## dtc-all

Discover modules, then read `19 02` from each one — the all-module code read that
generic OBD-II cannot do. Every target is reported, including the ones that
answered with a negative response, so a gap is visible rather than implied.

```sh
uv run pacifica-obd dtc-all
uv run pacifica-obd dtc-all --bus both --json
uv run pacifica-obd dtc-all --module 7E0 --timeout-ms 40
uv run pacifica-obd dtc-all --bus both --json | jq '.total_dtcs'
```

| Flag | Default | Meaning |
|---|---|---|
| `--bus can-c\|can-ihs\|both` | `can-c` | One bus, or both in sequence. |
| `--start ID` / `--end ID` | `700` / `7FF` | Address range scanned when `--module` is omitted. |
| `--module ID` | — | Query one request ID instead of scanning (e.g. `7E0`). |
| `--timeout-ms N` | `100` | Per-request adapter timeout. |
| `--vin VIN` | — | Attach stored DTCs to this VIN. |

Per-module `status` is one of `ok`, `negative`, `malformed`, `unexpected` or
`no_response`; `incomplete: true` in the JSON means at least one target did not
answer cleanly.

## read-did

Read one or more Data Identifiers (`22`) from a single module. The CLI enters an
extended session first and correlates replies against the responding ECU, so a
stray frame from another module cannot be mistaken for an answer.

```sh
uv run pacifica-obd read-did --module 7E0 F190                     # VIN
uv run pacifica-obd read-did --module 7E0 F187 F188 F191           # part / SW / HW
uv run pacifica-obd read-did --module 747 F190 --bus can-c --json
uv run pacifica-obd read-did --module 744 F186 F187 F188 F189 F18A F18C F190 F191
```

| Flag | Default | Meaning |
|---|---|---|
| `--module ID` | **required** | Request ID of the target module (e.g. `7E0`). |
| DIDs (`F190`, …) | **required** | One or more 4-hex Data Identifiers. |
| `--bus can-c\|can-ihs` | `can-c` | Bus to address the module on. |
| `--timeout-ms N` | `100` | Per-request adapter timeout. |
| `--vin VIN` | — | Attach stored readings to this VIN. |

Common DIDs: `F186` active diagnostic session, `F187` spare part number, `F188`
ECU software number, `F189` software version, `F18A` system supplier identifier,
`F18C` ECU serial number, `F190` VIN, `F191` ECU hardware number, `F194`
supplier ECU software number, `F132` FCA identification (non-standard, observed
on these ECUs).

## module-info

The identification block (`F186`–`F191`) for one module, or for every module that
answers a scan.

```sh
uv run pacifica-obd module-info --module 7E0
uv run pacifica-obd module-info --bus both
uv run pacifica-obd module-info --bus both --json
```

Without `--module`, the address range is scanned first (`--start`, `--end`,
`--timeout-ms` as above) and each responder is read in turn.

## capture

The full read-only sweep: discover modules, read each one's DTCs and
identification block, and persist everything under one session and VIN.

```sh
uv run pacifica-obd capture --bus both
uv run pacifica-obd capture --bus both --json | jq '{vin, modules: (.modules|length), dtcs: (.dtcs|length)}'
```

The JSON snapshot has `vin`, `buses`, `modules` (request/response/kind/VIN/part),
`dtcs` and `readings`. Without `--json` it prints a one-line summary and the
database path it wrote to.

## monitor

Poll UDS DIDs on one module or across a scan, on an interval, storing every
sample. Like `watch`, but for manufacturer-specific data rather than OBD-II PIDs.

```sh
uv run pacifica-obd monitor --module 7E0 --did F190
uv run pacifica-obd monitor --module 7E0 --did F187 --did F188 --interval 30
uv run pacifica-obd monitor --module 747 --did F190 --json | jq -c .values
```

| Flag | Default | Meaning |
|---|---|---|
| `--did HEX` | **required** | One or more DIDs to poll; repeat the flag to group them. |
| `--module ID` | — | One request ID; omit to scan `--start`–`--end` instead. |
| `--interval F` | `1.0` | Seconds between polls. |
| `--bus`, `--start`, `--end`, `--timeout-ms`, `--vin` | as above | Bus, range, timeout, VIN attribution. |

Each sample is stored in the `samples` table; short (≤ 4 hex digit) values are
also stored as a number, so `monitor` doubles as a poor-man's data logger.

## Guarded write path

!!! danger "These commands send write or security services"

    `security`, `write-did` and `routine` exist as **experimental escape hatches**,
    not as features. `write-did` and `routine` refuse to run without `--confirm`,
    print a write warning, and are **not reversible by this tool**. On a 2021
    Pacifica the **SGW will usually refuse them** unless a 12+8 bypass cable is
    fitted — and with a bypass fitted, a mistake is *more* likely to be accepted
    by the vehicle. Change one thing at a time, and record the original value
    first.

## security

Request a UDS `0x27` security seed from a module and, for the modules this
project ships, compute the FCA/Stellantis PowerNet key **without an external
provider file** (see [`fca-keys.md`](fca-keys.md)). The default is a **dry run**:
it reads the seed and reports the algorithm it *would* use — it never sends a
key. Sending a key needs `--unlock --confirm`.

```sh
uv run pacifica-obd security --module 742 --device-id 11872 --level 1 --explain  # offline
uv run pacifica-obd security --module 742 --device-id 11872 --level 1            # dry run
uv run pacifica-obd security --module 742 --device-id 11872 --level 1 --unlock --confirm
uv run pacifica-obd security --module 7E0 --json
```

| Flag | Default | Meaning |
|---|---|---|
| `--module ID` | **required** | Request ID of the target module. |
| `--level N` | `1` | Security level (odd = request seed). |
| `--provider FILE` | — | Custom `key_for(seed, **ctx)`; overrides the built-in selector. |
| `--ecu NAME` | from `--module` | ECU name for the built-in selector. |
| `--device-id N`, `--variant N`, `--supplier CODE` | — | Discriminators for the built-in selector. |
| `--explain` | off | Print the chosen algorithm + ids and exit (no adapter). |
| `--unlock` | off | Also send the key. Needs `--confirm`. |
| `--confirm` | off | **Required** with `--unlock`. |
| `--bus`, `--timeout-ms` | `can-c`, `100` | Bus and per-request timeout. |

The built-in selector picks exactly one algorithm or it **refuses**: an
ambiguous or unmapped module exits `2` and never sends a key (a wrong key can
lock a module). It uses a module's device id automatically only when the dataset
pins a single one; otherwise pass `--device-id`/`--variant` (IPC `11872` is the
MY2020+ PowerNet unit, `11871` the MY2017 one).

`status` is one of `ok` (seed returned), `already_unlocked` (an all-zero seed),
`no_key_provider` (seed returned, no key sent — the dry run), `unlocked`,
`rejected`, `negative`, `unexpected` or `no_response`. With `--json` the result
also carries `algorithm` and `ids` when the built-in selector resolved one.

## write-did

Write one Data Identifier (UDS `2E`) on a module. Requires an explicit
acknowledgement.

```sh
uv run pacifica-obd write-did --module 7E0 --did F190 --data 01 --confirm --json
```

| Flag | Default | Meaning |
|---|---|---|
| `--module ID` | **required** | Request ID of the target module. |
| `--did HEX` | **required** | 4-hex Data Identifier to write. |
| `--data HEX` | **required** | Even-length hex payload. |
| `--confirm` | off | **Required.** Without it the command exits `2`. |
| `--bus`, `--timeout-ms` | `can-c`, `100` | Bus and per-request timeout. |

## routine

Run a UDS `31` routine — activations, calibrations, self-tests. Also guarded.

```sh
uv run pacifica-obd routine --module 7E0 --rid 0203 --sub 1 --confirm
uv run pacifica-obd routine --module 7E0 --rid 0203 --sub 3 --confirm --json
```

| Flag | Default | Meaning |
|---|---|---|
| `--module ID` | **required** | Request ID of the target module. |
| `--rid HEX` | **required** | Routine identifier (4 hex). |
| `--sub N` | `1` | `1` start, `2` stop, `3` request results. |
| `--data HEX` | — | Optional even-length hex payload. |
| `--confirm` | off | **Required.** Without it the command exits `2`. |
| `--bus`, `--timeout-ms` | `can-c`, `100` | Bus and per-request timeout. |

## raw

Send arbitrary commands to the adapter, in order, printing each response. This is
the layer below python-OBD — the one the UDS commands are built on.

```sh
uv run pacifica-obd raw ATI STI
uv run pacifica-obd raw ATRV STPRS
uv run pacifica-obd raw 22F190 --header 7E0 --protocol 6 --json
uv run pacifica-obd raw 1003 --header 7E0 --protocol 6 --no-auto-format --json
uv run pacifica-obd raw "ATSH 7E0" 22F187 --allow-long --headers
```

Each positional argument is sent as its **own** command, so `ATSH 7E0` must be
quoted (or use `--header 7E0`).

| Flag | Meaning |
|---|---|
| `--header HDR` | Set the CAN header first (`ATSH`, e.g. `7E0`). |
| `--protocol N` | Try a protocol first (`ATTP`, **non-saving**, e.g. `6` = CAN 11-bit/500k) — unlike `ATSP`, which would persist to adapter EEPROM. |
| `--headers` | Show response headers (`ATH1`). |
| `--no-auto-format` | `ATCAF0` — raw CAN, no ISO-TP framing. |
| `--no-flow-control` | `ATCFC0`. |
| `--allow-long` | `ATAL` — allow messages longer than 7 bytes. |

!!! danger "`raw` is unfiltered"

    Nothing stops you from sending a write service. Prefer read services
    (`19`, `22`) and remember that the SGW is there for a reason: on a stock port
    writes are refused, and with a bypass cable installed a mistake is *more*
    likely to be accepted by the vehicle, not less.

For a guarded alternative, the web UI's **Raw console** (`#/raw`) sends one
bounded command at a time and classifies it into read / confirm / deny tiers
before anything reaches the adapter (see [Web UI](web-ui.md)); the CLI `raw`
command above stays the expert escape hatch.

## history

Stored vehicles plus the most recent sessions (command, status, VIN).

```sh
uv run pacifica-obd history
uv run pacifica-obd history --limit 100 --json
```

## show

Everything stored for one VIN: DTC rows and DID readings, newest first.

```sh
uv run pacifica-obd show --vin <VIN>
uv run pacifica-obd show --vin <VIN> --json
```

## vehicles

Vehicles we have records for, with model year and profile fields when known.

```sh
uv run pacifica-obd vehicles
uv run pacifica-obd vehicles --json
```

## tui

An interactive Textual interface over the same helpers: live PIDs, readiness,
freeze frame, VIN, module discovery, all-module DTCs and DID reads. Operations
run on worker threads, and a lock guarantees a single owner of the adapter at a
time — a second request is refused with a visible message instead of interleaving
two conversations on one CAN bus.

```sh
uv run pacifica-obd tui
```

Press ++ctrl+c++ (or quit from the UI) and the previous adapter protocol is
restored before the port closes.

## web

A local web UI served from the same source tree. It is an **optional extra**:
the CLI and TUI keep working without it. The default is **read-only**; the
guarded write surface (`0x27` unlock, `2E` write, `31` routine) and the
bring-your-own-key AI layer are opt-in.

```sh
uv sync --extra web                     # installs fastapi + uvicorn + httpx
uv run pacifica-obd web                 # http://127.0.0.1:8765 (read-only)
uv run pacifica-obd web --port 9000
uv run pacifica-obd web --demo          # synthetic adapter + a seeded database
uv run pacifica-obd web --token         # requires a bearer token; prints a #t=… URL
uv run pacifica-obd web --open          # open a browser
uv run pacifica-obd web --allow-writes  # enable the guarded write surface
uv run --extra web pacifica-obd-web     # same server, standalone entry point
```

| Flag | Default | Meaning |
|---|---|---|
| `--host HOST` | `127.0.0.1` | Bind address. Never `0.0.0.0` without `--allow-remote`. |
| `--port N` | `8765` | TCP port. |
| `--token [TOKEN]` | off | Require `Authorization: Bearer …`. A bare flag generates one and prints a URL with the token in the **fragment** (never sent to the server or written to a log). |
| `--demo` | off | Synthetic adapter + a seeded `pacifica.db` so the UI is explorable with no car. |
| `--read-only` | **on** | The default. Removes every write route from the app. |
| `--allow-writes` | off | Registers the guarded write surface: `0x27` unlock, `2E` write, `31` routine. Each still needs a typed confirmation and is audited. |
| `--open` | off | Open the UI in a browser. |
| `--allow-remote` | off | Permit a non-loopback bind (requires `--token`). |

With the extra missing, the command prints an install hint and exits `1` instead
of raising an import error:

```text
uv sync --extra web
uv run --extra web pacifica-obd web
```

Screens: Dashboard (`#/`), Live data (`#/live`), Modules & DTCs (`#/modules`),
DIDs / module info (`#/dids`), Security 0x27 (`#/security`), Writes (`#/writes`),
Assistant (`#/assistant`), Capture (`#/capture`), Database (`#/db`), Reports
(`#/reports`), Raw console (`#/raw`), Settings (`#/settings`) and Help
(`#/help`). It reaches parity with the CLI's read commands and adds gauges, a
time-series chart with replay, a session comparison, CSV export and print-to-PDF
reports.

The **Raw console** (`#/raw`) is the one screen that sends an arbitrary command,
but it is **not unfiltered**: one bounded command per submit (hex bytes or a
single `AT…` command; a second smuggled command is refused) is classified into
three tiers before anything is sent — **read** (sent immediately: OBD modes
`01 03 07 09 0A`, UDS `10 19 22 23 24 2A 3E`, and a conservative AT allowlist),
**confirm** (write mode only, a single-use token plus a typed acknowledgement
naming the exact bytes) and **deny** (never sent: mode `04`, UDS
`27 2E 31 2F 3D 34 35 36 37 11 14 28 85`, and the reset/EEPROM/monitor AT
families). Every send is audited. The classifier lives in
`src/pacifica_obd/raw_guard.py` and the tier table is served to the UI by
`GET /api/raw/allowlist`. The CLI's `raw` command is unchanged and remains the
expert escape hatch.

The write surfaces are **structurally absent** unless `--allow-writes` is given:
the routes are not registered, and a request to them returns `403 readonly`. In
write mode each operation is two-phase (preview → typed confirm), single-use and
audited. `0x27` holds one seed under a 30 s lease rather than re-reading it; the
opt-in strict re-read is documented in [Web UI](web-ui.md).

The AI layer is bring-your-own-key, has **no write capability** (a read-only tool
registry with no write/reach path into `security` or `writes`), and needs no SDK.
See [How to run the AI layer](web-ui.md#how-to-run--stage-2-security-writes-and-ai).

The same rule as the CLI/TUI applies: one owner talks to the adapter at a time,
and each request runs as a job on a single worker thread (a second bus operation
while a stream is running is refused with `409 bus_busy`).

See [Web UI](web-ui.md) for the design, the API catalogue, and how to run it.

## Storage and environment

| Variable | Effect |
|---|---|
| `PACIFICA_OBD_HOME` | Directory for `pacifica.db` and `events.jsonl`. |
| `PACIFICA_OBD_DB` | Explicit SQLite path. |
| `PACIFICA_OBD_LOG` | Explicit JSONL log path. |
| `PACIFICA_OBD_NO_LOG=1` | Disable persistence entirely. |
| `PACIFICA_OBD_WEB_TOKEN` | Bearer token for the web UI (same as `--token`). |
| `PACIFICA_OBD_AI_PROVIDER` | Override the AI provider id (file values lose to the environment). |
| `PACIFICA_OBD_AI_MODEL` / `PACIFICA_OBD_AI_BASE_URL` | Override the model / endpoint. |
| `PACIFICA_OBD_AI_KEY_OPENAI` / `_ANTHROPIC` | Key override (wins over the 0600 `ai.json`). |
| `PACIFICA_OBD_AI_OFFLINE=1` | Force the deterministic offline provider (tests/demos). |

`--db PATH` and `--no-log` override the corresponding variables for one run. See
[Storage model](technical.md#storage-model) for the tables and retention story.

!!! tip "Isolate experiments"

    Point `PACIFICA_OBD_HOME` at a scratch directory when testing, so synthetic
    captures never mix with real vehicle data:

    ```sh
    export PACIFICA_OBD_HOME=$(mktemp -d)
    uv run pacifica-obd modules --start 7E0 --end 7E1 --timeout-ms 40 --json
    ```
