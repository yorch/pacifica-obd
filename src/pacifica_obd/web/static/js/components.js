// Component factories. Each is a small function returning a DOM node, styled by
// a class in app.css. Everything renders untrusted bus values as text nodes.

import { h, clear, mount, on } from "./dom.js";
import { numberText, statusClass, escapeControls, valueText } from "./format.js";

/* -------------------------------------------------------------- containers */

export function card({ title, eyebrow, actions, flush = false, body = null } = {}) {
  const head = (title || eyebrow || actions)
    ? h("div.card__head", null,
        h("div", null,
          eyebrow ? h("div.eyebrow", { text: eyebrow }) : null,
          title ? h("h2", { text: title }) : null),
        actions ? h("div.card__actions", null, actions) : null)
    : null;
  const node = h("section.card", null, head, h("div", { class: `card__body${flush ? " card__body--flush" : ""}` }));
  if (body) node.querySelector(".card__body").appendChild(body);
  return node;
}

export function cardBody(node) { return node.querySelector(".card__body"); }

export function pill(text, status) {
  return h("span", { class: `pill ${statusClass(status)}`, text: String(text ?? "—"),
                     title: status ? `status: ${status}` : "" });
}

export function untrustedBadge(note) {
  return h("span.untrusted", { text: "vehicle data",
    title: note || "This value came off the vehicle's CAN bus and is untrusted input." });
}

export function kpi({ label, value, unit, foot, sparkline }) {
  return h("div.kpi", null,
    h("div.kpi__label", { text: label }),
    h("div.kpi__value", null, valueText(value),
      unit ? h("span.kpi__unit", { text: unit }) : null),
    foot || sparkline ? h("div.kpi__foot", null, foot, sparkline || null) : null);
}

export function banner(text, kind = "info", actions = null) {
  return h("div", { class: `banner banner--${kind}` }, h("span", { text }), actions || null);
}

export function emptyState({ icon = "◎", title, hint, action } = {}) {
  return h("div.empty", null,
    h("div.empty__icon", { text: icon, "aria-hidden": "true" }),
    title ? h("div.empty__title", { text: title }) : null,
    hint ? h("div.sub", { text: hint }) : null,
    action || null);
}

export function loadingRows(rows = 5) {
  const wrap = h("div", { "aria-busy": "true", "aria-live": "polite" },
    h("span", { class: "sub", text: "Loading…" }));
  for (let i = 0; i < rows; i += 1) wrap.appendChild(h("div.skeleton.skeleton--row"));
  return wrap;
}

export function errorState(error, { onRetry } = {}) {
  const code = error?.code || "error";
  const message = error?.message || String(error);
  const hint = error?.detail?.hint;
  return h("div.error-state", null,
    h("div", null, h("span.error-state__code", { text: code }), " ",
      h("span", { text: escapeControls(message) })),
    error?.status ? h("div.sub", { text: `status: ${error.status}` }) : null,
    hint ? h("div.sub", { text: `hint: ${hint}` }) : null,
    onRetry ? h("div", { class: "row", style: { marginTop: "8px" } },
      h("button.btn.btn--sm", { type: "button", onClick: onRetry, text: "Retry" })) : null);
}

export function spinner() { return h("span.spinner", { "aria-label": "loading" }); }

/* ------------------------------------------------------------------ tables */

export function dataTable({ columns, rows, rowClass: rowClassFn, onRowClick, empty = "No rows." }) {
  const thead = h("thead", null, h("tr", null,
    columns.map((col) => h("th", { class: col.num ? "num" : "", text: col.label }))));
  const tbody = h("tbody");
  if (!rows.length) {
    tbody.appendChild(h("tr", null,
      h("td", { colspan: columns.length, class: "sub", text: empty })));
  }
  for (const row of rows) {
    const tr = h("tr", { class: rowClassFn ? rowClassFn(row) : "" });
    for (const col of columns) {
      const value = col.render ? col.render(row) : row[col.key];
      const cell = h("td", { class: [col.num ? "num" : "", col.mono ? "mono" : ""].join(" ").trim() });
      if (value instanceof Node) cell.appendChild(value);
      else cell.textContent = escapeControls(valueText(value));
      tr.appendChild(cell);
    }
    if (onRowClick) on(tr, "click", () => onRowClick(row));
    tbody.appendChild(tr);
  }
  return h("section.card.card__body--flush", null, h("div.table-wrap", null,
    h("table.data", null, thead, tbody)));
}

