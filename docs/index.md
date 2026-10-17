<div class="hero" markdown>

# pacifica-obd

A command-line **OBD-II and UDS diagnostics toolkit** for a 2021 Chrysler
Pacifica, talking to a **vLinker FS** USB adapter. Built on
[python-OBD](https://github.com/brendan-w/python-OBD), managed with
[uv](https://docs.astral.sh/uv/), scriptable end to end.

Generic OBD-II reads are the tested core. The same adapter is then driven at the
raw ELM327/STN level for UDS work: module discovery, all-module DTC reads and
identification DIDs on either CAN bus — plus a deliberately guarded write path
you have to opt into explicitly.

```sh
uv sync
uv run pacifica-obd scan      # VIN + stored codes + supported PIDs
```

[Get started](getting-started.md){ .md-button .md-button--primary }
[Command reference](usage.md){ .md-button }

<small>macOS · Python 3.12+ via uv · MIT · not affiliated with Stellantis</small>

</div>

## What this can and can't do here

| Task | Support | How |
|---|---|---|
| VIN, stored and pending codes, readiness monitors, live data, freeze frame | ✅ | Generic OBD-II: `scan`, `dtc`, `readiness`, `watch`, `freeze` |
| Codes from the other modules that answer on CAN-C / CAN-IHS | ✅ read-only | `dtc-all` — raw codes and status flags, **no text descriptions** |
| Part, software and hardware numbers per module | ✅ read-only | `module-info`, `read-did` (`F187`–`F191`) |
| Which modules should exist, and at which addresses | ✅ read-only | `module-map` (plus an optional read-only AlfaOBD database) |
| Clear engine codes | ⚠️ | `dtc --clear` sends mode `04`. The Security Gateway may refuse it, and it erases freeze frames and readiness history |
| Write a DID (`2E`) or run a routine (`31`) | ⚠️ experimental | Guarded behind `--confirm`, not reversible by this tool, usually refused by the SGW |
| Coding, adaptations, module programming | ❌ | Beyond those primitives. Use AlfaOBD / Appcar DiagFCA / JScan / dealer wiTECH, with an SGW bypass |

## What it does

<div class="grid cards" markdown>

-   :material-engine-outline:{ .lg .middle } **Check-engine basics**

    ---

    VIN, stored (mode `03`) and pending (mode `07`) codes, permanent codes when
    the installed python-OBD exposes mode `0A`, readiness monitors, and the
    freeze frame behind a fault.

-   :material-gauge:{ .lg .middle } **Live dashboard**

    ---

    `watch` RPM, speed, coolant, load, throttle, intake temp, MAF and fuel level.
    Log every sample to CSV, or stream newline-delimited JSON into `jq`.

-   :material-car-wrench:{ .lg .middle } **Every module, not just the PCM**

    ---

    `dtc-all` discovers the modules that answer on one or both CAN buses, then
    asks each of them for its codes over UDS `19 02`.

-   :material-identifier:{ .lg .middle } **Who is on the bus**

    ---

    `modules` finds the responders, `module-info` reads their part and
    software/hardware numbers, and `module-map` shows which modules *should*
    exist and at which addresses.

-   :material-database-clock:{ .lg .middle } **History that stays local**

    ---

    Every run lands in a local SQLite database plus an append-only JSONL log, so
    this week's codes can be compared with last month's. Nothing is uploaded.

-   :material-console:{ .lg .middle } **TUI or JSON**

    ---

    Browse with the keyboard/mouse in `tui`, or put `--json` on any read command
    for scripts, spreadsheets and CI.

</div>

!!! warning "Read-only by default — and park the van"

    Discovery and `dtc-all` put each module into an extended diagnostic session
    (`10 03`) while they read. Run them parked, with the ignition ON. Nothing
    writes unless you ask it to: the write services (`dtc --clear`, `write-did`,
    `routine`) are gated behind an explicit confirmation, and `security` only
    asks for a seed — no seed→key provider is configured, so nothing is unlocked.

    On a 2021 Pacifica the **Security Gateway blocks writes** on the stock port,
    so expect a write to be refused without a 12+8 bypass cable. See the
    [Security Gateway deep dive](chrysler-sgw.md).

## Deep dives

<div class="grid cards" markdown>

-   **[Chrysler Security Gateway](chrysler-sgw.md)**

    ---

    What the SGW is, what it blocks on a 2021, when a 12+8 bypass cable is
    needed, and where AlfaOBD / Appcar / JScan / wiTECH fit.

-   **[Enhanced (AlfaOBD-class) diagnostics](enhanced-diagnostics.md)**

    ---

    How manufacturer-aware tools actually work, what this adapter can already
    do, and the roadmap from generic OBD-II to UDS.

-   **[Vehicle support (RU family)](vehicle-support.md)**

    ---

    Other model years, the Voyager and Grand Caravan, and the PHEV: what is
    known to carry over and what still has to be mapped.

-   **[Technical reference](technical.md)**

    ---

    Architecture, ISO-TP/UDS notes, the `+8` versus `−0x280` addressing rule,
    bus presets, and the storage model.

</div>

## References & related work

- [python-OBD](https://github.com/brendan-w/python-OBD) — the library doing
  generic OBD-II PID work here. [pyobd](https://github.com/barracuda-fsh/pyobd)
  is a finished GUI front end built on the same library; this project is the
  engine plus a scriptable CLI.
- [comma.ai opendbc — Chrysler](https://github.com/commaai/opendbc/blob/master/opendbc/car/chrysler/values.py)
  — vehicle-observed ECU addresses, the `−0x280` response rule, and the
  non-standard FCA DID `F132`.
- [OBD Solutions STN11xx reference manual](https://www.scantool.net/scantool/downloads/234/stn1100-frpm-preliminary.pdf)
  — AT/ST command set and protocol presets.

MIT licensed — see the repository's `LICENSE`.
