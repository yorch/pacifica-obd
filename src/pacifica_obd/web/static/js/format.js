// Value / status formatting helpers.
//
// Status is never conveyed by colour alone: every helper returns a class *and*
// text, and the pill text is always the machine status string the CLI emits.

const STATUS_CLASS = {
  ok: "pill--ok",
  unlocked: "pill--ok",
  resolved: "pill--ok",
  ready: "pill--ok",
  complete: "pill--ok",
  confirmed: "pill--crit",
  testfailed: "pill--crit",
  warninglamp: "pill--crit",
  negative: "pill--crit",
  rejected: "pill--crit",
  wrong_responder: "pill--crit",
  malformed: "pill--warn",
  unexpected: "pill--warn",
  no_response: "pill--idle",
  pending: "pill--warn",
  ambiguous: "pill--warn",
  unmapped: "pill--idle",
  partial: "pill--warn",
  stored: "pill--crit",
  permanent: "pill--crit",
};

export function statusClass(status) {
  if (!status) return "pill--idle";
  return STATUS_CLASS[String(status).toLowerCase()] || "pill--idle";
}

export function rowClass(status) {
  const cls = statusClass(status);
  if (cls === "pill--crit") return "row--crit";
  if (cls === "pill--warn") return "row--warn";
  if (cls === "pill--ok") return "row--ok";
  return "";
}

export function valueText(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    if ("value" in value) return `${valueText(value.value)}${value.unit ? " " + value.unit : ""}`;
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (Number.isInteger(value)) return String(value);
    return String(Math.round(value * 100) / 100);
  }
  return String(value);
}

export function unitOf(value) {
  if (value && typeof value === "object" && "unit" in value) return value.unit || "";
  return "";
}

export function numberText(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1000) return n.toFixed(0);
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(digits);
}

export function timeOf(ts) {
  if (!ts) return "—";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return String(ts);
  return date.toLocaleTimeString([], { hour12: false });
}

export function dateTimeOf(ts) {
  if (!ts) return "—";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return String(ts);
  return date.toLocaleString([], { hour12: false });
}

export function agoOf(ts) {
  if (!ts) return "";
  const date = new Date(ts);
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 1) return "now";
  if (seconds < 60) return `${seconds.toFixed(1)} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export function msText(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

// Escape-free by construction: this only ever builds text nodes, and callers use
// it for labels, never for markup.
export function truncate(text, max = 60) {
  const s = text === null || text === undefined ? "" : String(text);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

// Escape control characters so bus ASCII cannot smuggle terminal controls into
// the page; returns a plain string.
export function escapeControls(text) {
  if (text === null || text === undefined) return "";
  return String(text).replace(/[\u0000-\u001f\u007f]/g, (ch) => {
    const code = ch.charCodeAt(0).toString(16).padStart(2, "0");
    return `\\x${code}`;
  });
}

export function seriesColor(index) {
  return `var(--series-${(index % 8) + 1})`;
}

export function seriesColorValue(index) {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(`--series-${(index % 8) + 1}`).trim();
  return value || "#2451d6";
}
