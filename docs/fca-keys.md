# Built-in FCA seed→key (PowerNet)

`pacifica_obd.fca_keys` ships the FCA/Stellantis PowerNet **seed→key**
algorithms and the selector that picks one, so `pacifica-obd security` can
compute a `0x27` key **without any external provider file** on the modules this
project supports.

```sh
# show which algorithm would be used (offline, no adapter)
uv run pacifica-obd security --module 742 --device-id 11872 --level 1 --explain

# dry run: read the seed only, never send a key
uv run pacifica-obd security --module 742 --device-id 11872 --level 1

# unlock: send the key (still gated by --confirm)
uv run pacifica-obd security --module 742 --device-id 11872 --level 1 --unlock --confirm
```

The module is deliberately conservative: **a wrong key can lock a module**, so
whenever more than one algorithm could apply the command refuses and lists the
candidates instead of guessing.

!!! note "Provenance"
    The algorithms are a mechanical translation, produced by
    [`tools/extract_seedkey.py`](fca-seedkey-selector.md) from the **operator's
    own licensed copy** of a third-party FCA diagnostic application (plus the
    decompiler's name map). No vendor code or constants are vendored in source
    form and the shipped runtime file does not name the vendor application. The
    selector table is the part of that application's key dispatcher we could
    read for the modules this project already ships. This repository is MIT
    licensed; treat a generated/unverified algorithm as a guess, not a key.

!!! warning "Unlocking is a write-path action"
    Behind the Security Gateway a `0x27` key is usually only accepted with a
    12+8 bypass fitted. See [`chrysler-sgw.md`](chrysler-sgw.md). Nothing here is
    a statement about safety.

---

## 1. What is shipped

`src/pacifica_obd/fca_keys.py` is checked in (so the tool works with no vendor
material present) but is **generated**. It exposes:

| Name | Meaning |
|---|---|
| `ALGORITHMS` | `name -> f(seed_bytes) -> key_bytes`, the transpiled entries. |
| `explain(module, level, device_id=, variant_id=, supplier=)` | why an algorithm was (or was not) chosen. |
| `provider_for(module, level, ...)` | a `key_for(seed, **ctx) -> int` provider, or raises `FcaKeyError`. |
| `compute_key(module, level, seed, ...)` | resolve + compute in one call. |

The selector is the recovered dispatcher reduced to what is reachable for a
module in [`pacifica.json`](pacifica-data.md): a row is kept only when its ECU
type is one of ours or a **device id** it selects on belongs to one of our
modules. Rows keyed solely on something an operator cannot supply — part-number
strings (`m_c4`), guided-procedure overrides (`m_ge`) — are dropped, so the
selector can never silently run one of them.

The table keys on three discriminators an operator can give on the command line:

| Flag | Field | Meaning |
|---|---|---|
| `--device-id N` | `c2` | ECU **device id** (integer). |
| `--variant N` | `cx` | ECU **variant id** (`22 F110`/`22 F100`, integer; `0x…` accepted). |
| `--supplier CODE` | `m_c0` | supplier/isocode string, e.g. `00C6`. |

When the bundled dataset pins a **single** device id for a module the CLI uses it
automatically; when a module has several (e.g. IPC `11871`/`11872`) you must pass
`--device-id`, because the two map to different algorithms.

## 2. Coverage of the RU module set

Levels are the security levels (`S1`, `S3`, `S5`, `S61`, `S65`) recovered for
each module. "resolves with" is what the CLI needs to pick exactly one
algorithm.

| Module | Levels | Discriminator | Resolves with |
|---|---|---|---|
| `IPC_PN` | S1, S5, S61, S65 | device | `--device-id 11872` (MY2020+) or `11871` (MY2017) |
| `LBSS_PN` | S1, S5 | device | automatic (`8467`) |
| `RBSS_PN` | S1, S5 | device | automatic (`8468`) |
| `ABS_PN` | S1 | variant | `--variant …` |
| `ANC_PN` | S1, S5 | variant | `--variant …` |
| `APM_PN` | S1, S5 | variant | `--variant …` |
| `BPCM_PN` | S1, S5 | variant | `--variant …` |
| `CVPM_PN` | S1, S5 | variant | `--variant …` |
| `ESM_PN` | S1, S5 | variant | `--variant …` |
| `ICS_PN` | S1, S5 | variant | `--variant …` |
| `OCM_PN` | S1, S5 | variant | `--variant …` |
| `ORC_PN` | S1 | variant | `--variant …` |
| `RFH_PN` | S1, S3, S5, S61 | device + part | **refuses** — see §3 |
| `AMP_PN`, `EPS_PN`, `HCP_PN` | S1, S5 | variant **and** supplier | **refuses** — see §3 |

For example, `IPC_PN` S1 resolves like this:

```sh
$ uv run pacifica-obd security --module 742 --device-id 11872 --level 1 --explain
algorithm: CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01 (IPC_PN level 0x01; device=11872)
```

The full 131-row table can be listed from Python:

```sh
uv run python -c "from pacifica_obd import fca_keys as k; print(len(k._SELECTOR), len(k.ALGORITHMS))"
```

### Verified vectors

These algorithm names and keys are the original C#'s output for seed
`12 34 56 78`; `tests/test_fca_keys.py` asserts the shipped Python reproduces
every one.

| Algorithm | Seed | Key |
|---|---|---|
| `CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01` | `12345678` | `BA32A3C0` |
| `CalculateKeyS5_IPC_PN_Sup00C6_Var49_AlgID0258_V01` | `12345678` | `D8618EDE` |
| `CalculateKeyS1_IPC_PN_Sup0087_Var47_AlgID01A5_V01` | `12345678` | `37A80259` |
| `CalculateKeyS1_RFH_PN_Sup0040_Var45_AlgID0240_V01` | `12345678` | `5472BAAE` |
| `CalculateKeyS1_RFH_PN_Sup00C6_Var4A_AlgID02A7_V01` | `12345678` | `CC8A4751` |
| `CalculateKeyS1_RFH_PN_Sup00C6_Var48_AlgID0293_V01` | `12345678` | `3254D0B9` |
| `CalculateKeyS1_ABS_PN_Sup0027_Var40_V01` | `12345678` | `F69E7C3A` |
| `CalculateKeyS1_BPCM_PN_Sup00DD_Var40_AlgID017D_V01` | `12345678` | `5B4EBEF4` |
| `CalculateKeyS1_LBSS_PN_Sup00F1_Var0001_AlgID0329_V02` | `12345678` | `2E5A13EB` |
| `CalculateKeyS1_ICS_PN_Sup0032_Var40_V01` | `12345678` | `95A1ADFE` |
| `CalculateKeyS1_ESM_PN_Sup0007_Var41_V02` | `12345678` | `C76B60FA` |
| `CalculateKeyS1_APM_PN_Sup00D6_Var40_AlgID01B8_V01` | `12345678` | `4E28C521` |
| `CalculateKeyS1_ORC_PN_Sup0079_Var41_V02` | `0000000012345678` | `822E3CEA` |

Most algorithms take a 4-byte seed; a few (e.g. the ORC row above) take 8, and
**the key they return is still 4 bytes**. `fca_keys` records both widths:
`_SEED_WIDTH` (how many seed bytes to rebuild from the integer — so an 8-byte
seed with leading zeros is not silently shrunk to 4) and `_KEY_WIDTH` (how many
key bytes `security_access` sends, which is *not* the seed's length). When
`security_access` calls the provider it passes the seed's real byte length as
`width`, and the provider exposes its output length as `key_width`.

## 3. What could not be mapped (and why)

| Target | Why it refuses |
|---|---|
| `RFH_PN` (all levels) | three rows share `c2 == 14039`; the real discriminator is a part-number string the dispatcher hashes (not captured), so all are listed as candidates. |
| `AMP_PN`, `EPS_PN`, `HCP_PN` | rows are keyed on **variant *or*** a supplier/isocode (`m_c0`) we cannot read for the Pacifica; an unknown discriminator could still match, so it refuses. |
| `BODY_CHRYSLER` | the recovered BCM rows select on a device id (`56077`) that is not this module's; the S1/S5 arms use inline ternaries the recovery did not capture. |
| `PENTASTAR` (engine PCM) | RSA/legacy shapes whose supplier id is not recoverable per-vehicle; excluded from the build. |
| `SGW_PN`, `ESL_PN`, `PTS_PN`, `SCCM_PN`, `AHCP_PN`, `OBCM_PN`, `EBCM_PN`, `ABSO_PN`, `EAC_PN`, `AHLM_PN`, `FFCMHALF_PN`, `ACC_PN`, `HVAC_PN`, `DDM_PN`, `PDM_PN`, `DMRL_PN`, `DMRR_PN`, `PSDML_PN`, `PSDMR_PN`, `MSM_PN`, `MSMP_PN`, `PLGM_PN`, `FSM_PN`, `HSM_PN`, `HVACR_PN`, `DCSD_PN`, `VRM_PN`, `EOMR_PN`, `EDM_PN`, `RADIO_PN` | no recovered selector branch keyed by a discriminator we can supply. `--provider FILE` is the escape hatch. |

## 4. CLI behaviour

| Invocation | What happens |
|---|---|
| `--explain` | prints the chosen algorithm + the ids used, then exits. **No adapter is opened.** |
| no `--unlock` | **dry run**: reads the `0x27` seed and prints the chosen algorithm; **no key is sent**. |
| `--unlock --confirm` | computes the key and sends `0x27 <level+1> <key>`. |
| `--unlock` without `--confirm`, or with an ambiguous/unmapped target | exits `2` **before** opening the adapter; no key is ever sent. |
| `--provider FILE` | overrides the built-in selector entirely (the provider decides). |

## 5. Regenerate / verify

`tools/build_fca_keys.py` regenerates `src/pacifica_obd/fca_keys.py` from the
decompiled key class, the dispatcher and `pacifica.json`:

```sh
# inputs (once):  ilspycmd -p -o dec --nested-directories <app>.exe
python3 tools/build_fca_keys.py \
    --source dec/ad.cs --map Map.xml --selector dec/af.cs \
    -o src/pacifica_obd/fca_keys.py
```

Verify the freshly generated module against the **original** C# (build the
harness first with `tools/extract_seedkey.py --emit-harness`):

```sh
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \
    --emit-harness /tmp/gt && (cd /tmp/gt && dotnet build -c Release)
python3 tools/build_fca_keys.py --source dec/ad.cs --map Map.xml \
    --selector dec/af.cs -o src/pacifica_obd/fca_keys.py \
    --verify /tmp/gt --samples 4
# verify: 512 agree, 0 mismatch, 0 refused over 128 algorithms x 4 seeds
```

A single algorithm can also be checked the usual way:

```sh
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \
    --emit CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01 \
    -o /tmp/ipc.py --verify /tmp/gt --samples 8
```

Unit tests (no hardware, no vendor material):

```sh
uv run python tests/test_fca_keys.py
```

## 6. Related

* [`fca-seedkey-selector.md`](fca-seedkey-selector.md) — how the dispatcher and
  the `Calculate*` functions were recovered, and how to build a **custom**
  provider from your own install (`--provider FILE`).
* [`chrysler-sgw.md`](chrysler-sgw.md) — the Security Gateway, bypass, writes.
* [`pacifica-data.md`](pacifica-data.md) — the bundled module dataset this
  selector keys off.
