# FCA / Stellantis seed→key: the selector, and how to generate a provider

*Scope:* this document explains how AlfaOBD decides **which** security-access
algorithm to run for a given module, reproduces the mapping we could recover
from the vendor artefacts, and shows how to turn **your own** copy of AlfaOBD
into a Python provider for `pacifica-obd security --provider`.

*Shipped subset:* the project also ships a built-in provider for the Pacifica
(RU) modules it already supports — the same recovery, reduced to those modules
and regenerated into `src/pacifica_obd/fca_keys.py`. See
[`fca-keys.md`](fca-keys.md) for what is included, what refuses, and how to
regenerate/verify it. Use `--provider` when you need a module outside that set.

*Licence:* nothing in this repository contains vendor constants or vendor code.
`tools/extract_seedkey.py` is our own tool; every constant that ends up in a
generated provider is read out of the files *you* point it at. Keep generated
providers local.

Confidence labels used below match `docs/fca-modules.md`:
**[observed]** = read directly out of a file/table (path + line given);
**[verified]** = reproduced byte-for-byte against the vendor binary;
**[inferred]** = reasoned from observed facts, stated as such.

---

## 1. Where the pieces live

Three separate things have to line up before a key can be computed:

| Piece | Vendor artefact | What it is |
|---|---|---|
| The algorithm | `dec/ad.cs` — decompiled class `ad`, Dotfuscator original `AlfaOBD_PC.AlfaOBDKeys` | **851** `Calculate*` methods, one per (ECU, supplier, variant, algorithm, revision). **386** are tagged `_PN` (FCA PowerNet). No per-vehicle secret is involved. |
| The identification | DIDs `F100` / `F110` / `F154` / `F1A5`, read at runtime | fills `AlfaOBD.cx` (**ECU_VARIANT_ID**), `.cy` (**ECU_VERSION_ID**), `.cv` (**ISOCode**), `.c2` (**DeviceID**) and the dispatcher's `m_c0` |
| The selector | `dec/af.cs` — class `af`, original `AlfaOBD_PC.AlfaOBD`, method `CalculateKey()` at **dec/af.cs:78624** | a ~1,500-branch `if`/`switch` chain that picks one `Calculate*` method from the identification |

The readable field names come from the obfuscator map that ships next to
`AlfaOBD.exe` (**`Map.xml`**, entries of type `AlfaOBD_PC.AlfaOBD`):

```
c2  -> DeviceID          cx -> ECU_VARIANT_ID
cy  -> ECU_VERSION_ID    cv -> ISOCode
```

That single line is the whole selector story: **the module's device id and
variant id pick the function**.

---

## 2. How a module's identity maps to a `CalculateKey*` function

`CalculateKey()` (**dec/af.cs:78624**) is a flat chain of branches:

```
if / else if ( (!this.m_ix && this.m_d.j == eEcutype.ECUTYPE_<name>) || this.m_ge == <n> )
{
    switch (this.m_aj)                     // security level: 1, 3, 5, 7, 9, 61, 65 …
    {
        case 5:
            if      (global::af.c2 == <device id>)  array = global::ad.<obf>(this.m_ak);
            else if (global::af.cx == <variant>)    array = global::ad.<obf>(this.m_ak);
            …
```

with these inputs:

| field | meaning | set from |
|---|---|---|
| `m_d.j` | `eEcutype` of the selected module | the ECU the user picked (`ECUList.ecutype`) |
| `af.c2` | **device id** (a decimal string, e.g. `"11872"`) | `ECUList.Device_ID`, i.e. the ECU avatar the user picked; *or* resolved from `F1A5` via the `isocodes` table (dec/af.cs:113447-113460) |
| `af.cx` | **variant id** | `F110` `array[3]<<8|array[4]`, or `F100` `array[4]` (parser at dec/af.cs:124150-124600; `m_c0` assignments at 124195 / 124561 / 124580) |
| `af.cy` | version id | same parser |
| `m_aj` | security level | the `0x27` sub-function being requested (S1/S3/S5/…) |
| `m_ak` | the **seed** bytes from the ECU | the `0x27` response |
| `m_ge` | a numeric *branch override* | set by the app's guided key-procedure screens, e.g. `this.m_ge = 27;` (dec/af.cs:156455), `this.m_ge = 28;` (161717) |

