# Getting started

Ten minutes from a cold clone to reading your van's codes. Everything below runs
from the repository root.

## What you need

| | |
|---|---|
| **Vehicle** | 2021 Chrysler Pacifica (RU), any trim. Ignition must be **ON** for bus reads. |
| **Adapter** | a **vLinker FS** USB adapter (FTDI FT230X → STN1170-class), plugged into the OBD-II port under the dash. |
| **Host** | macOS (the `/dev/cu.usbserial-*` paths below) with a USB-A/C port. |
| **Toolchain** | [uv](https://docs.astral.sh/uv/) — it manages Python 3.12+ and the virtualenv for you. |

!!! tip "Adapter, but no car yet?"

    The adapter answers AT commands with no vehicle attached, so `ports`, `info`,
    `raw` and the JSON plumbing all work bench-side. Bus reads report
    `CAN ERROR` / `NO DATA` until an ignition-on vehicle is present.

## 1. Install

```sh
git clone https://github.com/yorch/pacifica-obd.git
cd pacifica-obd
uv sync                     # creates .venv and installs python-OBD, rich, textual
uv run pacifica-obd --help  # every subcommand, generated from the CLI
```

`uv run pacifica-obd …` is the only invocation you need — it keeps the project
environment in sync and never touches your system Python.

## 2. Plug in the adapter and find it

With the adapter in the OBD port and USB attached:

```sh
uv run pacifica-obd ports
```

```text
Candidate serial ports
┏━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ # ┃ Device                    ┃
┡━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1 │ /dev/cu.usbserial-1420    │
└───┴───────────────────────────┘
```

The CLI auto-detects the adapter (preferring `usbserial` / `vlinker` / `ftdi`
names). Pass `--port /dev/cu.usbserial-XXXX` if you have other serial devices
attached.

## 3. Turn the car on

!!! warning "The adapter powers up from USB; the vehicle bus does not"

    With the ignition **OFF** you will see `status: ELM Connected` and no
    protocol — the adapter is alive, the car is silent. Turn the ignition to
    **ON** (engine off is fine) or start the engine for full live data, then
    re-run.

Check the link, the battery voltage and the protocol:

```sh
uv run pacifica-obd info
```

```text
Port            /dev/cu.usbserial-1420
Status          Car Connected
Protocol        ISO 15765-4 (CAN 11/500)
Adapter (ATI)   ELM327 v1.5 (vLinker FS)
Voltage (ATRV)  12.4 Volts
VIN             <VIN>
```

A healthy van reads **12.4–12.6 V** at rest. Below ~12.0 V expect flaky negative
responses and phantom "no module" results — charge the battery before
troubleshooting the tool.

`info` reports the ELM327-compatible identity (`ATI`). For the chip itself use
`raw STI`, which is where `STN1170 v4.3.2` comes from.

## 4. First reads

```sh
uv run pacifica-obd scan      # one-shot: VIN + stored DTCs + supported PID count
uv run pacifica-obd dtc       # stored (03), pending (07) and permanent (0A) codes
uv run pacifica-obd dtc-all   # UDS 19 02 from *every* module on CAN-C
```

`scan` is the five-second answer to "is this thing talking to my van?". `dtc-all`
is the interesting one: it discovers modules by probing the address range, then
asks each responder for its codes — no SGW bypass needed to *read*.

!!! note "Discovery takes about a minute per bus"

    The default range is `0x700`–`0x7FF`, and each address waits at least 0.25 s,
    so a full CAN-C or CAN-IHS sweep is roughly a minute; `--bus both` is over
    two. Narrow it with `--start`/`--end` (or `--module 7E0`) while you are
    iterating.

UDS codes come back as `P1234-12` style values plus status flags, with **no text
descriptions** — that is what a proprietary DTC database would add.

Then, when you want the wider picture:

```sh
uv run pacifica-obd dtc-all --bus both --json   # both buses, machine-readable
uv run pacifica-obd capture --bus both          # full snapshot, stored locally
uv run pacifica-obd module-map                  # what modules are known, no car needed
```

!!! note "Nothing writes unless you ask it to"

    Every command above is read-only. The tool also ships guarded, experimental
    write primitives (`write-did`, `routine`, `dtc --clear`), which demand an
    explicit `--confirm` and are usually refused by the Security Gateway on a
    2021 anyway — see [Guarded write path](usage.md#guarded-write-path).

## 5. Optional: talk to the adapter directly

`raw` is the escape hatch: it sends AT/ST/OBD/UDS commands straight to the
adapter, outside python-OBD's abstractions. Each positional argument is one
command, so keep a two-word command in quotes.

```sh
uv run pacifica-obd raw ATI STI        # adapter identity and chip identity
uv run pacifica-obd raw ATRV           # battery voltage
uv run pacifica-obd raw 0100 --header 7DF     # supported-PID bitmask (functional)
uv run pacifica-obd raw 22F190 --header 7E0 --json
uv run pacifica-obd raw "ATSH 7E0" 22F187 --allow-long --headers
```

!!! note "Commands are configured, not guessed"

    Before a UDS operation the CLI selects a bus preset (`STP 33` for CAN-C,
    `STP 53` for CAN-IHS), turns headers on (`ATH1`), keeps ISO-TP auto-format
    on (`ATCAF1`), allows long messages (`ATAL`) and sets a per-request timeout
    (`STPTO`). It restores whatever protocol was active when it opened the port,
    and it never selects a protocol in a way that persists to adapter EEPROM
    (`ATSP` is never used).

    `raw` is the exception: it configures the adapter per invocation for exactly
    what you asked for (protocols via non-saving `ATTP`) and does not
    snapshot/restore the previous state.

## Troubleshooting

| Symptom | Likely cause | Do this |
|---|---|---|
| `No serial port found.` | Adapter unplugged, or USB not enumerated | Re-seat the USB end; `ls /dev/cu.usb*` should list a `usbserial` device |
| `Could not open the port: …` | Another app (FORScan, `screen`, a serial monitor) holds it | Quit that app; unplug/replug |
| `ELM Connected`, no protocol | Ignition off, or the adapter's on a bus with no traffic | Ignition **ON**, re-run |
| `CAN ERROR` / `NO DATA` | Wrong bus, empty address range, or a module that ignores the probe | Try `--bus can-ihs` (or `--bus both`), widen `--start`/`--end` |
| No VIN | Vehicle off, or the PCM refused mode `09 02` | Ignition ON; try `read-did --module 7E0 F190` |
| Fewer modules than expected | The module is on the other bus, asleep, outside the probe range, or ignores `10 03` | Try the other bus and a wider range before suspecting wiring; a scan is not proof a module is absent |
| `No local database found.` | Nothing has been captured yet (or `--no-log` was used) | Run a command that stores data, then `history` |

## Where your data goes

Every non-trivial run is logged locally — a SQLite database plus an append-only
JSONL log:

```sh
uv run pacifica-obd history            # vehicles + recent sessions
uv run pacifica-obd show --vin <VIN>   # everything stored for one VIN
```

The files live under `$PACIFICA_OBD_HOME`, else `$XDG_DATA_HOME/pacifica-obd`,
else `~/.local/share/pacifica-obd`. Nothing is uploaded anywhere, and no VIN or
capture is ever committed to the repository — see
[Storage](technical.md#storage-model) for the schema and overrides.

## Next

- [Command reference](usage.md) — every subcommand and flag.
- [Technical reference](technical.md) — how the layers fit together.
- [Chrysler Security Gateway](chrysler-sgw.md) — what a 2021 Pacifica will and
  won't let a tool do.
