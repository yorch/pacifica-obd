// Component factories. Each is a small function returning a DOM node, styled by
// a class in app.css. Everything renders untrusted bus values as text nodes.
//
// Two rules the whole file follows:
//   1. Nothing here ever builds markup, so a value off the CAN bus cannot inject
//      anything. `h()` only sets text nodes.
//   2. State is never colour alone: every pill, badge and error carries a word,
//      and the icons are inline SVG at 1.5px stroke (no emoji, no webfont).

import { h, clear, mount, on } from "./dom.js";
import { numberText, statusClass, escapeControls, valueText } from "./format.js";

/* ------------------------------------------------------------------ icons */

// 24x24, stroke-only, `currentColor`. Path-only so `h()` stays a whitelist.
const ICONS = {
  table: "M4 5h16v14H4zM4 10h16M10 10v9",
  layers: "M12 3l9 5-9 5-9-5 9-5M3 13l9 5 9-5",
  tag: "M20 12l-8 8-9-9V3h8l9 9zM7.5 7.5h.01",
  gauge: "M12 13.5l3.5-3.5M5.5 19a8.5 8.5 0 1 1 13 0",
  activity: "M3 12h4l3-7 4 14 3-7h4",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v9H5z",
  pencil: "M4 20h4l11-11-4-4L4 16v4zM14 5l4 4",
  sparkles:
    "M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3z"
    + "M18.5 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2z",
  download: "M12 4v11M8 11l4 4 4-4M4 19h16",
  database:
    "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z"
    + "M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  file: "M6 3h8l4 4v14H6zM14 3v4h4M9 13h6M9 17h4",
  terminal: "M5 6l6 6-6 6M13 18h6",
  sliders: "M4 8h10M18 8h2M4 16h4M12 16h8M16 5v6M8 13v6",
  help:
    "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z"
    + "M9.6 9.2a2.4 2.4 0 1 1 3 2.3c-.6.2-1 .8-1 1.4v.6M12 17h.01",
  check: "M4 12.5l5 5L20 6.5",
  "check-circle": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12l3 3 5-6",
  "alert-triangle": "M12 4l9 16H3l9-16zM12 10v4M12 17h.01",
  "alert-octagon": "M8 3h8l5 5v8l-5 5H8l-5-5V8l5-5zM12 8v5M12 16h.01",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01",
  shield: "M12 3l7 3v6c0 4.4-2.9 7.4-7 9-4.1-1.6-7-4.6-7-9V6l7-3z",
  "shield-check": "M12 3l7 3v6c0 4.4-2.9 7.4-7 9-4.1-1.6-7-4.6-7-9V6l7-3zM9 12l2 2 4-4",
  "shield-alert": "M12 3l7 3v6c0 4.4-2.9 7.4-7 9-4.1-1.6-7-4.6-7-9V6l7-3zM12 8.5v4.5M12 16h.01",
  ban: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM5.6 5.6l12.8 12.8",
  copy: "M9 9h10v10H9zM5 15V5h10",
  "chevron-right": "M9 6l6 6-6 6",
  "chevron-left": "M15 6l-6 6 6 6",
  plug: "M9 3v6M15 3v6M7 9h10v3a5 5 0 0 1-10 0zM12 17v4",
  x: "M6 6l12 12M18 6L6 18",
  circle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
  dot: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z",
};

// Views were written against a small set of unicode glyphs for empty states;
// map them onto the SVG set so the emoji never reach a screenshot.
const GLYPH_ICON = {
  "🔌": "plug", "⌗": "layers", "▦": "database", "▤": "table", "◎": "gauge",
  "◉": "gauge", "✓": "check", "»": "terminal", "⤓": "download", "⎙": "file",
  "⚙": "sliders", "⧉": "copy", "✦": "sparkles", "?": "help", "#": "table",
};

export function icon(name, { size = 16, className = "" } = {}) {
  const key = GLYPH_ICON[name] || name;
  const d = ICONS[key] || ICONS.circle;
  return h("svg", {
    class: `icon ${className}`.trim(), viewBox: "0 0 24 24",
    width: size, height: size, fill: "none", stroke: "currentColor",
    "stroke-width": 1.5, "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false",
  }, h("path", { d }));
}

/* -------------------------------------------------------------- containers */

