# Troubleshooting a fault code

## Cadence: identify → read → interpret → research → advise

Move in that order and keep the vehicle in the loop — an online writeup is a
hypothesis, the car is the evidence. **Do not clear a code** to "see if it comes
back" unless the user explicitly asks: clearing erases the freeze frame and
resets readiness monitors, which is often the evidence you need.

## Read the context around a code

A bare DTC is a starting point, not a diagnosis. Gather, per module:

- the DTC itself (UDS `0x19 0x02`), its **status byte**, and failure-type byte;
- the **freeze frame / snapshot** captured when it set;
- the module's **identification**: `F187` (part number), `F188`/`F189`
  (software number/version);
- **readiness monitors** and whether the MIL is on.

Attribution matters: mode 03 is a merged union across modules. Always attribute
each code to the module that reported it (see `protocol.md` for reading modules
correctly — a misconfigured adapter hides most of them).

## Research the code

- Search `<code> <make> <model> <year>`.
- Prefer primary sources: **NHTSA TSB PDFs** (`static.nhtsa.gov/odi/tsbs/...`) and
  manufacturer bulletins mirrored on oemdtc.com. Watch for **superseded** TSBs —
  the newest revision is the current one.
- Classify the likely cause: **software (reflash)**, **component failure**,
  **wiring/connector**, or **network** (a `U`-code usually means one module
  can't talk to another — often the real story).
- A code can be a **false log from a software anomaly**; verify before concluding
  a part must be replaced.

## Match a TSB to *this* car

Read the relevant module's software/part identifiers and compare to the TSB's
applicability (model year, sales code, build date). Dealer tools (e.g. wiTECH)
can read the flash part number; our tool reads whatever the module exposes via
`F187`/`F188`/`F189`.

## Worked example — `P0AA1`

`P0AA1` = *Hybrid/EV Battery Positive Contactor "A" Circuit Stuck Closed*: the
ECU believes the high-voltage battery's positive contactor is welded closed.

- On the **Pacifica Hybrid**, Stellantis TSBs document this as often a **BPCM
  software** fault (erroneous logging), fixed by a **BPCM reflash** —
  TSB **08-082-24**, superseded by **08-084-24** (later HCP/AHCP flashes exist
  too). Documented symptoms can escalate to **the vehicle not starting**.
- It can also be a **genuine stuck contactor**, so don't assume software.
- Useful companion codes to look for: `U0100`/`U0140` (lost comms), `P0AA4`
  (negative contactor), `P0DAB` (cell balancing), `P0C78` (precharge time).
- Practical guidance: **don't clear**; read the BPCM (`0x7E7`) software/part
  info; recommend a dealer check of the **BPCM software level** — it is
  frequently a reflash rather than a battery replacement.

## What this tool cannot do

Module **flashing/programming** and some key/immobiliser functions are dealer-only
(licensed tools + OEM files). This tool reads; it does not program. Treat the
**Security Gateway** as a hard boundary for writes on 2018+ vehicles.

## Privacy reminder

Anything you capture — VIN, DTC list, module map — is **personal vehicle data**.
Keep it out of shared repositories; use the tool's local data dir or a private
project the user chooses.
