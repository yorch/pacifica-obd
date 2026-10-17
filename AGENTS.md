# AGENTS.md

Guidance for AI agents (and humans) working in this repository.

## What this is

`pacifica-obd` is an OBD-II and UDS (ISO 14229) diagnostics toolkit for a **2021
Chrysler Pacifica** talking to a **vLinker FS** (FTDI FT230X → STN1170-class)
USB adapter on macOS. Built on `python-OBD` for generic OBD-II and a hand-rolled
raw ELM327/STN transport for manufacturer-specific UDS work.

## Environment & commands

- Package manager: **uv**. Run anything via `uv run …`.
- Install/sync: `uv sync`
- Lint by hand (no configured linter): keep lines ≤ ~100 chars, no unused imports.
- Tests are manual/CLI; there is no test suite yet (add one if you change the
  protocol layer — see below).

```sh
uv run pacifica-obd --help
uv run pacifica-obd ports
uv run pacifica-obd modules --start 7E0 --end 7E1 --timeout-ms 40 --json
```

## Layout

```
src/pacifica_obd/
  cli.py    argparse subcommands, UDS helpers (_probe_ids, _parse_uds_frame, ...), storage wiring
  elm.py    raw ELM327/STN transport (Elm327, Elm327Error)
  store.py  SQLite + JSONL persistence (Store)
  tui.py    interactive Textual UI (run())
docs/
  chrysler-sgw.md          SGW, 12+8 bypass, AlfaOBD/Appcar/JScape path
  enhanced-diagnostics.md  roadmap P0–P6 toward AlfaOBD-class UDS
```

## Conventions

- Every read command supports `--json`; status messages go to **stderr** in JSON
  mode (`diag(args)`), data to **stdout**.
- Validate CLI inputs with `argparse` types (`_can_id`, `_did`, `_hex_request`,
  `_timeout_ms`) — reject bad input before opening the adapter.
- Adapter configuration must be checked: use `must_ok()` / `configure_bus()` and
  fail loudly on `?`.
- Reuse helpers; don't re-implement ISO-TP, DTC decoding, or DID reads.
- Prefer explicit, typed code and short docstrings.

## Hardware/protocol facts (do not regress these)

- The adapter reports `STN1170 v4.3.2`. ST presets: **33 = HS CAN 500K/11-bit
  (CAN-C, pins 6/14)**, **53 = MS CAN 125K/11-bit (CAN-IHS, pins 3/11)**.
- **Responses require spaces:** `_init()` sends `ATS1`. The parser also accepts
  compact 11-bit lines but do not disable spacing.
- ISO-TP: a **first frame is a full 8-byte classic-CAN frame**; single-frame
  length is 1–7; consecutive frames must be sequence-checked; frames are ≤ 8 bytes.
- Never persist adapter EEPROM: select protocols with `STP` (ST) / `ATTP` (AT),
  restore the snapshot from `current_protocol()`, and avoid `ATSP` writes.
- The **Security Gateway (SGW)** blocks writes on 2018+ vehicles; reads pass.
  Do not assume a write will succeed. See `docs/chrysler-sgw.md`.

## Persistence

- `store.py` writes a **SQLite DB** (`pacifica.db`) and an append-only **JSONL**
  log (`events.jsonl`) under `$PACIFICA_OBD_HOME` or
  `$XDG_DATA_HOME/pacifica-obd` or `~/.local/share/pacifica-obd`.
- Overrides: `--db PATH`, `PACIFICA_OBD_DB`, `PACIFICA_OBD_LOG`. Disable with
  `--no-log` or `PACIFICA_OBD_NO_LOG=1`.
- `Store` must **degrade to a no-op** on any SQLite/OS error — never crash a
  command because logging failed.
- Every invocation is recorded in `sessions` + `events`; domain reads land in
  `modules`, `dtcs`, `readings`, and the VIN in `vehicles`.

## Testing without a vehicle

The adapter answers AT commands with no car, but bus requests return
`CAN ERROR` / `NO DATA`. Test protocol/parsing logic with **synthetic frames**
(e.g. build spaced ELM response lines and feed `_parse_uds_frame`), and use an
isolated `PACIFICA_OBD_HOME` (e.g. a temp dir) so you don't pollute real data.

```sh
export PACIFICA_OBD_HOME=$(mktemp -d)
uv run pacifica-obd modules --start 7E0 --end 7E1 --timeout-ms 40 --json
```

## Adversarial review workflow

Changes to protocol/parsing/storage are reviewed adversarially before commit:

```sh
codex exec -m gpt-6.1-sol -s read-only -C "$PWD" \
  "Adversarially review <files/functions>. Assume bugs exist. For each finding:
   severity, file:line, failing input, fix. End with the highest-risk defect."
```

Fix real findings, re-run, and note the outcome in the commit message.

## Parallel agents

When a subagent owns a file (e.g. `tui.py`), do not edit that file; work in your
own files and let the parent integrate and commit. Do not run `git` in a
subagent.

## Commits

Small, focused commits with a descriptive subject and a body explaining *why*
(and any adversarial findings addressed). Example subjects:
`P3: read-did (UDS 0x22) and module-info (F186-F191)`.

## Safety

- Never clear DTCs or write data without an explicit user request and a
  confirmation path.
- Treat the SGW and any module write as high-risk; prefer read-only operations.
- Do not commit secrets, VINs, or captured vehicle data. Real captures live in
  the local data dir, not in the repo.