export function card({ title, eyebrow, meta, actions, flush = false, body = null } = {}) {
  const head = (title || eyebrow || actions || meta)
    ? h("div.card__head", null,
        h("div", null,
          eyebrow ? h("span.eyebrow", { text: eyebrow }) : null,
          title ? h("h2", { text: title }) : null),
        meta ? h("span.sub", { text: meta }) : null,
        actions ? h("div.card__actions", null, actions) : null)
    : null;
  const node = h("section.card", null, head,
    h("div", { class: `card__body${flush ? " card__body--flush" : ""}` }));
  if (body) node.querySelector(".card__body").appendChild(body);
  return node;
}

export function cardBody(node) { return node.querySelector(".card__body"); }

export function pill(text, status) {
  return h("span", { class: `pill ${statusClass(status)}`, text: String(text ?? "—"),
                     title: status ? `status: ${status}` : "" });
}

// The raw console's tier badge. Bigger than a status pill and always worded, so
// read / confirm / deny can never be confused, in a screenshot or at a glance.
export function tierPill(tier) {
  const word = tier === "invalid" ? "deny" : tier;
  const glyph = word === "read" ? "check" : word === "confirm" ? "alert-triangle" : "ban";
  const cls = word === "read" ? "tier--read" : word === "confirm" ? "tier--confirm" : "tier--deny";
  return h("span", { class: `pill pill--tier ${cls}`, title: `tier: ${word}` },
    icon(glyph, { size: 12 }), h("span", { text: word }));
}

export function untrustedBadge(note) {
  return h("span.untrusted", { title: note || "This value came off the vehicle's CAN bus and is untrusted input." },
    icon("shield-alert", { size: 10 }), h("span", { text: "vehicle data" }));
}

export function kpi({ label, value, unit, foot, sparkline: spark }) {
  const empty = value === null || value === undefined || value === "";
  return h("div.kpi", null,
    h("div.kpi__label", { text: label }),
    empty
      ? h("div.kpi__value.kpi__value--empty", { text: "Not read" })
      : h("div.kpi__value", null, valueText(value),
          unit ? h("span.kpi__unit", { text: unit }) : null),
    foot || spark
      ? h("div.kpi__foot", null,
          spark ? h("span.kpi__spark", null, spark) : null, foot || null)
      : null);
}

const BANNER_ICON = { info: "info", ok: "check-circle", warn: "alert-triangle",
                      crit: "alert-octagon", err: "alert-octagon" };

export function banner(text, kind = "info", actions = null) {
  return h("div", { class: `banner banner--${kind}` },
    icon(BANNER_ICON[kind] || "info"),
    h("span", { text }), actions || null);
}

export function emptyState({ icon: glyph = "circle", title, hint, action } = {}) {
  return h("div.empty", null,
    h("div.empty__icon", { "aria-hidden": "true" }, icon(glyph, { size: 20 })),
    title ? h("div.empty__title", { text: title }) : null,
    hint ? h("p.sub", { text: hint }) : null,
    action || null);
}

export function loadingRows(rows = 5, { title = true } = {}) {
  const wrap = h("div.skeleton-stack", { "aria-busy": "true", "aria-live": "polite" },
    h("span.sr-only", { text: "Loading" }));
  if (title) wrap.appendChild(h("div.skeleton.skeleton--title"));
  for (let i = 0; i < rows; i += 1) wrap.appendChild(h("div.skeleton.skeleton--row"));
  return wrap;
}

export function errorState(error, { onRetry } = {}) {
  const code = error?.code || "error";
  const message = error?.message || String(error);
  const hint = error?.detail?.hint;
  const http = error?.httpStatus;
  const recoverable = typeof http === "number" && http >= 400 && http < 500;
  return h("div", { class: `error-state${recoverable ? " error-state--recoverable" : ""}` },
    h("div.error-state__head", null,
      icon(recoverable ? "alert-triangle" : "alert-octagon"),
      h("span.error-state__title", { text: recoverable ? "Request did not complete" : "Request failed" })),
    h("div.row", null,
      h("span.error-state__code", { text: code }),
      http ? h("span.sub", { text: `HTTP ${http}` }) : null,
      h("button.cellcopy", { type: "button", title: "Copy the error code",
        onClick: () => navigator.clipboard?.writeText(`${code}: ${message}`) },
        icon("copy", { size: 13 }))),
    h("div.error-state__msg", { text: escapeControls(message) }),
    hint ? h("div.sub", { text: `hint: ${escapeControls(String(hint))}` }) : null,
    onRetry ? h("div.row", null,
      h("button.btn.btn--sm", { type: "button", onClick: onRetry, text: "Retry" })) : null);
}