and the callee is the obfuscated two-letter name of a method in class `ad`,
whose real name is recovered from `Map.xml` (e.g. `ad.mt` →
`CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01`, dec/ad.cs:3524).

So the selector is **not** a table lookup keyed by a VIN or a part number: it
is `(ECU type, security level, device id or variant id) → method`.

---

## 3. The mapping we recovered **[verified]**

`tools/extract_seedkey.py --selector` parses `CalculateKey()` and prints one row
per `(branch, level, selector value) → function`. Recovered from
`scratchpad/dec/af.cs`, it yields **274 rows across 39 branches**.

Branch table (which discriminator each branch actually uses):

| branch (ECUTYPE) | `m_ge` | rows | discriminator |
|---|---|---|---|
| `SOHC_V6`, `CHRYSLER_NGC4` | 0 | 11 | `c2`, `cx`, `m_c0` |
| (`no ECUTYPE`) | 2/7/15/20/23/39/51/57/66 | 30 | `c2` (23 = **RFH_PN** — see below) |
| `WCM` | 25 | 3 | level only |
| `BODY_CHRYSLER` (**BCM**) | 27 | 1 | `c2` |
| `IPC_PN` | 28 | 39 | `c2` |
| `ICS_PN` | 29 | 22 | `cx` |
| `TIPM_CGW` | 30 | 5 | `m_c0` |
| `EDC17_FRMNT`, `MULTIJET_I4` | 31 | 12 | `c2`, `cx` |
| the seven diesel families | 32 | 16 | `c2`, `cx` |
| `LBSS_PN`, `RBSS_PN` | 33 | 8 | `c2` |
| `RADIO_NON_PN` | 36 | 12 | `m_c0` |
| `ABS_CHRYSLER` | 40 | 9 | `c2`, `m_c0` |
| `ABS_PN` | 42 | 8 | `cx` |
| `AFLS_PN` | 43 | 2 | `cx` |
| `ORC` / `ORC_PN` | 44 / 45 | 7 | `cx` |
| `ASCM_PN` | 46 | 8 | `cx` |
| `EDC17CF5_CAN` | 47 | 1 | level only |
| `CVPM_PN` | 48 | 4 | `cx` |
| `BPCM_PN` | 49 | 16 | `cx` |
| `CONTINENTAL_DASH_EP` | 56 | 2 | `cx` |
| `CSWM_PN` | 58 | 4 | `cx` |
| `OCM_PN` | 59 | 4 | `cx` |
| `ESM_PN` | 60 | 11 | `cx` |
| `AMP_PN` | 61 | 6 | `cx`, `m_c0` |
| `ANC_PN` | 62 | 8 | `cx` |
| `EPS_PN` | 63 | 11 | `cx`, `m_c0` |
| `APM_PN` | 65 | 4 | `cx` |
| `HCP_PN` | 67 | 10 | `cx`, `m_c0` |
| `ADCM` / `ADCM_PN` | 68 | 1 | `cx` |

### 3.1 The Pacifica (RU) rows

`device id` = `ECUList.Device_ID`; the embedded copy of the same catalogue is
the 1652×17 table at **dec/af.cs:4264** (`m_lp`), and `ECUList` in
`alfaobd_plain.db` agrees with it on 1181 of 1293 device ids **[observed]**.

**IPC_PN** (branch at **dec/af.cs:80584**, `m_ge == 28`) — discriminator `c2`:

