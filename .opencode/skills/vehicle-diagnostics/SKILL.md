---
name: vehicle-diagnostics
description: Guide for connecting to a vehicle and diagnosing it with the pacifica-obd toolkit (or any ELM327/STN adapter) — reading and clearing trouble codes (DTCs), interpreting a check-engine/MIL light, reading the VIN, module identification (DIDs), live data, readiness monitors, and researching a specific fault code. Use this whenever the user mentions a car, OBD-II, UDS, a scan tool, an adapter like vLinker/OBDLink/ELM327, a trouble code (e.g. P0AA1, U0100), a check-engine or MIL light, "connect to my vehicle", "read the codes", "what modules respond", or a Chrysler/Stellantis/FCA vehicle such as a Pacifica (including the Hybrid/PHEV). It encodes the protocol gotchas that silently break real vehicle reads, the read-first safety rules, the private-data policy, and how to research a DTC (NHTSA TSBs). Reach for this skill even if the user just says "my check engine light is on" or "scan my car".
---

# Vehicle diagnostics

Use this skill to connect to a vehicle, read it correctly, and reason about a
fault code. It exists because the obvious approach (open the port, ask for DTCs)
**appears** to work while silently missing most of the vehicle — you will
mislead the user if you don't follow the protocol rules below.

## The one thing that matters most

An ELM327/STN adapter only accepts replies from `request + 8` by default, so it
will happily talk to the four modules in the `7E8`–`7EF` block and **pretend
that is the whole car**. It is not: most modules answer at `request − 0x280`
(CAN-C chassis and CAN-IHS) or `+1` (SGW), and their replies are **silently
dropped** — they look like "no response". Setting the adapter's **receive filter
(`ATCRA <responder>`)** and discovering with a **single-frame probe** (`22F100`)
raised the visible module count from **4 to 32** on a Pacifica Hybrid.

If you take one thing from this skill: **read `references/protocol.md` before
trusting any "module did not respond" result.**

## Workflow

1. **Confirm the adapter.** `uv run pacifica-obd ports` then `info`. The chip
   identity comes from `STI` (e.g. `STN1170 v4.3.2`); `ATI` returns the
   compatibility string (`ELM327 v2.3`) and is *not* the chip. Check `ATRV`:
   `--.-V` or a low/absent reading means no vehicle power / ignition off.
2. **Check the port is yours.** Only one process may hold the serial port; if
   another app (a scan tool, a `dotnet` or `python scan.py` app) has it, reads
   fail or corrupt. Verify with `lsof /dev/cu.usbserial-*` before blaming the car.
3. **Read the VIN.** Do not assume mode `09 02` works — many vehicles (incl. the
   Pacifica Hybrid) **do not serve it**. Read UDS `22 F190` instead; the `vin`
   command falls back automatically. The VIN's 10th character is the model year.
4. **Turn the ignition ON** for bus traffic (engine off is fine for most reads;
   some modules stay asleep until RUN). Re-read if modules are silent.
5. **Discover modules** across **both buses** (`can-c` and `can-ihs`) and with
   **more than one probe service** (`1003` and `22F190`). Note the request and
   response ID for each responder — you will need the response ID for flow control.
6. **Read DTCs per module** (UDS `0x19 0x02`), attributing each code to the module
   that reported it. A merged union (mode 03) hides which module is actually
   faulted; always prefer per-module reads.
7. **For a fault you care about, get context**: freeze frame / snapshot, the
   module's identification block (`F187` part number, `F188`/`F189` software
   version), and the readiness monitors.
8. **Research the code** before advising (see `references/troubleshooting.md`).
9. **Persist** reads with the tool (local SQLite/JSONL) and export a report with
   `dump` if the user wants something to keep.

## Safety — read first

- **Read-only by default.** Never `0x2E` (write), `0x31` (routine), `0x27`
  (security) or clear DTCs (`0x14` / mode 04) without an explicit, specific
  request from the user. Clearing erases freeze-frame evidence and resets
  readiness monitors, and does not fix anything.
- **The Security Gateway (SGW)** on 2018+ vehicles blocks writes without a
  bypass cable. Reads pass. Don't promise a write will work.
- **MIL on = diagnose, not clear.** If the check-engine light is on, the useful
  action is identifying the fault, not turning it off.
- Writes are high-risk (a bad value can brick a module). If asked, gate behind
  confirmation, prefer read-only, and record before/after.

## Privacy — real vehicle data never goes in a shared repo

VINs, DTC lists, module maps and captures of a specific car are personal data —
they must live **outside any public repository**. Don't hard-code a
machine-specific path in guidance: resolve the location at runtime. The tool
writes to its own data directory (an XDG-style default, overridable with
`PACIFICA_OBD_HOME` / `PACIFICA_OBD_DB`); for anything to keep, use a separate
**private** project or directory the user chooses — ask them where it is if it
isn't obvious. Make sure the repo's `.gitignore` blocks dump filenames
(`vehicle.*`, `dumps/`).

## When it goes wrong

- **Everything is "no response"** → flow control / receive filter not set, or
  ignition off, or the wrong bus. This is the #1 false diagnosis.
- **A module answered once, then "no response"** → a stale receive filter or a
  desynced adapter; reset the adapter and re-address the module.
- **Mode `09 02` empty** → normal on some vehicles; use `22 F190`.
- **`ATI` shows `?`** → python-OBD desync, not a hardware fault; a direct `ATI`
  usually returns the string.

## References

- `references/protocol.md` — adapter/ELM/STN command layer, STP bus presets,
  ISO-TP + flow control, the `+8` / `-0x280` / `+1` addressing rules, SGW, VIN
  via F190, UDS service cheat-sheet. **Read this before trusting discovery.**
- `references/troubleshooting.md` — how to research a DTC (NHTSA TSB route),
  read a module's software version, and a worked example (P0AA1 on the Pacifica
  Hybrid), plus the read → interpret → advise cadence.