export function spinner() { return h("span.spinner", { "aria-label": "loading" }); }

/* ------------------------------------------------------------------ tables */

export function dataTable({ columns, rows, rowClass: rowClassFn, onRowClick,
                            empty = "No rows.", emptyTitle = null } = {}) {
  const thead = h("thead", null, h("tr", null,
    columns.map((col) => {
      const th = h("th", {
        class: col.num ? "num" : "",
        "aria-sort": col.sortable
          ? (col.sorted === "asc" ? "ascending" : col.sorted === "desc" ? "descending" : "none")
          : undefined,
      });
      if (col.sortable) {
        th.appendChild(h("button.th-sort", {
          type: "button", onClick: (event) => { event.stopPropagation(); col.onSort?.(); },
        }, h("span", { text: col.label })));
      } else {
        th.textContent = col.label;
      }
      return th;
    })));
  const tbody = h("tbody");
  if (!rows.length) {
    tbody.appendChild(h("tr", null,
      h("td", { colspan: columns.length },
        h("div.empty", null,
          emptyTitle ? h("div.empty__title", { text: emptyTitle }) : null,
          h("p.sub", { text: empty })))));
  }
  for (const row of rows) {
    const tr = h("tr", { class: [rowClassFn ? rowClassFn(row) : "",
                                  onRowClick ? "clickable" : ""].join(" ").trim() });
    for (const col of columns) {
      const value = col.render ? col.render(row) : row[col.key];
      const cell = h("td", { class: [col.num ? "num" : "", col.mono ? "mono" : ""].join(" ").trim() });
      if (value instanceof Node) cell.appendChild(value);
      else cell.textContent = escapeControls(valueText(value));
      tr.appendChild(cell);
    }
    if (onRowClick) {
      tr.tabIndex = 0;
      tr.setAttribute("role", "button");
      on(tr, "click", () => onRowClick(row));
      on(tr, "keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onRowClick(row); }
      });
    }
    tbody.appendChild(tr);
  }
  // A bare scroller: the surrounding card already provides the surface, so a
  // table never nests a second bordered, shadowed box inside a card.
  return h("div.table-wrap", null, h("table.data", null, thead, tbody));
}

export function cellWithCopy(text, { mono = true } = {}) {
  const span = h("span", { class: mono ? "mono" : "", text: escapeControls(valueText(text)) });
  const button = h("button.cellcopy", {
    type: "button", title: "Copy", "aria-label": "Copy this value",
    onClick: (event) => {
      event.stopPropagation();
      navigator.clipboard?.writeText(String(text ?? ""));
    },
  }, icon("copy", { size: 13 }));
  return h("span", null, span, " ", button);
}

/* ------------------------------------------------------------------ gauges */

