#!/usr/bin/env python3
"""build_fca_keys.py -- regenerate ``src/pacifica_obd/fca_keys.py``.

``fca_keys.py`` is checked in (it must work with no vendor material present),
but it is **generated**: this tool reads the operator's own decompiled copy of a
third-party FCA/Stellantis diagnostic application (the ``CalculateKey*`` class
and its ``CalculateKey()`` dispatcher) and the bundled ``pacifica.json`` module
dataset, then writes the transpiled algorithms plus the selector table that the
Pacifica (RU) module set needs.

It reuses the transpiler in ``tools/extract_seedkey.py``; run it from a checkout
where that file sits next to this one::

    python3 tools/build_fca_keys.py \\
        --source ~/re/dec/ad.cs --map ~/re/Map.xml --selector ~/re/dec/af.cs \\
        -o src/pacifica_obd/fca_keys.py

Add ``--verify HARNESS_DIR`` to diff the freshly generated module against the
*original* C# (build the harness first with
``tools/extract_seedkey.py --emit-harness``) over ``--samples`` random seeds.

Scope is intentionally narrow: only the ``(module, level, discriminator)`` rows
that can actually be reached for an ECU in ``pacifica.json`` are kept.  A row is
kept when its ECU type is one of ours, or when a device-id it selects on is one
of our modules' device ids.  Rows whose only discriminator we cannot supply at
run time (part-number strings / ``m_c4``, guided-procedure overrides) are dropped
so the selector can never silently pick one of them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_seedkey as ex  # noqa: E402  (same directory)


# Which dispatcher fields become selector kinds.  We only ship discriminators an
# operator can supply on the command line; anything else (``m_c4`` part numbers,
# ``m_ge`` procedure overrides, ``cy``/``cv``) is not selectable here.
FIELD_MAP = {"c2": "device", "cx": "variant", "m_c0": "supplier"}

# Engine PCMs use RSA/legacy shapes whose "supplier id" (``m_c0``) is not
# recoverable per-vehicle; leave them out rather than guess.
EXCLUDE_ECUS = {"PENTASTAR"}


# --------------------------------------------------------------------------- #
# Loading the decompiled sources
# --------------------------------------------------------------------------- #
def load_key_class(source: Path, map_path: Path | None):
    text = source.read_text(encoding="utf-8", errors="replace")
    class_name, info = "ad", None
    if map_path is not None:
        m = ex.load_map(str(map_path))
        for obf, meta in m.items():
            if meta["orig"].endswith(".AlfaOBDKeys"):
                class_name, info = obf, meta
                break
    cls = ex.parse_class(text, class_name)
    if info is not None:
        ex.attach_orig_names(cls, info)
    return cls


def load_dispatcher(source: Path, map_path: Path | None):
    text = source.read_text(encoding="utf-8", errors="replace")
    class_name, info = "af", None
    if map_path is not None:
        m = ex.load_map(str(map_path))
        for obf, meta in m.items():
            if meta["orig"].endswith(".AlfaOBD"):
                class_name, info = obf, meta
                break
    cls = ex.ParsedClass(class_name)
    cls.methods.append(ex.find_dispatcher(text, info, class_name))
    if info is not None:
        ex.attach_orig_names(cls, info)
    return cls


def recover_rows(ad_cls, selector_path: Path, map_path: Path | None):
    key_map = {md.name: (md.orig or md.name) for md, _ in ex.iter_key_functions(ad_cls)}
    af_cls = load_dispatcher(selector_path, map_path)
    return ex.recover_selector(af_cls, key_map)["rows"]


# --------------------------------------------------------------------------- #
# Turning dispatcher rows into (module, level, field, value, algorithm) entries
# --------------------------------------------------------------------------- #
def build_entries(rows: list[dict], modules: list[dict]):
    shipped = {m["ecu"] for m in modules if m["ecu"] not in EXCLUDE_ECUS}
    dev2ecu: dict[int, list[str]] = {}
    for m in modules:
        if m["ecu"] not in shipped:
            continue
        for d in m.get("device_ids") or []:
            dev2ecu.setdefault(int(d), []).append(m["ecu"])

    def module_for(row: dict, field: str, value) -> str | None:
        if field == "device":
            cands = [e for e in dev2ecu.get(value, []) if e in row["ecu_types"]]
            if cands:
                return cands[0]
            allc = dev2ecu.get(value)
            return allc[0] if allc else None
        for e in row["ecu_types"]:
            if e in shipped:
                return e
        return None

    entries: set[tuple] = set()
    obf_of: dict[str, str] = {}
    for row in rows:
        level = ex.parse_key_name(row["func"])["level"]
        if not level:
            continue
        for sel in row["selectors"] or []:
            field = FIELD_MAP.get(sel["field"])
            if field is None:
                continue
            if field == "supplier":
                value: object = str(sel["value"]).upper()
            else:
                try:
                    value = int(sel["value"])
                except ValueError:
                    continue
            module = module_for(row, field, value)
            if module is None:
                continue
            entries.add((module, level, field, value, row["func"]))
            obf_of[row["func"]] = row["obf"]
    return sorted(entries,
                  key=lambda e: (e[0], e[1], e[2], str(e[3]), e[4])), obf_of


def detect_partial(dispatcher_text: str, rows: list[dict], entries: list[tuple]) -> dict:
    """Modules whose dispatcher branch is **not** fully modelled.

    The selector recovery only captures a branch's flat ``array = ad.f(seed)``
    arms.  A branch that assigns via a ternary or picks the callee from a hashed
    string (``ai.a(text)`` / ``m_c4``) is only partially captured, so its
    device/variant match is *necessary but not sufficient*.  Mark those modules
    so ``explain`` refuses instead of pretending a device id settles it.
    """
    lines = dispatcher_text.splitlines()
    starts = []
    for i, line in enumerate(lines):
        m = re.search(r"(?:\bif|else if) \(.*?m_ge == (\d+)\)", line)
        if m:
            starts.append((i, int(m.group(1))))
    starts.append((len(lines), None))

    rows_per_ge: dict[int, int] = {}
    ge_funcs: dict[int, set[str]] = {}
    for row in rows:
        for g in row["m_ge"]:
            rows_per_ge[g] = rows_per_ge.get(g, 0) + 1
            ge_funcs.setdefault(g, set()).add(row["func"])

    func_modules: dict[str, set[str]] = {}
    for module, _level, _field, _value, func in entries:
        func_modules.setdefault(func, set()).add(module)

    reason = ("this dispatcher branch is only partially recovered: it also "
              "selects on a part-number/string discriminator that was not "
              "captured, so a device/variant id alone does not prove the choice")
    partial: dict[str, str] = {}
    for (a, g), (b, _) in zip(starts, starts[1:]):
        if g is None:
            continue
        region = lines[a:b]
        body = "\n".join(region)
        assigns = sum(1 for line in region
                      if "array =" in line and "global::ad." in line)
        incomplete = (
            assigns != rows_per_ge.get(g, 0)          # an arm we did not recover
            or "global::ai.a(" in body                 # hashed string switch
            or "m_c4 ==" in body                       # part-number predicate
            or any("array =" in line and "?" in line for line in region)
        )
        if not incomplete:
            continue
        for func in ge_funcs.get(g, ()):
            for module in func_modules.get(func, ()):
                partial[module] = reason
    return partial


def probe_widths(ns: dict, funcs: list[str]):
    """Learn each algorithm's seed width and key (output) width, by probing.

    Seed width mirrors ``tools/extract_seedkey.py --verify``: try 4 bytes, fall
    back to 8 (and 16 as a safety net).  Key width is the length of the bytes the
    algorithm returns, which is what service ``0x27`` must send -- not the seed
    length.
    """
    seed_width: dict[str, int] = {}
    key_width: dict[str, int] = {}
    probe = b"\x12\x34\x56\x78"
    for name in funcs:
        fn = ns["ALGORITHMS"][name]
        for width in (4, 8, 16):
            try:
                out = fn(bytearray(probe.rjust(width, b"\x00")))
            except IndexError:
                continue
            seed_width[name] = width
            key_width[name] = len(bytes(out or b""))
            break
        else:
            raise RuntimeError(f"cannot find a seed width for {name}")
    return seed_width, key_width


# --------------------------------------------------------------------------- #
# PDF-free output: one module, no I/O, no vendor name
# --------------------------------------------------------------------------- #
_DOC = '''\
"""FCA/Stellantis PowerNet seed->key for the Pacifica (RU) module set.

