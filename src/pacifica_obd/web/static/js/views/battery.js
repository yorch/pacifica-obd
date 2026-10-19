// HV battery (#/battery) — the PHEV pack panel (design §3.6).
//
// One screen answers "is the pack healthy?": a per-cell bar chart with the mean
// line and min/max marked, the max−min imbalance as the hero number with a
// plain-English verdict, and pack voltage / SoC / temperature stat cards.
//
// The DID map ships *unverified*: the operator's analysis database names the
// pack quantities and their units but carries no DID numbers, so the screen
// reads nothing it cannot name and says so. The preset editor lets the owner
// supply the identifier; the Raw console (#/raw) confirms one column by hand.

import { h, clear, mount, on } from "../dom.js";
import { api } from "../api.js";
import {
  card, kpi, emptyState, errorState, toast, banner, field, checkbox,
} from "../components.js";
import { escapeControls, numberText } from "../format.js";

const ROUTE_NOTE =
  "This screen reads the BPCM (7E7) and HCP (7E2) on CAN-C with UDS 0x22 only. "
  + "No write, no routine, no security access.";

export function render(root, ctx) {
  const state = { schema: null, preset: null, snapshot: null, busy: false, record: false };

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "HV battery" }),
      h("div.sub", { text: "Per-cell voltages, pack imbalance, pack voltage, state of charge and cell temperatures for the hybrid pack." })),
    h("div.row", null,
      h("a.btn.btn--ghost", { href: "#/raw", text: "Open Raw console" }),
      h("button.btn.btn--primary", { type: "button", text: "Read snapshot", onClick: () => read() })));

  const noticeHolder = h("div");
  const heroHolder = h("div");
  const chartHolder = h("div");
  const controlsHolder = h("div");

  mount(root, head, noticeHolder, heroHolder, chartHolder, controlsHolder);

  const readButton = head.querySelector(".btn--primary");

  load();

  return {};

  /* -------------------------------------------------------------- schema */

  async function load() {
    try {
      const schema = await api.batterySchema();
      state.schema = schema;
      state.preset = { ...schema.preset };
      renderNotice();
      renderControls();
      renderEmpty();
      const auto = ctx.params?.get("auto");
      if (auto === "1" || auto === "true") read();
      if (["1", "true"].includes(ctx.params?.get("record"))) {
        state.record = true;
        const box = document.getElementById("batt-record");
        if (box) box.checked = true;
      }
    } catch (error) {
      mount(heroHolder, card({ eyebrow: "HV battery", title: "Could not load the DID map", body: errorState(error, { onRetry: load }) }));
    }
  }

  function renderNotice() {
    const notes = state.schema?.notes || [];
    clear(noticeHolder);
    noticeHolder.appendChild(card({
      eyebrow: "Unverified", title: "The pack DID map is not confirmed",
      body: h("div", null,
        banner("No pack DID was recovered for this vehicle: the analysis database lists the "
          + "quantities and units but no identifiers. Every DID below is unverified, and an "
          + "unverified DID may read nothing — or the wrong quantity.", "warn"),
        h("ul", null, notes.map((note) => h("li", { text: escapeControls(note) }))),
        h("p.sub", null,
          "Confirm a column by hand in the ",
          h("a", { href: "#/raw", text: "Raw console" }),
          " with ",
          h("code.mono", { text: "22 <DID>" }),
          " against this chart before trusting it.")),
    }));
  }

  /* ------------------------------------------------------------- controls */

  function renderControls() {
    const schema = state.schema || {};
    const preset = state.preset || {};
    const cellsDid = field({ id: "batt-cells-did", label: "Cells DID (4 hex)", mono: true,
      value: preset.cells_did || "", placeholder: "e.g. F40C" });
    const firstIndex = field({ id: "batt-first", label: "First cell index", mono: true, type: "number",
      value: String(preset.first_cell_index ?? 1) });
    const count = field({ id: "batt-count", label: "Cell count (1–96)", mono: true, type: "number",
      value: String(preset.count ?? 0), max: 96 });
    const scale = field({ id: "batt-scale", label: "Scale (mV per count)", mono: true, type: "number",
      value: String(preset.scale ?? 1), hint: "Unverified convention: one big-endian 16-bit word per cell." });
    const packDid = field({ id: "batt-pack-did", label: "Pack voltage DID", mono: true,
      value: preset.pack_voltage_did || "", placeholder: "optional" });
    const packScale = field({ id: "batt-pack-scale", label: "Pack scale (V per count)", mono: true, type: "number",
      value: String(preset.pack_voltage_scale ?? 0.1) });
    const socDid = field({ id: "batt-soc-did", label: "SoC DID (HCP)", mono: true,
      value: preset.soc_did || "", placeholder: "optional" });
    const socScale = field({ id: "batt-soc-scale", label: "SoC scale (% per count)", mono: true, type: "number",
      value: String(preset.soc_scale ?? 0.5) });
    const tempDid = field({ id: "batt-temp-did", label: "Temperature DID", mono: true,
      value: preset.temp_did || "", placeholder: "optional" });
    const tempScale = field({ id: "batt-temp-scale", label: "Temp scale (°C per count)", mono: true, type: "number",
      value: String(preset.temp_scale ?? 1) });
    const moduleField = field({ id: "batt-module", label: "Pack module", mono: true,
      value: preset.module || schema.module?.bpcm || "7E7" });
    const socModuleField = field({ id: "batt-soc-module", label: "SoC module", mono: true,
      value: preset.soc_module || schema.module?.hcp || "7E2" });
    const timeoutField = field({ id: "batt-timeout", label: "Timeout (ms)", mono: true, type: "number", value: "100" });
    const recordBox = checkbox({ id: "batt-record", label: "Record this snapshot to the local database", checked: state.record });
    on(recordBox.querySelector("input"), "change", (event) => { state.record = event.target.checked; });

    const saveButton = h("button.btn", { type: "button", text: "Save preset", onClick: () => savePreset() });
    const readButton = h("button.btn.btn--primary", { type: "button", text: "Read snapshot", onClick: () => read() });

    mount(controlsHolder, card({
      eyebrow: "Pack preset", title: "DID map",
      actions: h("div.row", null, saveButton, readButton),
      body: h("div", null,
        h("div.grid.grid--2", null,
          h("div", null, cellsDid, firstIndex, count, scale),
          h("div", null, packDid, packScale, socDid, socScale, tempDid, tempScale)),
        h("div.row", null, moduleField, socModuleField, timeoutField),
        h("p.sub", { text: "An unverified DID may read nothing. Confirm one column against the Raw console first; save the map once it matches." }),
        recordBox),
    }));
  }

  function currentPreset() {
    const value = (id) => document.getElementById(id)?.value ?? "";
    const number = (id, fallback) => {
      const text = value(id);
      const parsed = Number(text);
      return text === "" || Number.isNaN(parsed) ? fallback : parsed;
    };
    return {
      cells_did: value("batt-cells-did").trim().toUpperCase().replace(/^0X/, "") || null,
      first_cell_index: number("batt-first", 1),
      count: number("batt-count", 0),
      scale: number("batt-scale", 1),
      pack_voltage_did: value("batt-pack-did").trim().toUpperCase().replace(/^0X/, "") || null,
      pack_voltage_scale: number("batt-pack-scale", 0.1),
      soc_did: value("batt-soc-did").trim().toUpperCase().replace(/^0X/, "") || null,
      soc_scale: number("batt-soc-scale", 0.5),
      temp_did: value("batt-temp-did").trim().toUpperCase().replace(/^0X/, "") || null,
      temp_scale: number("batt-temp-scale", 1),
      module: value("batt-module").trim().toUpperCase() || "7E7",
      soc_module: value("batt-soc-module").trim().toUpperCase() || "7E2",
    };
  }

  async function savePreset() {
    try {
      const result = await api.batteryPreset(currentPreset());
      state.preset = result.preset;
      toast(result.saved ? "Preset saved" : "Preset validated (could not write the file)", result.saved ? "ok" : "warn");
    } catch (error) {
      toast(error.message, "err", 0);
    }
  }

  /* ------------------------------------------------------------------ read */

  async function read() {
    if (state.busy) { toast("a snapshot is already running", "warn"); return; }
    state.busy = true;
    readButton.disabled = true;
    const body = { preset: currentPreset(), store: state.record, timeout_ms: Number(document.getElementById("batt-timeout")?.value || 100) };
    mount(heroHolder, card({ eyebrow: "Snapshot", title: "Reading the pack…", body: h("div.pad", null, h("span.sub", { text: "Addressing the BPCM and HCP and reading 0x22." })) }));
    clear(chartHolder);
    try {
      state.snapshot = await api.batteryRead(body);
      renderSnapshot();
    } catch (error) {
      mount(heroHolder, card({ eyebrow: "Snapshot", title: "Snapshot failed", body: errorState(error, { onRetry: read }) }));
    } finally {
      state.busy = false;
      readButton.disabled = false;
    }
  }

  function renderEmpty() {
    mount(heroHolder, h("div.card", null, h("div.card__body", null,
      emptyState({
        icon: "gauge", title: "No snapshot yet",
        hint: "The pack DID map is unverified, so nothing is read until you supply an identifier "
          + "and press Read snapshot. If a read returns nothing, check the identifier in the Raw console.",
      }))));
    clear(chartHolder);
  }

  /* -------------------------------------------------------------- render */

  function verdictCard(snapshot) {
    const verdict = snapshot.verdict || { band: "unknown", text: "" };
    const kind = { balanced: "ok", watch: "warn", imbalanced: "warn", severe: "crit",
                   incomplete: "warn", unknown: "warn" }[verdict.band] || "warn";
    const imbalance = snapshot.imbalance_mv;
    const cells = snapshot.cells || {};
    return h("div", { class: `verdict verdict--${kind}` },
      h("div", null,
        h("div.eyebrow", { text: "Pack imbalance" }),
        h("div.verdict__word", { text: imbalance === null || imbalance === undefined ? "—" : `${numberText(imbalance)} mV` }),
        h("div.verdict__meta", { text: `max − min across ${cells.values_mv?.length || 0} cell(s)` }),
        h("p", { text: verdict.text })),
      h("div", { class: "verdict__meta", text: verdict.note || "A spread is only meaningful with the pack at rest." }));
  }

  function statCards(snapshot) {
    const pack = snapshot.pack_voltage || {};
    const soc = snapshot.soc || {};
    const temps = snapshot.temperature || {};
    const current = snapshot.current || {};
    const foot = (field_) => field_.status && field_.status !== "ok"
      ? `status: ${field_.status}${field_.did ? ` · DID ${field_.did}` : ""}`
      : (field_.source === "cells_sum" ? "cell sum" : (field_.did ? `DID ${field_.did}` : null));
    return h("div.grid.grid--kpi", null,
      kpi({ label: "Pack voltage", value: pack.value, unit: pack.value === null ? "" : "V", foot: foot(pack) }),
      kpi({ label: "State of charge", value: soc.value, unit: soc.value === null ? "" : "%", foot: foot(soc) }),
      kpi({ label: "Cell temp (min)", value: temps.min_c, unit: temps.min_c === null ? "" : "°C", foot: foot(temps) }),
      kpi({ label: "Cell temp (max)", value: temps.max_c, unit: temps.max_c === null ? "" : "°C", foot: foot(temps) }),
      current.did ? kpi({ label: "Pack current", value: current.value, unit: "A", foot: foot(current) }) : null,
    );
  }

  function renderSnapshot() {
    const snapshot = state.snapshot;
    const cells = snapshot.cells || {};
    mount(heroHolder, null,
      h("div.grid.grid--2", null,
        h("div.card", null, h("div.card__body", null, verdictCard(snapshot))),
        h("div.card", null, h("div.card__body", null, statCards(snapshot)))),
      snapshot.warnings?.length
        ? h("div", { style: { marginTop: "var(--space-5)" } },
            snapshot.warnings.map((warning) => banner(warning, "warn")))
        : null,
      snapshot.recorded
        ? h("div", { style: { marginTop: "var(--space-4)" } },
            banner(`Recorded ${snapshot.recorded.rows} row(s) to session ${snapshot.recorded.session_id}.`, "ok"))
        : null);

    clear(chartHolder);
    if (!cells.values_mv?.length) {
      chartHolder.appendChild(card({
        eyebrow: "Per-cell voltages", title: "No cell data",
        body: h("div", null,
          banner(cellMissReason(cells), "warn"),
          h("p.sub", { text: "Set a cells DID in the preset above, or read " + "22 <DID>" + " in the Raw console to find one." })),
      }));
      return;
    }
    chartHolder.appendChild(card({
      eyebrow: "Per-cell voltages", title: "Pack cells (mV)",
      meta: `${cells.values_mv.length} cell(s) · mean ${numberText(cells.mean_mv)} mV · min ${numberText(cells.min_mv, 0)} · max ${numberText(cells.max_mv, 0)} · cells > ${cells.away_mv} mV from the mean highlighted`,
      body: h("div", null, cellChart(cells)),
    }));
  }

  function cellMissReason(cells) {
    if (cells.did && cells.status && cells.status !== "ok") {
      return `DID ${cells.did} returned ${cells.status} — nothing to chart.`;
    }
    return "No cells DID is configured, so no per-cell read was attempted.";
  }
}