function polar(cx, cy, r, deg) {
  const rad = ((deg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

function arcPath(cx, cy, r, start, end) {
  const [x1, y1] = polar(cx, cy, r, start);
  const [x2, y2] = polar(cx, cy, r, end);
  const large = Math.abs(end - start) > 180 ? 1 : 0;
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

// A radial gauge: a scale (ticks, min/max labels), an optional redline band and
// a value arc — no needle, the number is the largest thing on the instrument.
// The node carries `.update(props)` so a live stream mutates the arc and the
// number in place (the transition runs, the digit never reflows) instead of
// rebuilding the SVG on every sample.
export function gaugeRadial(props = {}) {
  const {
    label = "", value = null, unit = "", min = 0, max = 100,
    warnAt = null, redlineFrom = null, size = "md", status = "ok", stale = false,
  } = props;
  const px = size === "sm" ? 118 : size === "lg" ? 208 : 158;
  const cx = px / 2;
  const cy = px / 2 + (size === "sm" ? 2 : 6);
  const r = px / 2 - (size === "sm" ? 20 : 26);
  const band = size === "sm" ? 7 : size === "lg" ? 11 : 9;
  const START = 225;
  const SWEEP = 270;
  const end = START + SWEEP;
  const length = (r * SWEEP * Math.PI) / 180;
  const outer = r + band / 2 + 3;
  let state = { ...props };
  const frac = (v) => {
    const value_ = Number(v);
    const lo = Number(state.min ?? 0);
    const hi = Number(state.max ?? 100);
    if (!Number.isFinite(value_) || !Number.isFinite(lo) || !Number.isFinite(hi) || hi === lo) return 0;
    return Math.max(0, Math.min(1, (value_ - lo) / (hi - lo)));
  };
  const at = (v) => START + SWEEP * frac(v);

  const zone = [];
  const warnFrom = warnAt === null || warnAt === undefined ? null : warnAt;
  const critFrom = redlineFrom === null || redlineFrom === undefined ? null : redlineFrom;
  if (warnFrom !== null) {
    const warnEnd = at(critFrom !== null ? critFrom : max);
    zone.push(
      h("path", { class: "gauge__zone", d: arcPath(cx, cy, r, at(warnFrom), warnEnd), "stroke-width": band }),
      // on the outer ring too, so the value arc cannot cover it
      h("path", { class: "gauge__zone__mark", d: arcPath(cx, cy, outer, at(warnFrom), warnEnd), "stroke-width": 3 }),
    );
  }
  if (critFrom !== null) {
    zone.push(
      h("path", { class: "gauge__redzone", d: arcPath(cx, cy, r, at(critFrom), end), "stroke-width": band }),
      h("path", { class: "gauge__redline", d: arcPath(cx, cy, outer, at(critFrom), end), "stroke-width": 3 }),
    );
  }

  const ticks = [];
  for (let i = 0; i <= 20; i += 1) {
    const degree = START + (SWEEP * i) / 20;
    const major = i % 4 === 0;
    const [x1, y1] = polar(cx, cy, r - band / 2 - 3, degree);
    const [x2, y2] = polar(cx, cy, r - band / 2 - (major ? 9 : 6), degree);
    ticks.push(h("line", { class: major ? "gauge__tick gauge__tick--major" : "gauge__tick",
      x1, y1, x2, y2, "stroke-width": major ? 1.5 : 1 }));
  }

  const valueArc = h("path", {
    class: "gauge__value", d: arcPath(cx, cy, r, START, end), "stroke-width": band,
    "stroke-dasharray": String(length.toFixed(2)),
    "stroke-dashoffset": String((length * (1 - frac(value))).toFixed(2)),
  });

  const number = h("text", { class: "gauge__num gauge__center", x: cx, y: cy + 10,
    text: value === null ? "—" : numberText(value) });
  // The unit sits *above* the number so the arc ends at the bottom stay clear
  // for the scale labels; the first real sample may fill it in place.
  const unitText = h("text", { class: "gauge__unit gauge__center", x: cx, y: cy - 22,
    text: unit || "" });
  // Scale ends sit *inside* the arc, so they never collide with the ticks or
  // the redline band outside it.
  const labelR = r - band - 12;
  const [minX, minY] = polar(cx, cy, labelR, START + 5);
  const [maxX, maxY] = polar(cx, cy, labelR, START + SWEEP - 5);
  const minLabel = h("text", { class: "gauge__scale", x: minX, y: minY + 8,
    "text-anchor": "middle", text: numberText(min) });
  const maxLabel = h("text", { class: "gauge__scale", x: maxX, y: maxY + 8,
    "text-anchor": "middle", text: numberText(max) });

  const svg = h("svg", { viewBox: `0 0 ${px} ${px}`, role: "img",
    "aria-label": `${label}: ${value === null ? "no data" : numberText(value)} ${unit}` },
    h("path", { class: "gauge__track", d: arcPath(cx, cy, r, START, end), "stroke-width": band }),
    zone, ...ticks, valueArc, number, unitText, minLabel, maxLabel);

  const statusPill = h("span");
  const labelText = h("span", { text: label });
  const node = h("div", { class: "gauge gauge--" + size });

  node.update = (next = {}) => {
    state = { ...state, ...next };
    const v = state.value;
    node.className = "gauge gauge--" + (state.size || size);
    valueArc.setAttribute("stroke-dashoffset", String((length * (1 - frac(v))).toFixed(2)));
    const empty = v === null || v === undefined || !Number.isFinite(Number(v));
    number.textContent = empty ? "—" : numberText(v);
    number.setAttribute("class", `gauge__num gauge__center${empty ? " gauge__num--empty" : ""}`);
    if (unitText) unitText.textContent = state.unit || "";
    svg.setAttribute("aria-label",
      `${state.label || label}: ${empty ? "no data" : numberText(v)} ${state.unit || ""}`);
    const crit = state.redlineFrom !== null && state.redlineFrom !== undefined
      && !empty && Number(v) >= state.redlineFrom;
    const warn = state.warnAt !== null && state.warnAt !== undefined
      && !empty && Number(v) >= state.warnAt;
    if (empty) node.classList.add("gauge--idle");
    else if (crit) node.classList.add("gauge--crit");
    else if (warn) node.classList.add("gauge--warn");
    if (state.stale) node.classList.add("gauge--stale");
    const pillText = state.status && state.status !== "ok" && empty ? state.status : "";
    statusPill.className = pillText ? `pill ${statusClass(pillText)}` : "";
    statusPill.textContent = pillText;
    labelText.textContent = state.label || label;
    return node;
  };
  node.appendChild(svg);
  node.appendChild(h("div.gauge__label", null, labelText, statusPill));
  node.update({ value, unit, label, warnAt, redlineFrom, status, stale });
  return node;
}

export function gaugeBar(props = {}) {
  const base = { label: "", value: null, unit: "", min: 0, max: 100,
                 warnAt: null, redlineFrom: null, stale: false, ...props };
  const node = h("div", { class: "bar" });
  const labelText = h("span.bar__label", { text: base.label });
  const valueText = h("span.bar__val");
  const fill = h("div.bar__fill");
  const track = h("div.bar__track", { role: "meter", "aria-label": base.label }, fill);
  node.appendChild(h("div.bar__top", null, labelText, valueText));
  node.appendChild(track);

  let state = { ...base };
  node.update = (next = {}) => {
    state = { ...state, ...next };
    const v = state.value;
    const mn = state.min ?? 0;
    const mx = state.max ?? 100;
    const finite = v !== null && v !== undefined && Number.isFinite(Number(v));
    const fraction = !finite || mx === mn
      ? 0 : Math.max(0, Math.min(1, (Number(v) - mn) / (mx - mn)));
    const crit = finite && state.redlineFrom !== null && state.redlineFrom !== undefined
      && Number(v) >= state.redlineFrom;
    const warn = finite && state.warnAt !== null && state.warnAt !== undefined
      && Number(v) >= state.warnAt;
    node.className = `bar${state.stale ? " bar--stale" : ""}`;
    fill.className = `bar__fill${crit ? " bar__fill--crit" : warn ? " bar__fill--warn" : ""}`;
    fill.style.width = `${(fraction * 100).toFixed(1)}%`;
    track.setAttribute("aria-label", state.label || "");
    track.setAttribute("aria-valuenow", String(finite ? v : 0));
    track.setAttribute("aria-valuemin", String(mn));
    track.setAttribute("aria-valuemax", String(mx));
    valueText.textContent = finite ? numberText(v) : "—";
    const unitNode = valueText.querySelector(".kpi__unit");
    if (state.unit) {
      if (unitNode) unitNode.textContent = state.unit;
      else valueText.appendChild(h("span.kpi__unit", { text: state.unit }));
    } else if (unitNode) unitNode.remove();
    return node;
  };
  node.update(base);
  return node;
}

export function sparkline(values, { width = 72, height = 26, colour = "var(--accent)" } = {}) {
  const points = (values || []).filter((v) => typeof v === "number" && Number.isFinite(v));
  const svg = h("svg.sparkline", { viewBox: `0 0 ${width} ${height}`, role: "presentation",
    preserveAspectRatio: "none", "aria-hidden": "true", focusable: "false" });
  if (points.length < 2) return svg;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = width / (points.length - 1);
  const at = (v, i) => [i * step, height - ((v - min) / span) * (height - 6) - 3];
  const d = points.map((v, i) => `${i === 0 ? "M" : "L"} ${at(v, i)[0].toFixed(1)} ${at(v, i)[1].toFixed(1)}`).join(" ");
  const last = at(points[points.length - 1], points.length - 1);
  svg.appendChild(h("path", { d, fill: "none", stroke: colour, "stroke-width": 1.5,
    "vector-effect": "non-scaling-stroke",
    "stroke-linejoin": "round", "stroke-linecap": "round" }));
  svg.appendChild(h("circle", { cx: last[0].toFixed(1), cy: last[1].toFixed(1), r: 1.8,
    fill: colour, "vector-effect": "non-scaling-stroke" }));
  return svg;
}

export function digitalReadout({ label = "", value = null, unit = "", stale = false,
                                 status = "ok", history = null } = {}) {
  const spark = history && history.length > 1 ? sparkline(history, { width: 120, height: 22 }) : null;
  return h("div", { class: `digital${stale ? " bar--stale" : ""}` },
    h("div.digital__label", { text: label }),
    h("div", null,
      h("span.digital__num", { text: value === null ? "—" : numberText(value) }),
      unit ? h("span.digital__unit", { text: unit }) : null,
      status !== "ok" ? h("span", { class: `pill ${statusClass(status)}`, style: { marginLeft: "6px" }, text: status }) : null),
    spark);
}

/* -------------------------------------------------- overlays (trap focus) */

const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

// A focus trap + Esc for any overlay: Tab cycles inside the panel, focus returns
// to whatever opened it, and Escape closes. Shared by the modal and the drawer so
// neither can leak keystrokes into the page behind it.
function trapFocus(panel, close) {
  const opener = document.activeElement;
  const onKey = (event) => {
    if (event.key === "Escape") { event.preventDefault(); close(); return; }
    if (event.key !== "Tab") return;
    const items = [...panel.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  document.addEventListener("keydown", onKey);
  const first = panel.querySelector(FOCUSABLE);
  if (first) first.focus();
  return () => {
    document.removeEventListener("keydown", onKey);
    if (opener instanceof HTMLElement && document.contains(opener)) opener.focus();
  };
}

/* ------------------------------------------------------------------ drawer */

export function drawer({ title, content, onClose }) {
  const backdrop = h("div.drawer-backdrop", { role: "dialog", "aria-modal": "true",
    "aria-label": title });
  let released = () => {};
  const close = () => {
    released();
    backdrop.remove();
    if (onClose) onClose();
  };
  const panel = h("div.drawer", null,
    h("div.drawer__bar", null,
      h("h2", { text: title }),
      h("button.btn.btn--sm.drawer__close", { type: "button", text: "Close", onClick: close })),
    content);
  backdrop.appendChild(panel);
  on(backdrop, "click", (event) => { if (event.target === backdrop) close(); });
  document.body.appendChild(backdrop);
  released = trapFocus(panel, close);
  return { close };
}

/* ------------------------------------------------------------------ toasts */

const TOAST_ICON = { ok: "check-circle", err: "alert-octagon", warn: "alert-triangle", info: "info" };

export function toast(message, kind = "ok", timeout = 4000) {
  const region = document.getElementById("toasts");
  if (!region) return;
  const node = h("div", { class: `toast toast--${kind}`, role: "status" },
    icon(TOAST_ICON[kind] || "info"), h("span", { text: escapeControls(message) }));
  region.appendChild(node);
  if (timeout) {
    const leave = Math.max(0, timeout - 220);
    setTimeout(() => {
      node.dataset.leaving = "";
      setTimeout(() => node.remove(), 240);
    }, leave);
  }
  return node;
}

/* ------------------------------------------------------------ form pieces */

export function field({ label, value = "", placeholder = "", mono = false, type = "text",
                        id = null, min = null, max = null, hint = null } = {}) {
  return h("div.field", null,
    h("label", { for: id || undefined, text: label }),
    h("input", { id: id || undefined, class: mono ? "mono" : "", type, value, placeholder, min, max }),
    hint ? h("div.field__hint", { text: hint }) : null);
}

export function select({ label, options, value, id = null }) {
  const node = h("select", { id: id || undefined, class: "mono" },
    options.map((opt) => h("option", {
      value: opt.value, text: opt.label, selected: String(opt.value) === String(value) ? true : undefined,
    })));
  return h("div.field", null, label ? h("label", { for: id || undefined, text: label }) : null, node);
}

export function checkbox({ label, checked = false, id = null }) {
  return h("div.field.field--check", null,
    h("input", { id: id || undefined, type: "checkbox", checked: checked ? true : undefined }),
    h("label", { for: id || undefined, text: label }));
}

export function progressBar() {
  const fill = h("div.progress__fill");
  const node = h("div.progress", { role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100,
    "aria-valuenow": 0, "aria-label": "progress" }, fill);
  node.update = (fraction) => {
    const pct = Math.max(0, Math.min(1, fraction)) * 100;
    fill.style.width = `${pct}%`;
    node.setAttribute("aria-valuenow", String(Math.round(pct)));
  };
  return node;
}

/* ------------------------------------------------------------------ modal */

let dialogSeq = 0;

// A focus-trapped modal: Esc closes, the backdrop closes, Tab cycles inside the
// panel, and focus returns to whatever opened it. Content is built from nodes
// (never markup), so bus data can safely be shown inside.
export function modal({ title, content, onClose, actions = null, danger = false } = {}) {
  const titleId = `po-dialog-title-${(dialogSeq += 1)}`;
  const backdrop = h("div.modal-backdrop", { role: danger ? "alertdialog" : "dialog",
    "aria-modal": "true", "aria-labelledby": titleId });
  let closed = false;
  let released = () => {};
  const close = () => {
    if (closed) return;
    closed = true;
    released();
    backdrop.remove();
    if (onClose) onClose();
  };
  const panel = h("div", { class: `modal${danger ? " modal--danger" : ""}` },
    h("div.modal__head", null, h("h2", { id: titleId, text: title })),
    h("div.modal__body", null, content),
    h("div.modal__actions", null,
      h("button.btn", { type: "button", text: "Cancel", onClick: close }),
      actions || null));
  backdrop.appendChild(panel);
  on(backdrop, "click", (event) => { if (event.target === backdrop) close(); });
  document.body.appendChild(backdrop);
  released = trapFocus(panel, close);
  return { close };
}

// The typed-acknowledgement confirm. The only solid red button in the app lives
// here: the risk is stated, the exact wire bytes are shown, and the commit stays
// disabled until the acknowledgement matches character for character.
export function typedConfirm({ title, request, module, warning, hint, onConfirm, submitLabel = "Send" }) {
  const seq = (dialogSeq += 1);
  const inputId = `confirm-ack-${seq}`;
  const stateId = `confirm-state-${seq}`;
  let sent = false;
  const input = h("input.mono", {
    type: "text", autocomplete: "off", autocapitalize: "characters",
    spellcheck: "false", placeholder: module,
    "aria-label": `type ${module} to confirm`, "aria-describedby": stateId,
  });
  const state = h("div.dialog__ack__state", { id: stateId, "data-match": "empty", "aria-live": "polite" });
  const button = h("button.btn.btn--commit", { type: "button", disabled: true,
    text: submitLabel, onClick: () => {
      if (sent || button.disabled) return;
      sent = true;
      // Keep focus on the button rather than disabling it under the caret: a
      // disabled node drops focus to <body> and the trap stops matching.
      button.setAttribute("aria-disabled", "true");
      onConfirm(closeModal);
    } });
  let lastMatch = null;
  const update = () => {
    const typed = input.value.trim().toUpperCase();
    const expected = String(module).toUpperCase();
    const match = typed === expected;
    button.disabled = !match || sent;
    const mode = match ? "yes" : typed ? "no" : "empty";
    input.setAttribute("aria-invalid", mode === "no" ? "true" : "false");
    if (mode === lastMatch) return; // do not re-announce on every keystroke
    lastMatch = mode;
    state.dataset.match = mode;
    clear(state);
    state.appendChild(icon(match ? "check-circle" : mode === "no" ? "alert-triangle" : "circle", { size: 13 }));
    state.appendChild(h("span", { text: match
      ? `matches ${module} — the ${submitLabel.toLowerCase()} button is armed`
      : mode === "no"
        ? `does not match ${module}; keep typing`
        : `type ${module} exactly to arm this button` }));
  };
  on(input, "input", update);
  on(input, "keydown", (event) => {
    // Enter moves focus to the commit button; it never sends in one keystroke.
    if (event.key === "Enter" && !button.disabled) { event.preventDefault(); button.focus(); }
  });
  const content = h("div", null,
    warning ? h("div.banner.banner--warn", null, icon("alert-triangle"), h("span", { text: warning })) : null,
    h("div.modal__request", null,
      h("span.eyebrow", { text: "exact wire request — nothing has been sent" }),
      h("code.mono.dialog__wire", { text: request })),
    h("p.sub", { text: hint || `Type ${module} to confirm. This tool cannot undo the change.` }),
    h("div.field", null,
      h("label", { for: inputId, text: `Acknowledgement (module id ${module})` }),
      input),
    state);
  const handle = modal({ title, content, danger: true, actions: button });
  const closeModal = handle.close;
  input.id = inputId;
  update();
  input.focus();
  return handle;
}

export { clear, mount, h };