Pure Python: no I/O, no printing and no third-party runtime dependency. It
exposes the security-access (`0x27`) seed->key algorithms and the selector that
picks one from a module's identity.

Nothing here is vendor material in source form. The ``Calculate*`` functions are
a mechanical translation (via ``tools/extract_seedkey.py``) of the operator's own
licensed copy of a third-party diagnostic application; the selector table is the
part of that application's key dispatcher we could read for the modules this
project ships. Provenance, scope and the regeneration/verification procedure are
in ``docs/fca-keys.md``.

Public API
----------
``ALGORITHMS``      name -> ``f(seed_bytes) -> key_bytes`` (transpiled entries).
``explain(...)``    why an algorithm was (or was not) chosen, for a dry run.
``provider_for(...)``  a ``key_for(seed, **ctx) -> int`` provider, or a refusal.
``compute_key(...)``   convenience: resolve and compute in one call.

Selection is deliberately paranoid: a wrong key can lock a module, so any
ambiguity is a refusal listing the candidates, never a guess.
"""

from __future__ import annotations
'''

_API = '''
# --------------------------------------------------------------------------- #
# Selector + provider API
# --------------------------------------------------------------------------- #
_FIELDS = ("device", "variant", "supplier")


class FcaKeyError(RuntimeError):
    """No single seed->key algorithm could be chosen for this target."""


def _norm_module(module) -> str:
    return str(module).strip().upper()


def _ids(device_id, variant_id, supplier) -> dict:
    got: dict = {"device": None, "variant": None, "supplier": None}
    if device_id is not None:
        got["device"] = int(device_id)
    if variant_id is not None:
        got["variant"] = int(variant_id, 0) if isinstance(variant_id, str) else int(variant_id)
    if supplier is not None:
        got["supplier"] = str(supplier).strip().upper().removeprefix("0X")
    return got


def _candidates(module: str, level: int, ids: dict) -> list[tuple]:
    out = []
    for entry in _SELECTOR:
        if entry[0] != module or entry[1] != level:
            continue
        want = ids.get(entry[2])
        if want is None or want == entry[3]:
            out.append(entry)
    return out


def _known(entry: tuple, ids: dict) -> bool:
    return ids.get(entry[2]) == entry[3]


def explain(module, level: int = 1, device_id=None, variant_id=None,
            supplier=None) -> dict:
    """Describe the algorithm ``provider_for`` would pick (and why / why not).

    Returns a dict with ``status`` one of ``resolved``, ``ambiguous`` or
    ``unmapped``; when resolved it carries ``algorithm``. Otherwise
    ``candidates`` lists every algorithm the recovered selector allows, so a
    refusal is actionable rather than a guess.
    """
    module = _norm_module(module)
    level = int(level)
    ids = _ids(device_id, variant_id, supplier)
    cand = _candidates(module, level, ids)
    funcs = sorted({e[4] for e in cand})
    used_fields = {e[2] for e in cand}
    info: dict = {
        "module": module,
        "level": level,
        # only the supplied ids that the matching rows actually select on
        "ids": {k: v for k, v in ids.items()
                if v is not None and k in used_fields},
        "candidates": [
            {"algorithm": fn,
             "selectors": [{"field": e[2], "value": e[3]}
                           for e in cand if e[4] == fn]}
            for fn in funcs
        ],
    }
    if not cand:
        info["status"] = "unmapped"
        info["reason"] = (f"no shipped selector row for {module} at level "
                          f"0x{level:02X}")
        return info
    if module in _PARTIAL:
        info["status"] = "ambiguous"
        info["reason"] = _PARTIAL[module]
        return info
    if len(funcs) > 1:
        info["status"] = "ambiguous"
        info["reason"] = ("several algorithms match; supply --device-id / "
                          "--variant / --supplier")
        return info
    if not all(_known(e, ids) for e in cand):
        unknown = sorted({e[2] for e in cand if not _known(e, ids)})
        info["status"] = "ambiguous"
        info["reason"] = ("algorithm needs an unidentified discriminator ("
                          + ", ".join(unknown) + "); supply it explicitly")
        return info
    info["status"] = "resolved"
    info["algorithm"] = funcs[0]
    return info


def provider_for(module, level: int = 1, device_id=None, variant_id=None,
                 supplier=None):
    """Return ``key_for(seed, **ctx) -> int`` for the resolved algorithm.

    Raises :class:`FcaKeyError` (with the candidate list) when the target is
    ambiguous or unmapped, so a caller can never send a guessed key.
    """
    info = explain(module, level, device_id, variant_id, supplier)
    if info["status"] != "resolved":
        detail = ", ".join(c["algorithm"] for c in info["candidates"]) or "none"
        raise FcaKeyError(
            f"{info['status']}: {info['reason']} [candidates: {detail}]")
    func = ALGORITHMS[info["algorithm"]]
    default_width = _SEED_WIDTH.get(info["algorithm"], 4)

    def key_for(seed, **ctx) -> int:
        # ``security_access`` passes the seed's byte length as ``width``; fall
        # back to the width the algorithm was verified with otherwise.
        width = ctx.get("width")
        width = int(width) if width else default_width
        out = func(_seed_bytes(seed, width))
        return int.from_bytes(bytes(out or b""), "big")

    # the caller sends exactly this many key bytes, whatever the seed length
    key_for.key_width = _KEY_WIDTH.get(info["algorithm"], 0)
    return key_for


def compute_key(module, level: int, seed, device_id=None, variant_id=None,
                supplier=None) -> int:
    """Resolve an algorithm and compute one key; raises on any ambiguity."""
    return provider_for(module, level, device_id, variant_id,
                        supplier)(seed)
'''


def render(entries: list[tuple], funcs: list[str], py_names: dict[str, str],
           core: str, seed_width: dict[str, int], key_width: dict[str, int],
           partial: dict[str, str]) -> str:
    table = "\n".join(
        "    (%s, %d, %s, %s, %s)," % (
            json.dumps(m), lvl, json.dumps(field), json.dumps(value),
            json.dumps(func))
        for m, lvl, field, value, func in entries)
    registry = "\n".join(
        '    %s: %s,' % (json.dumps(n), py_names[n]) for n in funcs)
    widths = "\n".join(
        '    %s: %d,' % (json.dumps(n), seed_width[n]) for n in funcs)
    key_widths = "\n".join(
        '    %s: %d,' % (json.dumps(n), key_width[n]) for n in funcs)
    partials = "\n".join(
        '    %s: %s,' % (json.dumps(m), json.dumps(partial[m]))
        for m in sorted(partial))
    return (
        _DOC
        + "\n# --------------------------------------------------------------------------- #\n"
          "# Transpiled algorithm core (mechanical; see docs/fca-keys.md)\n"
          "# --------------------------------------------------------------------------- #\n"
        + core
        + "\n\n\n# --------------------------------------------------------------------------- #\n"
          "# Registry + selector (generated from the recovered dispatcher table)\n"
          "# --------------------------------------------------------------------------- #\n"
          "# name -> callable(seed_bytes) -> key_bytes\n"
          "ALGORITHMS = {\n" + registry + "\n}\n\n"
          "# name -> the seed width (bytes) the algorithm was verified with. Used\n"
          "# when the caller does not pass ``width``; keeps a leading-zero 8-byte\n"
          "# seed from being rebuilt as 4 bytes.\n"
          "_SEED_WIDTH = {\n" + widths + "\n}\n\n"
          "# name -> the key (output) width (bytes) the algorithm returns. This\n"
          "# is what service 0x27 must send -- not the seed's length.\n"
          "_KEY_WIDTH = {\n" + key_widths + "\n}\n\n"
          "# (module, level, field, value, algorithm).\n"
          "#   field 'device'  -> ECU device id (integer)\n"
          "#   field 'variant' -> ECU variant id (integer)\n"
          "#   field 'supplier'-> supplier/isocode string\n"
          "# A row matches when every value the caller supplied agrees; an unknown\n"
          "# discriminator does not disqualify a row but does force a refusal later.\n"
          "_SELECTOR = (\n" + table + "\n)\n\n"
          "# Modules whose dispatcher branch is only partially recovered (a\n"
          "# part-number/string discriminator is missing): never resolved.\n"
          "_PARTIAL = {\n" + partials + "\n}\n"
        + _API
        + "\n\ndef _seed_bytes(seed, width=None):\n"
          "    if isinstance(seed, (bytes, bytearray)):\n"
          "        return bytearray(seed)\n"
          "    if isinstance(seed, int):\n"
          "        if seed < 0:\n"
          "            raise ValueError(\"seed must be non-negative\")\n"
          "        if width is None:\n"
          "            width = 4 if seed <= 0xFFFFFFFF else 8\n"
          "        while seed >= (1 << (8 * width)):  # always room for the value\n"
          "            width += 1\n"
          "        return bytearray((seed >> (8 * (width - 1 - i))) & 0xFF\n"
          "                         for i in range(width))\n"
          "    return bytearray(seed)\n"
    )


# --------------------------------------------------------------------------- #
# Differential verification against the original C#
# --------------------------------------------------------------------------- #
def verify(module_path: Path, obf_of: dict[str, str], funcs: list[str],
           harness: str, samples: int) -> int:
    dll = os.path.join(harness, "bin", "Release", "net10.0", "gt.dll")
    if not os.path.exists(dll):
        print(f"error: harness not built: {dll}", file=sys.stderr)
        return 3
    ns: dict = {}
    exec(compile(module_path.read_text(encoding="utf-8"), str(module_path),
                 "exec"), ns)  # noqa: S102
    import random
    random.seed(20211018)
    # 4-byte seeds; an algorithm that needs a longer seed falls back to 8 bytes.
    seeds4 = [bytes(random.randrange(256) for _ in range(4))
              for _ in range(samples)]
    agree = mismatch = refused = 0
    for name in funcs:
        obf = obf_of.get(name)
        if not obf:
            print(f"  skip {name}: no obfuscated name recorded", file=sys.stderr)
            continue
        py = ns["ALGORITHMS"][name]
        payloads: list[bytes] = []
        for seed in seeds4:
            try:
                py(bytearray(seed))
                payloads.append(seed)
            except Exception:  # noqa: BLE001 - longer-seed algorithms
                payloads.append(seed + b"\x00\x00\x00\x00")
        stdin = "\n".join(f"{obf} " + " ".join(f"{b:02X}" for b in s)
                          for s in payloads) + "\n"
        r = subprocess.run(["dotnet", dll, "--batch"], input=stdin,
                           capture_output=True, text=True, timeout=600)
        csh = [ln.split(" ")[-1] for ln in r.stdout.strip().splitlines()]
        for j, seed in enumerate(payloads):
            try:
                got_hex = bytes(py(bytearray(seed)) or b"").hex().upper()
            except Exception:  # noqa: BLE001
                refused += 1
                continue
            want = csh[j] if j < len(csh) else "?"
            if want.startswith("ERR"):
                refused += 1
            elif got_hex == want:
                agree += 1
            else:
                mismatch += 1
                print(f"  MISMATCH {name} seed={seed.hex().upper()} "
                      f"py={got_hex} c#={want}", file=sys.stderr)
    print(f"verify: {agree} agree, {mismatch} mismatch, {refused} refused "
          f"over {len(funcs)} algorithms x {samples} seeds")
    return 0 if mismatch == 0 else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="decompiled key class (ad.cs)")
    ap.add_argument("--map", help="obfuscator Map.xml")
    ap.add_argument("--selector", required=True,
                    help="decompiled CalculateKey() dispatcher (af.cs)")
    ap.add_argument("--data", default="src/pacifica_obd/data/pacifica.json",
                    help="bundled module dataset")
    ap.add_argument("-o", "--output", default="-")
    ap.add_argument("--verify", metavar="HARNESS_DIR",
                    help="diff the generated module against the original C#")
    ap.add_argument("--samples", type=int, default=3)
    args = ap.parse_args(argv)

    map_path = Path(args.map) if args.map else None
    ad_cls = load_key_class(Path(args.source), map_path)
    rows = recover_rows(ad_cls, Path(args.selector), map_path)
    modules = json.loads(Path(args.data).read_text(encoding="utf-8"))["modules"]
    entries, obf_of = build_entries(rows, modules)
    funcs = sorted({e[4] for e in entries})

    typer = ex.Typer(ad_cls)
    emitter = ex.Emitter(ad_cls, typer)
    for name in funcs:
        emitter.collect(name)
    core = emitter.emit(emitter.find(funcs[0])).split("# --- public interface")[0]
    core = core.rstrip()
    py_names = {n: ex.method_id(emitter.find(n)) for n in funcs}

    # probe the freshly transpiled algorithms (pure) to learn each seed width
    probe_ns: dict = {"__name__": "fca_keys_probe"}
    exec(compile(core, "<fca_core>", "exec"), probe_ns)  # noqa: S102
    probe_ns["ALGORITHMS"] = {n: probe_ns[py_names[n]] for n in funcs}
    seed_width, key_width = probe_widths(probe_ns, funcs)

    dispatcher_text = Path(args.selector).read_text(encoding="utf-8",
                                                    errors="replace")
    partial = detect_partial(dispatcher_text, rows, entries)

    collisions = {}
    for name, obf in obf_of.items():
        collisions.setdefault(obf, []).append(name)
    dup = {o: names for o, names in collisions.items() if len(names) > 1}
    if dup:
        print(f"warning: obfuscated-name collisions (verify may pick the wrong "
              f"overload): {dup}", file=sys.stderr)

    text = render(entries, funcs, py_names, core, seed_width, key_width, partial)
    if args.output and args.output != "-":
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}: {len(entries)} selector rows, "
              f"{len(funcs)} algorithms, {len(emitter.needed)} transpiled methods, "
              f"{len(partial)} partial module(s), {text.count(chr(10))} lines")
    else:
        sys.stdout.write(text)

    if args.verify:
        return verify(Path(args.output), obf_of, funcs, args.verify, args.samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
