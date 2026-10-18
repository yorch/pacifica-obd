"""Vehicle dump export: JSON, XML, and a self-contained HTML report.

`build_dump` layers produce a plain dict; the renderers here turn that dict into
the three formats. Nothing here touches hardware, and the HTML is fully
self-contained (embedded CSS, no external assets) and HTML-escaped so arbitrary
string content from a vehicle cannot inject markup.
"""

from __future__ import annotations

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

_TAG_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_XML_BAD = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd]")


def dump_to_json(dump: dict) -> str:
    return json.dumps(dump, indent=2, default=str)


# --------------------------------------------------------------------------- #
# XML
# --------------------------------------------------------------------------- #
def _tag(name: str) -> str:
    tag = _TAG_SAFE.sub("_", str(name)) or "item"
    if not re.match(r"[A-Za-z_]", tag):  # XML names cannot start with - . or a digit
        tag = "_" + tag
    return tag


def _xml_add(parent: ET.Element, name: str, value) -> None:
    element = ET.SubElement(parent, _tag(name))
    if isinstance(value, dict):
        for key, item in value.items():
            _xml_add(element, str(key), item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _xml_add(element, "item", item)
    elif value is not None:
        element.text = _XML_BAD.sub("", str(value))


def dump_to_xml(dump: dict) -> str:
    root = ET.Element("vehicle-dump")
    for key, value in dump.items():
        _xml_add(root, str(key), value)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


# --------------------------------------------------------------------------- #
# HTML (self-contained, attractive)
# --------------------------------------------------------------------------- #
_CSS = """
:root{--bg:#0f1420;--panel:#171d2b;--ink:#e7ecf5;--mut:#9aa7bd;--line:#26304a;
--acc:#5b8cff;--ok:#37c98a;--warn:#ffb454;--bad:#ff6b6b;--chip:#212b40}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#0c1120,#0f1420 320px);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--acc)}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
header.hero{background:radial-gradient(1200px 300px at 10% -40%,#1d2b52,transparent),var(--panel);
border:1px solid var(--line);border-radius:16px;padding:26px 24px}
h1{margin:0 0 6px;font-size:26px;letter-spacing:.2px}
h2{margin:34px 0 12px;font-size:20px;border-bottom:1px solid var(--line);padding-bottom:8px}
h3{margin:18px 0 8px;font-size:16px;color:var(--mut)}
.sub{color:var(--mut);font-size:13px}
.vin{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:16px;color:#fff}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:150px;flex:1}
.card .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
table{width:100%;border-collapse:collapse;margin:10px 0 6px;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
tr:hover td{background:#141b2a}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);color:var(--mut);font-size:12px;margin:0 4px 4px 0}
.chip.ok{color:#bff5dc;background:#153226}.chip.bad{color:#ffd9d9;background:#3a1a1a}
.chip.warn{color:#ffe6bf;background:#3a2c12}
.dtc{font-family:ui-monospace,Menlo,monospace;font-weight:600;color:#fff}
details{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:10px 0;padding:6px 14px}
summary{cursor:pointer;padding:8px 0;font-weight:600}
summary .mut{color:var(--mut);font-weight:400}
.empty{color:var(--mut);font-style:italic}
footer{margin-top:40px;color:var(--mut);font-size:12px;text-align:center}
.none{color:var(--ok)}
"""


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _dtc_table(dtcs: list[dict]) -> str:
    if not dtcs:
        return '<p class="empty none">No trouble codes.</p>'
    rows = []
    for dtc in dtcs:
        if not isinstance(dtc, dict):
            continue
        flags = dtc.get("flags") or []
        if not isinstance(flags, list):
            flags = [flags]
        chips = "".join(f'<span class="chip {"bad" if f in ("testFailed","confirmed","warningLamp") else ""}">{_esc(f)}</span>' for f in flags)
        ftb = f"-{_esc(dtc['failure_type'])}" if dtc.get("failure_type") else ""
        rows.append(
            f"<tr><td class='dtc'>{_esc(dtc.get('code'))}{ftb}</td>"
            f"<td>{_esc(dtc.get('description'))}</td>"
            f"<td class='mono'>{_esc(dtc.get('status'))}</td><td>{chips}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Code</th><th>Description</th><th>Status</th><th>Flags</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _simple_dtc_table(dtcs: list[dict]) -> str:
    if not dtcs:
        return '<p class="empty none">None.</p>'
    rows = "".join(
        f"<tr><td class='dtc'>{_esc(d.get('code'))}</td><td>{_esc(d.get('description'))}</td></tr>"
        for d in dtcs
    )
    return "<table><thead><tr><th>Code</th><th>Description</th></tr></thead><tbody>" + rows + "</tbody></table>"


def dump_to_html(dump: dict) -> str:
    dump = dump if isinstance(dump, dict) else {}
    adapter = dump.get("adapter") if isinstance(dump.get("adapter"), dict) else {}
    vehicle = dump.get("vehicle") if isinstance(dump.get("vehicle"), dict) else {}
    generic = dump.get("generic") if isinstance(dump.get("generic"), dict) else {}
    modules = dump.get("modules") if isinstance(dump.get("modules"), list) else []
    readings = dump.get("readings") if isinstance(dump.get("readings"), list) else []
    totals = dump.get("totals") if isinstance(dump.get("totals"), dict) else {}
    tool = dump.get("tool") if isinstance(dump.get("tool"), dict) else {}

    vin = vehicle.get("vin")
    dtc_total = totals.get("dtcs", 0)

    cards = "".join(
        f'<div class="card"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div></div>'
        for k, v in (
            ("Modules", totals.get("modules", len(modules))),
            ("Trouble codes", dtc_total),
            ("Readings", totals.get("readings", len(readings))),
        )
    )

    def kv_table(data: dict) -> str:
        rows = "".join(
            f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in data.items() if v not in (None, "")
        )
        return "<table>" + rows + "</table>" if rows else '<p class="empty">n/a</p>'

    module_blocks = []
    for module in modules:
        if not isinstance(module, dict):
            continue
        bus = _esc(module.get("bus") or "?")
        req = _esc(module.get("request_id"))
        resp = _esc(module.get("response_id"))
        n_dtc = len(module.get("dtcs") or [])
        ident = module.get("identification") or {}
        ident_rows = "".join(
            f"<tr><th>{_esc(v.get('name') or did)}<br><span class='sub mono'>{_esc(did)}</span></th>"
            f"<td class='mono'>{_esc(v.get('hex'))}</td><td>{_esc(v.get('ascii'))}</td>"
            f"<td>{_esc(v.get('status'))}</td></tr>"
            for did, v in ident.items()
        )
        ident_table = (
            "<table><thead><tr><th>Identifier</th><th>Hex</th><th>ASCII</th><th>Status</th></tr></thead>"
            f"<tbody>{ident_rows}</tbody></table>"
            if ident_rows
            else '<p class="empty">No identifiers read.</p>'
        )
        module_blocks.append(
            f"<details><summary>{bus} · {req} → {resp} "
            f"<span class='mut'>({n_dtc} DTC{'' if n_dtc == 1 else 's'})</span></summary>"
            f"<h3>Trouble codes</h3>{_dtc_table(module.get('dtcs') or [])}"
            f"<h3>Identification</h3>{ident_table}</details>"
        )

    readiness = generic.get("readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    monitors = readiness.get("monitors")
    monitors = monitors if isinstance(monitors, list) else []
    monitor_rows = "".join(
        f"<tr><td>{_esc(m.get('name'))}</td><td>"
        f"<span class='chip {'ok' if m.get('complete') else 'warn'}'>"
        f"{'complete' if m.get('complete') else 'incomplete'}</span></td></tr>"
        for m in monitors
    )
    readiness_html = (
        f"<p>Ignition: <b>{_esc(readiness.get('ignition_type'))}</b> · "
        f"MIL: <b>{'ON' if readiness.get('mil') else 'off'}</b> · "
        f"Verdict: <b>{'READY' if readiness.get('ready') else 'NOT READY'}</b></p>"
        f"<table><thead><tr><th>Monitor</th><th>Status</th></tr></thead><tbody>{monitor_rows}</tbody></table>"
        if readiness else '<p class="empty">Not read.</p>'
    )

    live = generic.get("live")
    live = live if isinstance(live, dict) else {}
    live_rows = "".join(f"<tr><th>{_esc(k)}</th><td class='mono'>{_esc(v)}</td></tr>" for k, v in live.items())

    g_dtcs = generic.get("dtcs")
    g_dtcs = g_dtcs if isinstance(g_dtcs, dict) else {}
    title_vin = _esc(vin) if vin else "VIN not available"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vehicle report — {title_vin}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<header class="hero">
  <h1>Vehicle diagnostic report</h1>
  <div class="vin">{title_vin}</div>
  <div class="sub">{_esc(vehicle.get('year')) or ''} {_esc(vehicle.get('make')) or ''} {_esc(vehicle.get('model')) or ''}
    · profile {_esc(vehicle.get('profile_id')) or 'n/a'} · generated {_esc(dump.get('generated_at'))}</div>
  <div class="cards">{cards}</div>
</header>

<h2>Adapter</h2>{kv_table(adapter)}
<h2>Vehicle</h2>{kv_table(vehicle)}

<h2>Generic OBD-II</h2>
<h3>Trouble codes</h3>
<h3 style="margin:0">Stored (mode 03)</h3>{_simple_dtc_table(g_dtcs.get('stored') or [])}
<h3 style="margin:0">Pending (mode 07)</h3>{_simple_dtc_table(g_dtcs.get('pending') or [])}
<h3 style="margin:0">Permanent (mode 0A)</h3>{_simple_dtc_table(g_dtcs.get('permanent') or [])}
<h3>Readiness monitors</h3>{readiness_html}
<h3>Live data (snapshot)</h3>
{"<table>" + live_rows + "</table>" if live_rows else '<p class="empty">Not read.</p>'}

<h2>Modules ({len(modules)})</h2>
{"".join(module_blocks) if module_blocks else '<p class="empty">No UDS modules responded.</p>'}

<footer>Generated by pacifica-obd · {_esc(tool.get('version') or '')}</footer>
</div></body></html>
"""


_FORMATS = {"json": dump_to_json, "xml": dump_to_xml, "html": dump_to_html}
_EXT = {"json": ".json", "xml": ".xml", "html": ".html"}


def write_dump(dump: dict, out_dir: Path | str, formats=("json", "xml", "html"),
               stem: str = "vehicle") -> list[Path]:
    """Write the dump atomically in each requested format; returns the paths."""
    directory = Path(out_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = _TAG_SAFE.sub("_", str(stem)) or "vehicle"
    written: list[Path] = []
    for fmt in formats:
        fmt = str(fmt).strip().lower()
        if fmt not in _FORMATS:
            raise ValueError(f"unknown format: {fmt}")
        path = directory / (safe_stem + _EXT[fmt])
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(_FORMATS[fmt](dump), encoding="utf-8")
        os.replace(tmp, path)  # atomic: never leaves a truncated file
        written.append(path)
    if not written:
        raise ValueError("no formats requested")
    return written
