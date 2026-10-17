# pacifica-obd

An OBD-II diagnostics toolkit for a **2021 Chrysler Pacifica** talking to a
**vLinker FS (FTDI FT230X)** USB adapter, built on
[python-OBD](https://github.com/brendan-w/python-OBD) and managed with
[uv](https://docs.astral.sh/uv/).

## Why a script instead of a GUI app?

`python-OBD` is a **library**. This project wraps it in a small CLI so you can
script, log, and automate diagnostics — and extend it however you like. If you
want a ready-made mouse-driven GUI instead, use
[pyobd](https://github.com/barracuda-fsh/pyobd) (which is itself built on top of
python-OBD). See the "python-OBD vs pyobd" note at the bottom.

## Setup

```sh
cd ~/code/pacifica-obd
uv sync          # creates .venv and installs deps
```

## Usage

```sh
uv run pacifica-obd ports          # list serial ports (finds the vLinker FS)
uv run pacifica-obd info           # adapter + vehicle + protocol info
uv run pacifica-obd vin            # read the VIN (mode 09 02)
uv run pacifica-obd dtc            # read stored + pending trouble codes
uv run pacifica-obd dtc --clear    # read, then clear (asks to confirm)
uv run pacifica-obd live           # dump current value of every supported PID
uv run pacifica-obd watch          # live dashboard (Ctrl-C to stop)
uv run pacifica-obd watch --pids RPM,SPEED,COOLANT_TEMP
uv run pacifica-obd watch --csv drive.csv            # log every sample to CSV
uv run pacifica-obd readiness      # readiness / I-M monitors (pre-emissions test)
uv run pacifica-obd freeze         # freeze-frame snapshot behind a DTC (mode 02)
uv run pacifica-obd scan           # one-shot: VIN + DTCs + PID count
uv run pacifica-obd raw ATI ATRV   # raw AT/OBD/UDS commands (see docs)
uv run pacifica-obd modules        # discover UDS modules on CAN-C (read-only)
uv run pacifica-obd modules --bus can-ihs --identify --json
uv run pacifica-obd dtc-all        # read DTCs from every module on CAN-C
uv run pacifica-obd dtc-all --bus both --json
```

Raw/UDS examples:

```sh
uv run pacifica-obd raw 22F190 --header 7E0 --protocol 6 --json
uv run pacifica-obd raw 1003 --header 7E0 --protocol 6 --no-auto-format --json
```

Flags: `--port /dev/cu.usbserial-XXXX` (default auto), `--baud 115200`,
`--timeout 1.0`, `--check-voltage`.

### Machine-readable output

Add `--json` to any read command, before or after the subcommand:

```sh
uv run pacifica-obd --json dtc
uv run pacifica-obd scan --json
uv run pacifica-obd readiness --json
```

`watch --json` streams **newline-delimited JSON** (one object per sample), so it
pipes cleanly into `jq` or a file:

```sh
uv run pacifica-obd watch --pids RPM,SPEED --json | tee drive.jsonl
# {"timestamp": "...", "values": {"RPM": {"value": 812.5, "unit": "..."}, "SPEED": 0}}
```

In JSON mode, status messages go to **stderr**, so stdout stays valid JSON.
`dtc --clear --json` requires `--force` (there is no prompt in JSON mode).

### Turn the car on first

The adapter powers up from USB alone, but the **vehicle bus needs the ignition
on**. With the car off you'll see `status: ELM Connected` and no protocol. Turn
the ignition to **ON** (engine off is fine, or start it for full live data) and
re-run.

## What this can and can't do on a 2021 Pacifica

**Generic OBD-II (works here):** VIN, emissions readiness monitors, stored /
pending / permanent DTCs, live engine + transmission data (RPM, speed, coolant,
load, throttle, intake temp, MAF, fuel level, etc.), and clearing codes.

**Not covered:** Chrysler-specific modules (BCM, ABS, airbag, HVAC, TPMS,
programming/coding). Those need a manufacturer-aware tool — for Chrysler/Jeep/
Dodge/RAM look at **JScan** or **AlfaOBD**, not FORScan (FORScan is Ford/Mazda
only). See [`docs/chrysler-sgw.md`](docs/chrysler-sgw.md) for the 2021 Pacifica
Security Gateway, the 12+8 bypass cable, and the AlfaOBD / Appcar / JScan path.
[`docs/enhanced-diagnostics.md`](docs/enhanced-diagnostics.md) investigates how
to grow this project toward AlfaOBD-class UDS support.

## Layout

```
src/pacifica_obd/
  __init__.py   entry point
  cli.py        argparse subcommands + rich output
  elm.py        raw ELM327/STN transport (for UDS work)
docs/
  chrysler-sgw.md          SGW + bypass + tool path
  enhanced-diagnostics.md  AlfaOBD-class plan
```

## python-OBD vs pyobd

| | python-OBD (`obd`) — this project | pyobd |
|---|---|---|
| What it is | Library + your own CLI | Finished GUI app (wxPython) |
| Interface | Scripts / terminal | Point-and-click desktop window |
| Built on | — | python-OBD (the same library) |
| Best for | Automation, logging, custom logic, CI | Quickly reading/clearing codes by hand |
| Setup | `uv sync` (already done) | Needs wxPython + a GUI toolkit |

In short: **pyobd is a front-end; python-OBD is the engine.** This project gives
you the engine plus a convenient CLI.

## References & related work

- [`grantUser/fca-ecu-diagnostics`](https://github.com/grantUser/fca-ecu-diagnostics)
  — FCA/Stellantis ECU/CAN map, Mode‑22 PIDs, DTC text (currently unavailable).
- [`grantUser/seedkey-fca-stellantis`](https://github.com/grantUser/seedkey-fca-stellantis)
  — FCA/Stellantis Security Access (0x27) seed→key generators (currently unavailable).
- [`ludwig-v/psa-seedkey-algorithm`](https://github.com/ludwig-v/psa-seedkey-algorithm)
  (GPL‑3.0) — PSA/Stellantis seed/key *technique* (2‑byte keys, brute-forced or
  dumped from calibration). Different platform (PSA, not FCA); referenced for
  method only, no code copied.
- [OBD Solutions STN11xx reference manual](https://www.scantool.net/scantool/downloads/234/stn1100-frpm-preliminary.pdf)
  — AT/ST command set and protocol presets.

## License

MIT — see [LICENSE](LICENSE). This project contains no third-party OEM or
GPL‑licensed code.