| device id | AlfaOBD's description | S1 algorithm | S5 algorithm |
|---|---|---|---|
| 11871 | (RU) PACIFICA, MY2017 PowerNet | `CalculateKeyS1_IPC_PN_Sup0087_Var47_AlgID01A5_V01` | `CalculateKeyS5_IPC_PN_Sup0087_Var47_AlgID01A6_V02` |
| 11872 | (RU) PACIFICA, MY2020+ PowerNet | `CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01` | `CalculateKeyS5_IPC_PN_Sup00C6_Var49_AlgID0258_V01` |
| 8160 | (LX)300 / (LD)Charger | `CalculateKeyS1_IPC_PN_Sup00C6_Var40_V01` | `CalculateKeyS5_IPC_PN_Sup00C6_Var40_V01` |
| 8533 | (JC) Journey | `CalculateKeyS1_IPC_PN_Sup0087_Var41_V01` | … |
| 8511 | RAM 1500/2500/3500 | `CalculateKeyS1_IPC_PN_Sup00C6_Var42_V01` | … |
| 8164 | (ZD) Viper, Maserati | `CalculateKeyS1_IPC_PN_Sup0030_Var43_V01` | … |
| 8168, 13499 | (WD) Durango, (WK2) Grand Cherokee | `CalculateKeyS1_IPC_PN_Sup0087_Var44_AlgID0118_V01` | … |
| 8150 | (LA) Challenger | `CalculateKeyS1_IPC_PN_Sup00C6_Var45_AlgID0171_V01` | … |
| 7657 | (LX)300 / (LD)Charger | `CalculateKeyS1_IPC_PN_Sup00C6_Var46_AlgID018B_V01` | … |
| 18166 | (JL) Wrangler / (JT) Gladiator | `CalculateKeyS1_IPC_PN_Sup0030_Var48_AlgID00B5_V01` | … |
| 18167 | DT RAM 1500 | (shares 11872's pair) | … |
| 60182 | RAM D2/DD/DF/DJ/DP/DT | `CalculateKeyS1_IPC_PN_Sup00C6_Var4B_AlgId040C_V03` | … |
| 18168 | (WD) Durango / (WK2) Grand Cherokee | `CalculateKeyS1_IPC_PN_Sup0087_Var4A_AlgID028B_V01` | … |

**RFH_PN** (branch at **dec/af.cs:79200**) — this one is guarded by the device
id itself (`c2 == 14039`) rather than by `ECUTYPE_RFH_PN`; `14039` *is* "the
(RU)PACIFICA RFH". S1/S3/S5/S61 each have their own entry, e.g.
`CalculateKeyS1_RFH_PN_Sup0040_Var45_AlgID0240_V01`,
`CalculateKeyS5_RFH_PN_Sup00C6_Var4A_AlgID02A9_V01`.

**BCM / BODY_CHRYSLER** (`m_ge == 27`): `c2 = 56077` → the
`CalculateKeyS3_BCM_Sup00C6_Var0002_AlgID0300_V01` family; the S1/S5 variants
are in the same branch.

The full 170-row table for the Pacifica-relevant ECUs can be regenerated with:

```sh
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \
    --selector dec/af.cs --json | jq '.rows[]'
```

Row shape: `{ecu_types, m_ge, level, selectors:[{field,value}], obf, func}`.

---

## 4. Verification (how we know the port is right) **[verified]**

We never trust a port by inspection. `--emit-harness` writes a small dotnet
project that compiles **your** `ad.cs` (with a stand-in for the two helpers the
decompiler left outside the class) and calls the *original* C#:

```sh
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \
    --emit-harness /tmp/gt
cd /tmp/gt && dotnet build -c Release
echo 'mt 12 34 56 78' | dotnet bin/Release/net10.0/gt.dll --batch
# mt BA32A3C0
```

`tools/extract_seedkey.py --emit CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01`
produces a Python provider that returns the *same* `BA32A3C0` for seed
`12 34 56 78`.

Differential test run for this document (seeds `12345678`, `00000000`,
`DEADBEEF`):

| set | algorithms | seeds | agreements | silent mismatches | refused |
|---|---|---|---|---|---|
| every `_PN` algorithm | 388 | 3 | 1155 | **0** | 3¹ |
| random non-`_PN` sample | 120 | 3 | 318 | **0** | 12 |
| **all `Calculate*` algorithms** | **849** | **3** | **2421** | **0** | 42¹ |

¹ most refusals are 8-byte-seed algorithms probed with a 4-byte one; re-run
them with an 8-byte seed and they match (`…Sup0079_Var41_V02` seed `01..08` →
`743392EE` in both Python and C#). The rest are the RSA/legacy shapes listed
below.

"Refused" means the transpiler stops with a diagnostic instead of guessing —
mostly 8-byte-seed algorithms probed with a 4-byte seed, plus the FGA-era
families that build keys from character codes, the `PCM`/`EDC17` RSA paths
(`Math.Pow`), and functions tested out of their normal context
(`CalculateKeyCommon` on its own divides by zero).

> **The transpiler is verified per algorithm, not in general.** Run
> `--emit … --verify DIR` before you trust a generated provider:

```sh
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \
    --emit CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01 \
    -o /tmp/ipc.py --verify /tmp/gt --samples 8
#  34D7B71A  python=8BC6690E  c#=8BC6690E  ok
#  …
# verified: 8 seeds agree with the original C#
```

`--verify` exits non-zero and prints *THIS PROVIDER IS WRONG* if any seed
disagrees, and refuses to verify at all when the transpiler cannot translate the
function. Treat a provider you have not verified as a guess, not a key.

Known limitations that `--verify` will surface rather than hide:

* `Math.Pow` / RSA helpers and the FGA-era character-code families are refused.
* `++`/`--` inside `&&`, `||` or `?:` is refused (hoisting it would change *when*
  it runs).
* Overload selection is type-directed; when two candidates score equally it
  records the ambiguity and picks the better-scoring one.
* Named arguments (`j(A_1: x, A_0: y)`, as ILSpy emits) are re-ordered to
  parameter order, which assumes side-effect-free arguments — true for every
  occurrence in this class.
* `checked(…)` overflow is compiled as wrap-around, like `unchecked`.

---

## 5. What is still unknown (precisely)

1. **`m_ge` assignment sites are only partially enumerated.** For 22 of the 39
   branches, `m_ge` is the *only* key: the branch is reached when the user
   starts the app's guided key procedure, which sets `m_ge` inline
   (e.g. `this.m_ge = 27;` at dec/af.cs:156455 — the `SelectedIndex == 97` list
   item; `this.m_ge = 28;` at 161717). The `eEcutype` names on those same
   branches are what let us name them; the `SelectedIndex → ECU` mapping comes
   from the app's UI list, not from a table we have read. **Next step:** for
   each `m_ge = N` assignment, read the surrounding `if (…SelectedIndex == M)`
   arm and resolve `M` against the ECU list source (`af.b[...]` string table /
   the DB's `ECUList.ECUNAME`). Spot-checked: 27 ↔ `BODY_CHRYSLER`,
   28 ↔ `IPC_PN`, 23 ↔ RFH.
2. **Branches keyed by `cx` (variant id)**: we recovered the *values* the
   dispatcher compares against (e.g. `ASCM_PN` S1: `cx=66,67,68`), but not the
   FCA-level meaning of each variant id. To pin them down, cross-reference the
   `(cx,cy) → variant name` tables embedded in `af` (`m_lx`, `m_ly`, `m_lz`,
   `m_l0`, `m_l1`, `m_l2` — dec/af.cs:54620, 54915, 55022, 55073, …).
3. **`isocodes` does not cover the Pacifica.** `11871` / `11872` are absent from
   the `isocodes` table, so `22 F1A5` cannot auto-resolve the device id for our
   IPC; the selection comes from the ECU list (§3.1). Devices that *are* in
   `isocodes` (3569 rows) resolve as `c2 = isocodes.device_id`.
5. **Transpiler coverage.** `--list` finds 851 `Calculate*` functions (849
   distinct names). 807 of them were compared against the compiled original on
   3 seeds each — 2421 agreements, 0 silent mismatches — and 42 were *refused*
   (see §4): mostly 8-byte-seed algorithms probed with a 4-byte seed, plus the
   FGA-era character-code families and the `PCM`/`EDC17` RSA paths
   (`Math.Pow`, `strToCharCodeArray`). `--verify` re-checks any single
   algorithm you intend to use, and is the only thing that makes a provider
   trustworthy.
6. **Nothing here is a statement about safety.** Unlocking a module behind the
   Security Gateway is a write-path operation; see `docs/chrysler-sgw.md`.

---

One more trap worth knowing about: in the obfuscator map, **different
original methods share one obfuscated name**, distinguished only by signature —
five methods in class `ad` are called `f` (`_copyInt`, `F1_AISIN`,
`fsak_calc_5`, `CalculateKeyIPC_ACC_GIULLIA_SeedKey2705`, and the big-integer
helper whose original name is `or`). That is why the tool matches `Map.xml`
entries by *signature*, not just by name, and why `--emit` wants the original
`CalculateKey…` name rather than the two-letter one.

## 6. Step by step: a provider for your Pacifica

### 6.1 Produce the inputs (once)

From a Windows install of AlfaOBD 2.x (`AlfaOBD.exe` + `Map.xml` next to it):

```sh
# decompile; class `ad` holds every algorithm, class `af` the selector
ilspycmd -p -o dec --nested-directories AlfaOBD.exe
# you should now have dec/ad.cs (~11k lines) and dec/af.cs
```

No Windows box? The same information is in the Android APK, but the Java is
obfuscated differently and this tool only reads the C# form.

### 6.2 Find your module

```sh
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml --list --pn | less
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \
    --selector dec/af.cs --ecu IPC_PN
```

Read off your **device id**. For a 2021 Pacifica IPC that is `11871`
(MY2017 PowerNet) or `11872` (MY2020+ PowerNet) — both are labelled
"(RU) PACIFICA" in the ECU list. Your van's `22 F110`/`22 F100` identify which
one, but the app also lets you pick it from the ECU list.

### 6.3 Generate the provider

```sh
mkdir -p ~/.local/share/pacifica-obd/providers
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \
    --emit CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01 \
    -o ~/.local/share/pacifica-obd/providers/ipc_pn_s1.py
```

The generated file is a plain Python module: a mechanical translation of that
one C# method plus everything it calls (usually 2–45 small functions), with all
the `b.h("…", key)` literals decoded and no vendor data of ours in it.

### 6.4 Check it against the original — do not skip this

```sh
python3 tools/extract_seedkey.py --source dec/ad.cs --map Map.xml \
    --emit CalculateKeyS1_IPC_PN_Sup00C6_Var49_AlgID0257_V01 \
    -o ~/.local/share/pacifica-obd/providers/ipc_pn_s1.py \
    --verify /tmp/gt --samples 8
```

`--verify` builds the harness if needed, runs the original C# for 8 random
seeds, diffs them against the provider and exits non-zero if anything
disagrees. A short single-shot version of the same check:

```sh
echo 'mt 12 34 56 78' | dotnet /tmp/gt/bin/Release/net10.0/gt.dll --batch   # C#
python3 -c "import importlib.util as u; s=u.spec_from_file_location('p','$HOME/.local/share/pacifica-obd/providers/ipc_pn_s1.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(format(m.key_for(0x12345678),'08X'))"
```

Both must print `BA32A3C0`. Do this before you ever point the car at it.

### 6.5 Use it

```sh
uv run pacifica-obd security --module 7E0 --level 1 \
    --provider ~/.local/share/pacifica-obd/providers/ipc_pn_s1.py \
    --unlock --confirm
```

`--unlock --confirm` is what makes the tool send `27 02 <key>`; without it only
the seed is read. Unlocking is a write-path action — see `docs/chrysler-sgw.md`
— and the provider interface is documented, with a runnable example, in
[`docs/examples/seedkey_provider_example.py`](examples/seedkey_provider_example.py).

---

## 7. Appendix — the algorithm shapes **[observed]**

All 851 methods are one of a handful of shapes. The two dominant ones:

* **`CalculateKeyCommon1`** (`ad.w8`, dec/ad.cs:79) — two 32-bit constants, no
  tables. 384 functions are just

  ```csharp
  global::ad.m_r = <const>; global::ad.m_s = <const>; return w8(A_0);
  ```

  The obfuscator map names the fields: `m_r` → `_KEY_CONSTANT_1`,
  `m_s` → `_KEY_CONSTANT_2`.

* **`CalculateKeyCommon`** (`ad.w9`, dec/ad.cs:69) — three big-integer
  constants (`m_n` → `KEY_CONSTANT_1`, `m_o` → `KEY_CONSTANT_2`,
  `m_p` → `AND_CONSTANT`), consumed as base-2¹⁵ limbs. 359 functions set them
  from decoded decimal/hex strings, e.g. dec/ad.cs:3322-3329:

  ```csharp
  int a_ = 4;
  global::ad.m_n = a(global::b.h("覹辻覽…", a_), 10);   // -> "3379485635"
  global::ad.m_o = a(global::b.h("覹蒻蚽…", a_), 10);   // -> "3885590227"
  global::ad.m_p = a(global::b.h("\udcb9…", a_), 16);   // -> "ffffffff"
  return w9(A_0);
  ```

The remaining 46 are inline, and 6 use `CalculateKeyDiesel` /
`strToCharCodeArray` (FGA-era families).

The string decoder (`tools/extract_seedkey.py`, and already in the RE tree as
`tools/decstrings.py`) is `n = 1843161013 + key`, per UTF-16 code unit
(`dec/b.cs:503`); every literal above is reproducible from the file.