export function cellWithCopy(text, { mono = true, max = 64 } = {}) {
  const span = h("span", { class: mono ? "mono" : "", text: escapeControls(valueText(text)) });
  const button = h("button.cellcopy", {
    type: "button", title: "Copy", text: "⧉",
    onClick: (event) => {
      event.stopPropagation();
      navigator.clipboard?.writeText(String(text ?? ""));
    },
  });
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

// A radial gauge: an arc track (with an optional redline band) and a value arc,
// no needle — the number is the largest thing on the instrument (§2.5 rule 1).
export function gaugeRadial({ label = "", value = null, unit = "", min = 0, max = 100,
                              warnAt = null, redlineFrom = null, size = "md", status = "ok",
                              stale = false } = {}) {
  const px = size === "sm" ? 120 : size === "lg" ? 220 : 168;
  const cx = px / 2;
  const cy = px / 2 + (size === "sm" ? 2 : 6);
  const r = px / 2 - (size === "sm" ? 16 : 22);
  const START = 225;
  const SWEEP = 270;
  const end = START + SWEEP;
  const length = (r * SWEEP * Math.PI) / 180;
  const fraction = value === null || value === undefined || max === min
    ? 0 : Math.max(0, Math.min(1, (Number(value) - min) / (max - min)));

  const zone = redlineFrom !== null && redlineFrom !== undefined
    ? h("path", { class: "gauge__redline", d: arcPath(cx, cy, r, START + SWEEP * ((redlineFrom - min) / (max - min)), end),
      "stroke-width": size === "sm" ? 8 : 12 })
    : null;

  const ticks = [];
  for (let i = 0; i <= 5; i += 1) {
    const degree = START + (SWEEP * i) / 5;
    const [x1, y1] = polar(cx, cy, r - 7, degree);
    const [x2, y2] = polar(cx, cy, r - 12, degree);
    ticks.push(h("line", { class: "gauge__tick", x1, y1, x2, y2, "stroke-width": 1.5 }));
  }

  const valueArc = h("path", {
    class: "gauge__value", d: arcPath(cx, cy, r, START, end),
    "stroke-width": size === "sm" ? 8 : 12,
    "stroke-dasharray": String(length.toFixed(2)),
    "stroke-dashoffset": String((length * (1 - fraction)).toFixed(2)),
  });

  const stateClass = value === null ? "gauge--idle"
    : (redlineFrom !== null && redlineFrom !== undefined && Number(value) >= redlineFrom) ? "gauge--crit"
    : (warnAt !== null && warnAt !== undefined && Number(value) >= warnAt) ? "gauge--warn" : "";

  const svg = h("svg", { viewBox: `0 0 ${px} ${px}`, width: px, height: px, role: "img",
    "aria-label": `${label}: ${value === null ? "no data" : numberText(value)} ${unit}` },
    h("path", { class: "gauge__track", d: arcPath(cx, cy, r, START, end), "stroke-width": size === "sm" ? 8 : 12 }),
    zone, ...ticks, valueArc,
    h("text", { class: "gauge__num gauge__center", x: cx, y: cy - 2,
      text: value === null ? "—" : numberText(value) }),
    unit ? h("text", { class: "gauge__unit gauge__center", x: cx, y: cy + 16, text: unit }) : null);

  return h("div", { class: `gauge gauge--${size} ${stateClass}${stale ? " gauge--stale" : ""} ${status === "ok" ? "" : "gauge--idle"}` },
    svg, h("div.gauge__label", null, label, status !== "ok" && value === null
      ? h("span", { class: `pill ${statusClass(status)}`, style: { marginLeft: "6px" }, text: status }) : null));
}

export function gaugeBar({ label = "", value = null, unit = "", min = 0, max = 100,
                           warnAt = null, redlineFrom = null, stale = false } = {}) {
  const fraction = value === null || value === undefined || max === min
    ? 0 : Math.max(0, Math.min(1, (Number(value) - min) / (max - min)));
  const colour = value !== null && redlineFrom !== null && redlineFrom !== undefined
      && Number(value) >= redlineFrom ? "bar__fill--crit"
    : value !== null && warnAt !== null && warnAt !== undefined && Number(value) >= warnAt
      ? "bar__fill--warn" : "";
  return h("div", { class: `bar${stale ? " bar--stale" : ""}` },
    h("div.bar__top", null,
      h("span.bar__label", { text: label }),
      h("span.bar__val", null, value === null ? "—" : numberText(value),
        unit ? h("span.kpi__unit", { text: unit }) : null)),
    h("div.bar__track", { role: "meter", "aria-valuenow": value ?? 0, "aria-valuemin": min, "aria-valuemax": max },
      h("div", { class: `bar__fill ${colour}`, style: { width: `${(fraction * 100).toFixed(1)}%` } })));
}

export function digitalReadout({ label = "", value = null, unit = "", stale = false, status = "ok" } = {}) {
  return h("div", { class: `digital${stale ? " bar--stale" : ""}` },
    h("div.digital__label", { text: label }),
    h("div", null,
      h("span.digital__num", { text: value === null ? "—" : numberText(value) }),
      unit ? h("span.digital__unit", { text: unit }) : null,
      status !== "ok" ? h("span", { class: `pill ${statusClass(status)}`, style: { marginLeft: "6px" }, text: status }) : null));
}

export function sparkline(values, { width = 64, height = 24, colour = "var(--accent)" } = {}) {
  const points = (values || []).filter((v) => typeof v === "number" && Number.isFinite(v));
  if (points.length < 2) return h("svg.sparkline", { width, height, "aria-hidden": "true" });
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = width / (points.length - 1);
  const d = points.map((v, i) => `${i === 0 ? "M" : "L"} ${(i * step).toFixed(1)} ${(height - ((v - min) / span) * (height - 4) - 2).toFixed(1)}`).join(" ");
  return h("svg.sparkline", { width, height, "aria-hidden": "true" },
    h("path", { d, fill: "none", stroke: colour, "stroke-width": 1.5, "stroke-linejoin": "round" }));
}

/* ------------------------------------------------------------------ drawer */

export function drawer({ title, content, onClose }) {  const backdrop = h("div.drawer-backdrop", { role: "dialog", "aria-modal": "true", "aria-label": title });
  const close = () => { backdrop.remove(); document.removeEventListener("keydown", onKey); if (onClose) onClose(); };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  const panel = h("div.drawer", null,
    h("button.btn.btn--ghost.btn--sm.drawer__close", { type: "button", text: "Close", onClick: close }),
    h("h2", { text: title }), content);
  backdrop.appendChild(panel);
  on(backdrop, "click", (event) => { if (event.target === backdrop) close(); });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(backdrop);
  return { close };
}

/* ------------------------------------------------------------------ toasts */

export function toast(message, kind = "ok", timeout = 4000) {
  const region = document.getElementById("toasts");
  if (!region) return;
  const node = h("div", { class: `toast toast--${kind}`, text: escapeControls(message) });
  region.appendChild(node);
  if (timeout) setTimeout(() => node.remove(), timeout);
  return node;
}

export function field({ label, value = "", placeholder = "", mono = false, type = "text", id = null, min = null, max = null }) {
  return h("div.field", null,
    h("label", { for: id || undefined, text: label }),
    h("input", { id: id || undefined, class: mono ? "mono" : "", type, value, placeholder, min, max }));
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
  const fill = h("div.progress__fill", { style: { width: "0%" } });
  const node = h("div.progress", { role: "progressbar" }, fill);
  node.update = (fraction) => { fill.style.width = `${Math.max(0, Math.min(1, fraction)) * 100}%`; };
  return node;
}

/* ------------------------------------------------------------------ modal */

// A focus-trapped modal. Esc closes; the backdrop click closes. Content is
// built from nodes (never markup), so bus data can safely be shown inside.
export function modal({ title, content, onClose, actions = null, danger = false } = {}) {
  const backdrop = h("div.modal-backdrop", { role: "dialog", "aria-modal": "true",
    "aria-label": title });
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    backdrop.remove();
    document.removeEventListener("keydown", onKey);
    if (onClose) onClose();
  };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  const panel = h("div", { class: `modal${danger ? " modal--danger" : ""}` },
    h("div.modal__head", null, h("h2", { text: title })),
    h("div.modal__body", null, content),
    h("div.modal__actions", null,
      h("button.btn.btn--ghost", { type: "button", text: "Cancel", onClick: close }),
      actions || null));
  backdrop.appendChild(panel);
  on(backdrop, "click", (event) => { if (event.target === backdrop) close(); });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(backdrop);
  const focusable = panel.querySelector("input, select, textarea, button");
  if (focusable) focusable.focus();
  return { close };
}

// The typed-acknowledgement confirm: the button stays disabled until the user
// types the module id. This mirrors what a browser makes "practically
// impossible to do by accident" (design §4.5.3).
export function typedConfirm({ title, request, module, warning, hint, onConfirm }) {
  const input = h("input.mono", { type: "text", autocomplete: "off",
    placeholder: module, "aria-label": "type the module id to confirm" });
  const button = h("button.btn.btn--danger", { type: "button", disabled: true,
    text: "Send", onClick: () => { button.disabled = true; onConfirm(closeModal); } });
  const update = () => { button.disabled = input.value.trim().toUpperCase() !== module.toUpperCase(); };
  on(input, "input", update);
  const content = h("div", null,
    warning ? banner(warning, "warn") : null,
    h("div.modal__request", null,
      h("div.eyebrow", { text: "exact wire request" }),
      h("code.mono.code-line", { text: request })),
    h("p.sub", { text: hint || `Type ${module} to confirm. This tool cannot undo the change.` }),
    h("div.field", null, h("label", { for: "confirm-ack", text: `Module id (${module})` }), input));
  const handle = modal({ title, content, danger: true, actions: button });
  const closeModal = handle.close;
  input.id = "confirm-ack";
  return handle;
}

export { clear, mount, h };