/* ---------------------------------------------------------------- chart */

// A per-cell bar chart in SVG (no chart dependency needed — uPlot is a
// time-series engine). One bar per cell in pack order so a weak cell can be
// located by position; the pack mean is a dashed line; the min and max are
// marked; and any cell more than `away_mv` from the mean is tinted.
function cellChart(cells) {
  const values = cells.values_mv || [];
  const index = cells.index || values.map((_, i) => i + 1);
  const n = values.length;
  const height = 240;
  const padL = 44, padR = 16, padT = 18, padB = 30;
  const width = Math.max(380, padL + padR + n * 13);
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const mean = cells.mean_mv;
  const lo = cells.min_mv, hi = cells.max_mv;
  const span = (hi - lo) || 1;
  const yLo = lo - span * 0.25;
  const yHi = hi + span * 0.25;
  const yAt = (value) => padT + plotH * (1 - (value - yLo) / (yHi - yLo));
  const xAt = (i) => padL + plotW * ((i + 0.5) / n);
  const barW = Math.max(3, (plotW / n) * 0.72);

  const children = [];
  // Three horizontal gridlines with mV labels.
  for (let step = 0; step <= 3; step += 1) {
    const value = yLo + (yHi - yLo) * (step / 3);
    const y = yAt(value);
    children.push(h("line", { class: "batt-grid", x1: padL, y1: y, x2: width - padR, y2: y }));
    children.push(h("text", { class: "batt-axis", x: padL - 6, y: y + 3, "text-anchor": "end",
      text: numberText(value, 0) }));
  }
  // Bars. A tiny column is drawn as a rounded path so a value exactly on the
  // baseline is still visible.
  values.forEach((value, i) => {
    const x = xAt(i) - barW / 2;
    const y = yAt(Math.min(value, yHi));
    const barH = Math.max(2, (yAt(yLo) - y));
    const away = Math.abs(value - mean) > cells.away_mv;
    let cls = "batt-bar";
    if (away) cls += " batt-bar--away";
    if (index[i] === cells.min_index) cls += " batt-bar--min";
    if (index[i] === cells.max_index) cls += " batt-bar--max";
    children.push(h("path", {
      class: cls,
      d: `M ${x.toFixed(1)} ${(y + barH).toFixed(1)} h ${barW.toFixed(1)} v ${(-barH).toFixed(1)} h ${(-barW).toFixed(1)} z`,
    }));
  });
  // The mean line, drawn last so it sits above the bars.
  if (mean !== null && mean !== undefined) {
    const y = yAt(mean);
    children.push(h("line", { class: "batt-mean", x1: padL, y1: y, x2: width - padR, y2: y }));
    children.push(h("text", { class: "batt-mean-label", x: padL + 2, y: padT - 5,
      "text-anchor": "start", text: `mean ${numberText(mean)}` }));
  }
  // Min / max markers (the numbers are in the card caption, so a label can
  // never sit on the bar it describes).
  for (const [key, value] of [["min", cells.min_mv], ["max", cells.max_mv]]) {
    const i = key === "min" ? index.indexOf(cells.min_index) : index.indexOf(cells.max_index);
    if (i < 0) continue;
    children.push(h("circle", { class: `batt-mark batt-mark--${key}`, cx: xAt(i), cy: yAt(value), r: 3.4 }));
  }
  // Cell-index axis: a label every few cells so the axis never turns to soup.
  const every = Math.max(1, Math.ceil(n / 12));
  values.forEach((_, i) => {
    if ((i + 1) % every && i !== n - 1) return;
    children.push(h("text", { class: "batt-axis", x: xAt(i), y: height - 10, "text-anchor": "middle",
      text: String(index[i]) }));
  });

  const svg = h("svg", { class: "batt-chart", viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "xMidYMid meet", role: "img",
    "aria-label": `Per-cell voltages: ${n} cells, mean ${numberText(mean)} mV, min ${numberText(lo)} mV, max ${numberText(hi)} mV.` },
    ...children);

  return h("div", null,
    svg,
    h("div.batt-legend", null,
      h("span.batt-legend__item", null, h("span.batt-swatch"), h("span", { text: "cell" })),
      h("span.batt-legend__item", null, h("span.batt-swatch.batt-swatch--away"), h("span", { text: `> ${cells.away_mv} mV from mean` })),
      h("span.batt-legend__item", null, h("span.batt-swatch.batt-swatch--min"), h("span", { text: "min" })),
      h("span.batt-legend__item", null, h("span.batt-swatch.batt-swatch--max"), h("span", { text: "max" })),
      h("span.sub", { text: "pack order, not sorted, so a weak cell keeps its position" })));
}
