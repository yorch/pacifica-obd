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
  card, kpi, emptyState, errorState, toast, banner, field, checkbox, loadingState,
  verdict, tonePill, dataTable, progressBar,
} from "../components.js";
import { escapeControls, numberText, timeOf } from "../format.js";

export function render(root, ctx) {
  const timers = [];
  const state = { schema: null, preset: null, snapshot: null, busy: false, record: false,
                  readAt: null, sweep: null, sweeping: false };

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
  const sweepHolder = h("div");
  const sweepResults = h("div");
  const sweepProgress = progressBar();
  let sweepUseButton = null;

  mount(root, head, noticeHolder, heroHolder, chartHolder, controlsHolder, sweepHolder);

  const readButton = head.querySelector(".btn--primary");

  load();

  return { dispose() { for (const timer of timers) clearInterval(timer); }, resize() {} };

  /* -------------------------------------------------------------- schema */

  async function load() {
    try {
      const schema = await api.batterySchema();
      state.schema = schema;
      state.preset = { ...schema.preset };
      renderNotice();
      renderControls();
      renderSweep();
      renderEmpty();
      const auto = ctx.params?.get("auto");
      if (auto === "1" || auto === "true") read();
      if (["1", "true"].includes(ctx.params?.get("record"))) {
        state.record = true;
        const box = document.getElementById("batt-record");
        if (box) box.checked = true;
      }
      // Deep link for the sweep: `?sweep=1` runs it, `?sweep=apply` also fills
      // the preset editor from the best candidates.
      const sweepParam = ctx.params?.get("sweep");
      if (sweepParam === "1" || sweepParam === "true" || sweepParam === "apply") {
        discover().then(() => { if (sweepParam === "apply") applyProposedPreset(); });
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
      actions: tonePill("unverified DID map", "untested"),
      body: h("div.stack", null,
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

    mount(controlsHolder, card({
      eyebrow: "Pack preset", title: "DID map",
      actions: h("div.row", null, saveButton),
      body: h("div.stack", null,
        h("div.grid.grid--2", null,
          h("div.stack", null, cellsDid, firstIndex, count, scale),
          h("div.stack", null, packDid, packScale, socDid, socScale, tempDid, tempScale)),
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

  /* ------------------------------------------------------------- DID sweep */

  // "Discover DIDs": a bounded, read-only sweep of one module (`10 03` then
  // `22 <DID>`), run as a normal serialized worker job so it queues behind other
  // bus work and cancels like every other long operation. The server classifies
  // each reply; here we group the results by inferred kind and offer to prefill
  // the preset above from the best candidates (never automatically).
  function renderSweep() {
    const module = state.preset?.module || state.schema?.module?.bpcm || "7E7";
    const moduleField = field({ id: "batt-sweep-module", label: "Module", mono: true, value: module });
    const startField = field({ id: "batt-sweep-start", label: "Start DID", mono: true, value: "F200" });
    const endField = field({ id: "batt-sweep-end", label: "End DID", mono: true, value: "F2FF" });
    const stepField = field({ id: "batt-sweep-step", label: "Step", mono: true, type: "number", value: "1", min: 1 });
    const timeoutField = field({ id: "batt-sweep-timeout", label: "Timeout (ms)", mono: true, type: "number", value: "100" });
    sweepUseButton = h("button.btn", { type: "button", text: "Use as battery preset",
      disabled: !(state.sweep?.preset?.available), onClick: () => applyProposedPreset() });
    const runButton = h("button.btn.btn--primary", { type: "button", text: "Discover DIDs", onClick: () => discover() });
    const cancelButton = h("button.btn.btn--danger.btn--sm", { type: "button", text: "Cancel", disabled: !state.sweeping, onClick: cancelSweep });

    mount(sweepHolder, card({
      eyebrow: "DID discovery", title: "Sweep a DID range",
      actions: h("div.row", null, sweepUseButton),
      body: h("div.stack", null,
        banner("Read-only sweep: opens the session with 10 03 and reads 22 <DID> per identifier. "
          + "No write service is ever sent. A reply is named only when its bytes fit a conservative "
          + "shape; anything else is left unknown.", "info"),
        h("div.row", null, moduleField, startField, endField, stepField, timeoutField),
        h("div.row.row--spaced", runButton, cancelButton),
        h("div.row--spaced", sweepProgress),
        sweepResults),
    }));
    renderSweepResults();
  }

  function didValue(id, fallback) {
    const text = (document.getElementById(id)?.value || "").trim();
    return text || fallback;
  }

  async function discover() {
    if (state.sweeping) { toast("a sweep is already running", "warn"); return; }
    state.sweeping = true;
    sweepUseButton.disabled = true;
    sweepProgress.update(0);
    const stop = pollSweepProgress();
    mount(sweepResults, loadingState("Sweeping the range with 22 <DID>…"));
    try {
      state.sweep = await api.batterySweep({
        module: didValue("batt-sweep-module", state.preset?.module || "7E7"),
        bus: state.schema?.module?.bus || "can-c",
        start: didValue("batt-sweep-start", "F100").toUpperCase().replace(/^0X/, ""),
        end: didValue("batt-sweep-end", "F1FF").toUpperCase().replace(/^0X/, ""),
        step: Number(didValue("batt-sweep-step", "1")),
        timeout_ms: Number(didValue("batt-sweep-timeout", "100")),
      });
      renderSweepResults();
      toast(`sweep finished: ${state.sweep.answered} of ${state.sweep.plan.count} answered`,
        state.sweep.answered ? "ok" : "warn");
    } catch (error) {
      mount(sweepResults, errorState(error, { onRetry: discover }));
    } finally {
      stop();
      state.sweeping = false;
      sweepProgress.update(1);
      sweepUseButton.disabled = !(state.sweep?.preset?.available);
      ctx.refreshStatus();
    }
  }

  function pollSweepProgress() {
    const tick = setInterval(async () => {
      try {
        const { jobs } = await api.jobs();
        for (const job of jobs.filter((j) => j.state === "running")) {
          const done = Number(job.progress?.index);
          const total = Number(job.progress?.total);
          if (done && total) sweepProgress.update(done / total);
        }
      } catch { /* progress is best effort */ }
    }, 500);
    timers.push(tick);
    return () => clearInterval(tick);
  }

  async function cancelSweep() {
    const id = ctx.status?.status?.job?.holder_id;
    if (!id) { toast("nothing running", "warn"); return; }
    try { await api.cancelJob(id); toast("Cancel requested", "warn"); }
    catch (error) { toast(error.message, "err"); }
  }

  function renderSweepResults() {
    clear(sweepResults);
    const sweep = state.sweep;
    if (!sweepUseButton) return;
    sweepUseButton.disabled = !(sweep?.preset?.available);
    if (!sweep) {
      sweepResults.appendChild(emptyState({ icon: "layers", title: "No sweep yet",
        hint: "Discover DIDs reads 22 <DID> across the range and names the replies it can. "
          + "Unverified DIDs may answer nothing on a real vehicle." }));
      return;
    }
    const suggestion = sweep.preset || {};
    if (suggestion.available && suggestion.preset) {
      const preset = suggestion.preset;
      sweepResults.appendChild(banner(
        `Proposed preset — cells ${preset.cells_did || "—"} (${preset.count || 0} × ${preset.scale}), `
        + `pack voltage ${preset.pack_voltage_did || "—"}, SoC ${preset.soc_did || "—"}. `
        + "Confirm each DID in the Raw console before trusting a read.", "ok"));
    } else {
      sweepResults.appendChild(banner(
        "Nothing was confidently classified, so the saved preset is left alone.", "warn"));
    }
    for (const note of suggestion.notes || []) {
      sweepResults.appendChild(h("p.sub", { text: note }));
    }

    const groups = new Map();
    for (const entry of sweep.dids || []) {
      const kind = entry.verdict && entry.verdict.kind !== "unknown"
        ? entry.verdict.kind
        : (entry.status === "ok" ? "unclassified" : "no response");
      if (!groups.has(kind)) groups.set(kind, []);
      groups.get(kind).push(entry);
    }
    // Answering groups first (most interesting), the silent tail folded away.
    const ordered = [...groups.entries()].sort(([a], [b]) => {
      if ((a === "no response") !== (b === "no response")) return a === "no response" ? 1 : -1;
      return a.localeCompare(b);
    });
    for (const [kind, rows] of ordered) {
      const table = dataTable({
        columns: [
          { key: "did", label: "DID", mono: true },
          { key: "status", label: "Status" },
          { key: "hex", label: "Reply", mono: true, render: (row) => replyText(row.hex) },
          { key: "confidence", label: "Confidence", render: (row) => row.verdict?.confidence || "—" },
          { key: "reason", label: "Why", render: (row) => row.verdict?.reason || "" },
        ],
        rows,
        empty: "none",
      });
      const label = `${kind.replace(/_/g, " ")} · ${rows.length} DID${rows.length === 1 ? "" : "s"}`;
      if (kind === "no response") {
        sweepResults.appendChild(h("details.sweep-group", null,
          h("summary", null, h("span", { text: label })),
          table));
      } else {
        sweepResults.appendChild(h("h3", { text: label }));
        sweepResults.appendChild(table);
      }
    }
  }

  function replyText(hex) {
    const text = escapeControls(hex || "");
    if (!text) return "—";
    return h("span", { title: text, text: text.length > 40 ? `${text.slice(0, 40)}…` : text });
  }

  function applyProposedPreset() {
    const preset = state.sweep?.preset?.preset;
    if (!preset) { toast("no confident candidates to apply", "warn"); return; }
    const set = (id, value) => {
      const node = document.getElementById(id);
      if (node && value !== null && value !== undefined) node.value = String(value);
    };
    set("batt-cells-did", preset.cells_did || "");
    set("batt-count", preset.count || 0);
    set("batt-scale", preset.scale ?? 1);
    set("batt-pack-did", preset.pack_voltage_did || "");
    set("batt-pack-scale", preset.pack_voltage_scale ?? 0.1);
    set("batt-soc-did", preset.soc_did || "");
    set("batt-soc-scale", preset.soc_scale ?? 0.5);
    set("batt-temp-did", preset.temp_did || "");
    set("batt-temp-scale", preset.temp_scale ?? 1);
    state.preset = { ...state.preset, ...preset };
    const ambiguous = Object.values(state.sweep.preset.confidence || {}).includes("ambiguous");
    toast(ambiguous
      ? "Preset fields filled from the best candidates — some are ambiguous; confirm before trusting"
      : "Preset fields filled from the sweep candidates", ambiguous ? "warn" : "ok");
  }

  /* ------------------------------------------------------------------ read */

  async function read() {
    if (state.busy) { toast("a snapshot is already running", "warn"); return; }
    state.busy = true;
    readButton.disabled = true;
    state.readAt = new Date().toISOString();
    const body = { preset: currentPreset(), store: state.record, timeout_ms: Number(document.getElementById("batt-timeout")?.value || 100) };
    mount(heroHolder, card({ eyebrow: "Snapshot", title: "Reading the pack…",
      body: loadingState("Addressing the BPCM and HCP and reading 0x22.") }));
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
    mount(heroHolder, card({
      eyebrow: "Snapshot", title: "No snapshot yet",
      body: emptyState({
        icon: "gauge",
        hint: "The pack DID map is unverified, so nothing is read until you supply an identifier "
          + "and press Read snapshot. If a read returns nothing, check the identifier in the Raw console.",
      }),
    }));
    clear(chartHolder);
  }

  /* -------------------------------------------------------------- render */

  function verdictCard(snapshot) {
    const pack = snapshot.verdict || { band: "unknown", text: "" };
    const kind = { balanced: "ok", watch: "warn", imbalanced: "warn", severe: "crit",
                   incomplete: "warn", unknown: "warn" }[pack.band] || "warn";
    const imbalance = snapshot.imbalance_mv;
    const cells = snapshot.cells || {};
    return verdict({
      kind,
      eyebrow: "Pack imbalance",
      word: imbalance === null || imbalance === undefined ? "—" : `${numberText(imbalance)} mV`,
      meta: `max − min across ${cells.values_mv?.length || 0} cell(s)`,
      body: h("p", { text: pack.text }),
      aside: pack.note || "A spread is only meaningful with the pack at rest.",
    });
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
        card({ body: verdictCard(snapshot) }),
        card({ body: statCards(snapshot) })),
      snapshot.warnings?.length
        ? h("div.stack", null, snapshot.warnings.map((warning) => banner(warning, "warn")))
        : null,
      snapshot.recorded
        ? banner(`Recorded ${snapshot.recorded.rows} row(s) to session ${snapshot.recorded.session_id}.`, "ok")
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
      meta: `${state.readAt ? `read ${timeOf(state.readAt)} · ` : ""}${cells.values_mv.length} cell(s) · mean ${numberText(cells.mean_mv)} mV · min ${numberText(cells.min_mv, 0)} · max ${numberText(cells.max_mv, 0)} · cells > ${cells.away_mv} mV from the mean highlighted`,
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
