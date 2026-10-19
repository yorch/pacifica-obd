// Live data (#/live) — gauges, bars, digital readouts and the time-series chart,
// fed by the SSE watch/monitor streams, with a recording bar, bookmarks, CSV
// export, gauge templates and chart replay from stored samples.

import { h, clear, mount, on, download } from "../dom.js";
import { api, ApiError } from "../api.js";
import { openStream } from "../stream.js";
import {
  card, emptyState, toast, gaugeRadial, gaugeBar,
  digitalReadout, select, checkbox, field, icon,
} from "../components.js";
import { seriesColorValue, timeOf } from "../format.js";
import { TimeSeriesChart, smallMultiples } from "../charts.js";

const DEFAULT_PIDS = ["RPM", "SPEED", "COOLANT_TEMP", "ENGINE_LOAD", "CONTROL_MODULE_VOLTAGE"];

// Readable DID numbers we can pre-populate: the identification block. The bundled
// dataset's per-ECU entries name data items but carry no DID number, so anything
// beyond this list has to be typed in (free entry).
const KNOWN_DIDS = ["F100", "F186", "F187", "F188", "F189", "F18A", "F18C", "F190", "F191"];

// Instrument ranges (min/max and the warning/redline zones) per known PID.
const RANGES = {
  RPM: { min: 0, max: 7000, warn: 5500, redline: 6200, unit: "rpm" },
  SPEED: { min: 0, max: 240, warn: 130, unit: "km/h" },
  COOLANT_TEMP: { min: -40, max: 130, warn: 100, redline: 108, unit: "°C" },
  ENGINE_LOAD: { min: 0, max: 100, warn: 85, unit: "%" },
  THROTTLE_POS: { min: 0, max: 100, unit: "%" },
  INTAKE_TEMP: { min: -20, max: 90, warn: 70, unit: "°C" },
  MAF: { min: 0, max: 60, unit: "g/s" },
  FUEL_LEVEL: { min: 0, max: 100, warn: null, unit: "%" },
  CONTROL_MODULE_VOLTAGE: { min: 8, max: 16, redline: 15.2, unit: "V" },
  ELM_VOLTAGE: { min: 8, max: 16, unit: "V" },
};

const TEMPLATE_KEY = "pacifica-obd.gauge-templates";

function rangeFor(key, unit) {
  const base = RANGES[key] || RANGES[String(key).split(":").pop()] || {};
  return { min: 0, max: 100, ...base, unit: unit || base.unit || "" };
}

function loadTemplates() {
  try { return JSON.parse(localStorage.getItem(TEMPLATE_KEY) || "{}"); } catch { return {}; }
}

function saveTemplates(all) {
  localStorage.setItem(TEMPLATE_KEY, JSON.stringify(all));
}

export function render(root, ctx) {
  const charts = [];
  let stream = null;
  let streamId = null;
  let elapsedTimer = null;
  let sampleCount = 0;
  let startedAt = null;
  let frozen = false;
  const buffer = new Map();   // key -> [{t, value, text, unit}]
  const lastSeen = new Map(); // key -> ms
  const lastValues = new Map();

  const source = { mode: "pid", ecu: null, bus: "can-c", interval: 1000, store: false, display: "gauges" };
  let selection = [...DEFAULT_PIDS];
  let available = [];         // [{key,label,unit}] pickable
  let meta = new Map();       // key -> {label, unit}

  /* ------------------------------------------------------------- toolbar */

  const sourceSelect = select({ id: "source", label: "Source", value: "pid",
    options: [{ value: "pid", label: "OBD-II PIDs (generic)" }, { value: "did", label: "UDS DIDs (module)" }] });
  const displaySelect = select({ id: "display", label: "Display", value: "gauges",
    options: [
      { value: "gauges", label: "Gauges" }, { value: "bars", label: "Bars" },
      { value: "digital", label: "Digital" }, { value: "chart", label: "Chart" },
    ] });
  const busSelect = select({ id: "bus", label: "Bus", value: "can-c", mono: true,
    options: [{ value: "can-c", label: "can-c" }, { value: "can-ihs", label: "can-ihs" }] });
  const intervalField = field({ id: "interval", label: "Poll interval (ms)", value: "1000", mono: true, type: "number", min: 50 });
  const storeBox = checkbox({ id: "store", label: "Store to DB", checked: false });
  const ecuSelect = select({ id: "ecu", label: "ECU", value: "", mono: true, options: [{ value: "", label: "loading…" }] });
  const moduleField = field({ id: "module-id", label: "Module request ID (optional)", placeholder: "7E0", mono: true });

  const filterField = field({ id: "filter", label: "Filter values", placeholder: "type to filter…" });
  const chipsHolder = h("div.chips.chips--picker");
  const didFreeEntry = h("div.row", null,
    field({ id: "free-did", label: "Add DID (hex)", placeholder: "F190", mono: true }),
    h("button.btn.btn--sm", { type: "button", text: "Add", onClick: addFreeDid }));
  didFreeEntry.hidden = true;   // DID-only control: hidden until the DID source is chosen

  const recordBar = h("div.recording", null,
    h("span.recording__dot", { "aria-hidden": "true" }),
    h("span.recording__time", { id: "rec-time", text: "00:00.0" }),
    h("span.sub", { id: "rec-rate", text: "idle" }),
    h("span.sub", { id: "rec-count", text: "0 samples" }),
    h("span.spacer"),
    h("button.btn.btn--sm", { type: "button", text: "Bookmark", onClick: bookmark, disabled: true , id: "rec-bookmark" }),
    h("button.btn.btn--sm", { type: "button", text: "Clear", onClick: clearBuffers }),
    h("button.btn.btn--sm", { type: "button", text: "Freeze", onClick: toggleFreeze, id: "rec-freeze" }),
    h("button.btn.btn--sm", { type: "button", text: "CSV", onClick: exportCsv, disabled: true, id: "rec-csv" }),
    h("button.btn.btn--sm.btn--danger", { type: "button", text: "Stop", onClick: stop, disabled: true, id: "rec-stop" }));
  recordBar.id = "record-bar";

  const startButton = h("button.btn.btn--primary", { type: "button", id: "live-start", text: "Start", onClick: start });

  const templateSelect = select({ id: "template", label: "Gauge template", value: "",
    options: [{ value: "", label: "— none —" }] });
  const templateName = field({ id: "template-name", label: "Save as", placeholder: "e.g. Warm-up" });
  const templateRow = h("div.row", null, templateSelect,
    h("button.btn.btn--sm", { type: "button", text: "Load", onClick: loadTemplate }),
    templateName,
    h("button.btn.btn--sm", { type: "button", text: "Save", onClick: saveTemplate }),
    h("button.btn.btn--sm.btn--ghost", { type: "button", text: "Delete", onClick: deleteTemplate }));

  const replaySelect = select({ id: "replay", label: "Replay a stored session", value: "",
    options: [{ value: "", label: "loading…" }] });
  const replayRow = h("div.row", null, replaySelect,
    h("button.btn.btn--sm", { type: "button", text: "Load into chart", onClick: loadReplay }));

  const instruments = h("div", { id: "instruments" });
  const chartWrap = h("div", null, h("div.legend"), h("div.chart-holder", { id: "chart-holder" }));
  // One placeholder, two stories: no signals picked, or signals picked and the
  // first sample still on its way (a blank 320px plot reads as broken).
  const chartPlaceholder = { title: "Nothing plotted yet",
    hint: "Choose signals above; a selection of more than six becomes small multiples." };
  const chartEmpty = emptyState({ icon: "activity", ...chartPlaceholder });

  const controls = card({ eyebrow: "Configuration", title: "Signals", body: h("div.stack", null,
    h("div.row", null, sourceSelect, displaySelect, busSelect, intervalField, storeBox, startButton),
    h("div.row", { id: "ecu-row", hidden: true }, ecuSelect, moduleField),
    filterField, chipsHolder, didFreeEntry) });

  const templatesCard = card({ eyebrow: "Presets", title: "Templates & replay",
    body: h("div", null, templateRow, h("div.row.row--spaced", replayRow)) });

  const instrumentsCard = card({ eyebrow: "Live", title: "Instruments",
    body: h("div", null, recordBar, instruments) });
  const chartCard = card({ eyebrow: "Time series", title: "Chart",
    body: h("div", null, chartEmpty, chartWrap) });

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Live data" }),
      h("div.sub", { text: "Pick signals, choose an instrument, and record. Achieved rate is shown honestly next to the timer." })));

  mount(root, head, controls, instrumentsCard, chartCard, templatesCard);

  /* ------------------------------------------------------------- pickers */

  async function loadPicker() {
    try {
      if (source.mode === "pid") {
        const payload = await api.obdPids();
        available = payload.pids.map((row) => ({ key: row.name, label: row.name, unit: "" }));
      } else {
        if (!source.ecu) {
          const { ecus } = await api.ecus();
          const withDids = ecus.filter((ecu) => ecu.dids > 0);
          source.ecu = withDids[0]?.ecu || null;
          const node = ecuSelect.querySelector("select");
          clear(node);
          for (const ecu of withDids) {
            node.appendChild(h("option", { value: ecu.ecu,
              text: `${ecu.ecu}${ecu.name ? " — " + ecu.name : ""} (${ecu.dids} items)` }));
          }
          node.value = source.ecu || "";
        }
        // The dataset carries names, not DID numbers: offer the readable
        // identification block and let the user add ids by hand.
        available = KNOWN_DIDS.map((did) => ({ key: did, label: did, unit: "" }));
      }
    } catch (error) {
      toast(error.message, "err", 0);
      available = [];
    }
    meta = new Map(available.map((row) => [row.key, row]));
    renderChips();
  }

  function renderChips() {
    const filter = (chip_filter || "").toLowerCase();
    clear(chipsHolder);
    // Include any selected key that is not in the picker (e.g. a free-entry DID or
    // a replay's signals) so the current selection is always visible.
    const extra = selection
      .filter((key) => !available.some((row) => row.key === key))
      .map((key) => ({ key, label: (meta.get(key) || {}).label || key, unit: "" }));
    const rows = [...available, ...extra].filter((row) => !filter
      || row.key.toLowerCase().includes(filter) || row.label.toLowerCase().includes(filter));
    if (!rows.length) {
      chipsHolder.appendChild(h("span.sub", { text: available.length ? "no match" : "nothing available" }));
    }
    for (const row of rows.slice(0, 80)) {
      const index = selection.indexOf(row.key);
      const active = index >= 0;
      const label = row.label === row.key ? row.key : `${row.label} (${row.key})`;
      chipsHolder.appendChild(h("button.chip", {
        type: "button", "aria-pressed": active ? "true" : "false",
        title: label,
        onClick: () => toggleSelection(row.key),
      },
        h("span.chip__swatch", { style: { background: active ? seriesColorValue(index) : "var(--idle-line)" } }),
        h("span.chip__label", { text: label }),
        active ? icon("check", { size: 12 }) : null));
    }
  }

  let chip_filter = "";
  function toggleSelection(key) {
    const index = selection.indexOf(key);
    if (index >= 0) selection.splice(index, 1);
    else selection.push(key);
    renderChips();
    renderInstruments();
  }

  function addFreeDid() {
    const input = document.getElementById("free-did");
    const value = (input.value || "").trim().toUpperCase().replace(/^0X/, "");
    if (!/^[0-9A-F]{4}$/.test(value)) { toast("a DID is 4 hex characters, e.g. F190", "err"); return; }
    if (!selection.includes(value)) selection.push(value);
    if (!meta.has(value)) meta.set(value, { key: value, label: value, unit: "" });
    input.value = "";
    renderChips();
    renderInstruments();
  }

  /* -------------------------------------------------------- instruments */

  function seriesDefs() {
    return selection.map((key, index) => {
      const info = meta.get(key) || { label: key, unit: "" };
      const range = rangeFor(key, info.unit);
      return { key, label: info.label || key, unit: range.unit, colour: seriesColorValue(index), range };
    });
  }

  function renderInstruments() {
    if (frozen) return;
    const defs = seriesDefs();
    clear(instruments);
    if (!defs.length) {
      instruments.appendChild(emptyState({ icon: "◉", title: "No values selected",
        hint: "Pick PIDs or a module's DIDs to begin." }));
      renderChart([]);
      return;
    }
    const mode = source.display;
    if (mode === "gauges") {
      instruments.appendChild(h("div.grid.grid--gauges", null, defs.map((def) => gaugeRadial({
        label: def.label, ...instrumentValue(def.key), min: def.range.min, max: def.range.max,
        warnAt: def.range.warn, redlineFrom: def.range.redline, size: defs.length <= 4 ? "lg" : "md",
        stale: isStale(def.key), status: lastValues.get(def.key)?.status,
      }))));
    } else if (mode === "bars") {
      instruments.appendChild(h("div.grid.grid--bars", null, defs.map((def) => gaugeBar({
        label: def.label, ...instrumentValue(def.key), min: def.range.min, max: def.range.max,
        warnAt: def.range.warn, redlineFrom: def.range.redline, stale: isStale(def.key),
      }))));
    } else if (mode === "digital") {
      instruments.appendChild(h("div.grid.grid--kpi", null, defs.map((def) => digitalReadout({
        label: def.label, ...instrumentValue(def.key), stale: isStale(def.key),
        status: lastValues.get(def.key)?.status,
        history: (buffer.get(def.key) || []).slice(-48).map((entry) => entry.value),
      }))));
    } else {
      instruments.appendChild(h("p.sub", { text: "Chart mode — see the chart below." }));
    }
    if (mode === "chart" || mode === "gauges" || mode === "bars" || mode === "digital") {
      renderChart(defs);
    }
  }

  function instrumentValue(key) {
    const entry = lastValues.get(key);
    if (!entry) return { value: null, unit: (meta.get(key) || {}).unit || "" };
    return { value: entry.value, unit: entry.unit || (meta.get(key) || {}).unit || "", status: entry.status };
  }

  function isStale(key) {
    const seen = lastSeen.get(key);
    if (!seen) return true;
    return Date.now() - seen > Math.max(3000, source.interval * 2.5);
  }

  function renderChart(defs) {
    for (const chart of charts.splice(0)) chart.destroy();
    clear(chartWrap.querySelector(".legend"));
    clear(document.getElementById("chart-holder"));
    if (!defs.length) { setChartPlaceholder(chartPlaceholder); return; }
    // A plot with no samples yet is blank, so the placeholder stays until the
    // first sample actually lands.
    if (!sampleCount) { setChartPlaceholder({ title: "Waiting for the first sample",
      hint: "The stream is running; the plot appears as soon as data arrives." }); return; }
    chartEmpty.hidden = true;
    chartWrap.hidden = false;
    if (defs.length > 6) {
      const holder = document.getElementById("chart-holder");
      charts.push(...smallMultiples(holder, defs));
      return;
    }
    const chart = new TimeSeriesChart(document.getElementById("chart-holder"), { height: 320 });
    chart.setSeries(defs);
    charts.push(chart);
  }

  function setChartPlaceholder({ title, hint }) {
    clear(chartEmpty);
    chartEmpty.appendChild(h("div.empty__icon", { "aria-hidden": "true" }, icon("activity", { size: 20 })));
    chartEmpty.appendChild(h("div.empty__title", { text: title }));
    chartEmpty.appendChild(h("p.sub", { text: hint }));
    chartEmpty.hidden = false;
    chartWrap.hidden = true;
  }

  /* ------------------------------------------------------------ recording */

  function start() {
    if (stream) return;
    const interval = Math.max(50, Number(document.getElementById("interval").value) || 1000);
    source.interval = interval;
    source.bus = busSelect.querySelector("select").value;
    source.store = document.getElementById("store").checked;
    source.display = displaySelect.querySelector("select").value;
    const values = source.mode === "pid" ? selection : selection;
    if (!values.length) { toast("select at least one value", "warn"); return; }
    const body = source.mode === "pid"
      ? { source: "pid", values, interval_ms: interval, store: source.store, bus: source.bus }
      : { source: "did", values, module: didModuleId(), bus: source.bus, interval_ms: interval, store: source.store };
    const request = source.mode === "pid" ? api.watchStart(body) : api.monitorStart(body);
    request.then((payload) => {
      streamId = payload.stream_id;
      startedAt = Date.now();
      sampleCount = 0;
      setRecording(true);
      stream = openStream(`/api/live/watch/stream/${streamId}`, {
        onEvent: onStreamEvent,
        onError: (error, attempt) => toast(attempt ? `stream dropped (retry ${attempt})` : `stream ended: ${error.message}`, "warn"),
        onClose: () => setRecording(false),
      });
    }).catch((error) => {
      toast(error instanceof ApiError ? `${error.code}: ${error.message}` : String(error), "err", 0);
    });
  }

  function didModuleId() {
    const value = (document.getElementById("module-id")?.value || "").trim().toUpperCase();
    return /^[0-9A-F]{1,3}$/.test(value) ? value : null;
  }

  function onStreamEvent(event, data) {
    if (frozen) return;
    if (event === "sample") {
      const first = sampleCount === 0;
      sampleCount += 1;
      const now = Date.now();
      for (const [key, entry] of Object.entries(data.values || {})) {
        lastValues.set(key, entry);
        lastSeen.set(key, now);
        if (!meta.has(key)) meta.set(key, { key, label: entry.label || key, unit: entry.unit || "" });
        if (!buffer.has(key)) buffer.set(key, []);
        buffer.get(key).push({ t: now, value: entry.value, unit: entry.unit, status: entry.status });
      }
      const text = document.getElementById("rec-count");
      if (text) text.textContent = `${sampleCount} samples`;
      if (first) { chartEmpty.hidden = true; chartWrap.hidden = false; }
      for (const chart of charts) chart.update(data);
      updateInstrumentValues();
    } else if (event === "status" && data.achieved_hz) {
      const rate = document.getElementById("rec-rate");
      if (rate) rate.textContent = `requested ${data.requested_ms} ms / achieved ${data.achieved_hz} Hz`;
    }
  }

  function updateInstrumentValues() {
    // Cheap in-place update for gauges: the arc eases to the new value and the
    // digit stays put, so a 10 Hz stream reads as a needle, not a flash.
    const defs = seriesDefs();
    if (source.display === "gauges" || source.display === "bars") {
      const nodes = [...instruments.querySelectorAll(source.display === "gauges" ? ".gauge" : ".bar")];
      defs.forEach((def, index) => {
        const node = nodes[index];
        if (!node) return;
        const next = {
          label: def.label, ...instrumentValue(def.key), min: def.range.min,
          max: def.range.max, warnAt: def.range.warn, redlineFrom: def.range.redline,
          size: defs.length <= 4 ? "lg" : "md", stale: isStale(def.key),
          status: lastValues.get(def.key)?.status,
        };
        if (node.update) node.update(next);
        else if (source.display === "gauges") replaceWith(node, gaugeRadial(next));
      });
    } else if (source.display === "digital") {
      renderInstruments();
    }
  }

  function replaceWith(node, next) {
    node.replaceWith(next);
  }

  function stop() {
    if (streamId) api.streamStop(streamId).catch(() => {});
    setRecording(false);
    if (stream) stream.close();
  }

  function setRecording(on) {
    if (!on) {
      stream = null;
      streamId = null;
      if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
      document.getElementById("record-bar").classList.remove("recording--on");
      document.getElementById("live-start").disabled = false;
      document.getElementById("live-start").textContent = "Start";
      for (const id of ["rec-stop", "rec-bookmark", "rec-csv"]) {
        const node = document.getElementById(id);
        if (node) node.disabled = true;
      }
      const rate = document.getElementById("rec-rate");
      if (rate) rate.textContent = "idle";
      return;
    }
    document.getElementById("record-bar").classList.add("recording--on");
    document.getElementById("live-start").disabled = true;
    document.getElementById("live-start").textContent = "Recording…";
    const rate = document.getElementById("rec-rate");
    if (rate) rate.textContent = "starting…";
    for (const id of ["rec-stop", "rec-bookmark", "rec-csv"]) {
      const node = document.getElementById(id);
      if (node) node.disabled = false;
    }
    elapsedTimer = setInterval(() => {
      const ms = Date.now() - (startedAt || Date.now());
      const seconds = Math.floor(ms / 1000);
      const text = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}.${Math.floor((ms % 1000) / 100)}`;
      const node = document.getElementById("rec-time");
      if (node) node.textContent = text;
    }, 200);
  }

  function bookmark() {
    const t = Date.now() / 1000;
    for (const chart of charts) chart.bookmark(t);
    toast(`Bookmarked ${timeOf(new Date().toISOString())}`);
  }

  function toggleFreeze(event) {
    frozen = !frozen;
    event.currentTarget.textContent = frozen ? "Resume" : "Freeze";
    toast(frozen ? "Display frozen (recording continues)" : "Display resumed");
  }

  function clearBuffers() {
    buffer.clear();
    lastValues.clear();
    lastSeen.clear();
    sampleCount = 0;
    for (const chart of charts) chart.clearBookmarks();
    renderInstruments();
  }

  function exportCsv() {
    const keys = [...buffer.keys()];
    if (!keys.length) { toast("nothing recorded", "warn"); return; }
    const rows = ["timestamp," + keys.join(",")];
    const times = new Set();
    for (const entries of buffer.values()) for (const entry of entries) times.add(entry.t);
    for (const t of [...times].sort((a, b) => a - b)) {
      const cells = keys.map((key) => {
        const entry = (buffer.get(key) || []).find((item) => item.t === t);
        return entry && entry.value !== null && entry.value !== undefined ? entry.value : "";
      });
      rows.push(`${new Date(t).toISOString()},${cells.join(",")}`);
    }
    download(`pacifica-live-${Date.now()}.csv`, rows.join("\n"), "text/csv");
    toast("CSV exported");
  }

  /* ------------------------------------------------------------ templates */

  function refreshTemplateOptions() {
    const all = loadTemplates();
    const names = Object.keys(all[source.mode] || {});
    const node = templateSelect.querySelector("select");
    clear(node);
    node.appendChild(h("option", { value: "", text: "— none —" }));
    for (const name of names) node.appendChild(h("option", { value: name, text: name }));
  }

  function saveTemplate() {
    const name = (document.getElementById("template-name").value || "").trim();
    if (!name) { toast("give the template a name", "warn"); return; }
    const all = loadTemplates();
    all[source.mode] = all[source.mode] || {};
    all[source.mode][name] = { selection: [...selection], display: source.display, interval: source.interval };
    saveTemplates(all);
    refreshTemplateOptions();
    toast(`saved template "${name}"`);
  }

  function loadTemplate() {
    const name = templateSelect.querySelector("select").value;
    if (!name) return;
    const all = loadTemplates();
    const template = (all[source.mode] || {})[name];
    if (!template) return;
    selection = [...template.selection];
    source.display = template.display || "gauges";
    if (template.interval) { source.interval = template.interval; document.getElementById("interval").value = template.interval; }
    displaySelect.querySelector("select").value = source.display;
    renderChips();
    renderInstruments();
    toast(`loaded template "${name}"`);
  }

  function deleteTemplate() {
    const name = templateSelect.querySelector("select").value;
    if (!name) return;
    const all = loadTemplates();
    if (all[source.mode]) delete all[source.mode][name];
    saveTemplates(all);
    refreshTemplateOptions();
    toast(`deleted "${name}"`, "warn");
  }

  /* --------------------------------------------------------------- replay */

  async function loadReplay(explicitId) {
    const value = explicitId || replaySelect.querySelector("select").value;
    if (!value) return;
    try {
      const payload = await api.dbSeries({ session_id: value, max_points: 2000 });
      if (!payload.available || !payload.series.length) { toast("no samples for that session", "warn"); return; }
      const series = payload.series.slice(0, 6);
      const xs = [];
      for (const serie of series) for (const [t] of serie.points) xs.push(t);
      const uniqueTimes = [...new Set(xs)].sort((a, b) => a - b);
      const byId = new Map();
      for (const serie of series) {
        const lookup = new Map(serie.points.map(([t, value_]) => [t, value_]));
        byId.set(serie.id, uniqueTimes.map((t) => (lookup.has(t) ? lookup.get(t) : null)));
      }
      // Populate the instruments with the replay's final values so a replay shows
      // gauges/bars/digital too, not just a chart, and seed the sample buffer so
      // the digital readouts get their sparklines.
      for (const serie of series) {
        const last = serie.points[serie.points.length - 1];
        meta.set(serie.id, { key: serie.id, label: serie.name || serie.id, unit: serie.unit || "" });
        if (last) lastValues.set(serie.id, { key: serie.id, label: serie.name || serie.id,
          value: last[1], unit: serie.unit || "", status: "ok" });
        lastSeen.set(serie.id, Date.now());
        buffer.set(serie.id, serie.points.slice(-48).map(([t, value_]) => ({
          t, value: value_, unit: serie.unit || "", status: "ok",
        })));
      }
      selection = series.map((serie) => serie.id);
      const chartSeries = series.map((serie, index) => ({
        key: serie.id, label: serie.name || serie.id, unit: serie.unit, colour: seriesColorValue(index),
      }));
      renderChips();
      renderInstruments();
      if (charts.length === 1) {
        charts[0].setSeries(chartSeries);
        charts[0].setRows({ xs: uniqueTimes, series: series.map((serie) => ({ values: byId.get(serie.id) })) });
      } else {
        series.forEach((serie, index) => {
          const chart = charts[index];
          if (!chart) return;
          chart.setSeries([chartSeries[index]]);
          chart.setRows({ xs: uniqueTimes, series: [{ values: byId.get(serie.id) }] });
        });
      }
      toast(`replayed ${series.length} series from session ${value}`);
    } catch (error) {
      toast(error.message, "err", 0);
    }
  }

  async function loadSessions() {
    try {
      const payload = await api.dbTable("sessions", { limit: 25, order: "id", dir: "desc" });
      const node = replaySelect.querySelector("select");
      clear(node);
      node.appendChild(h("option", { value: "", text: payload.rows.length ? "— choose —" : "no sessions stored" }));
      for (const row of payload.rows) {
        node.appendChild(h("option", { value: row.id, text: `#${row.id} · ${row.command} · ${timeOf(row.started_at)}` }));
      }
    } catch { /* offline */ }
  }

  /* ------------------------------------------------------------- wiring */

  function setSourceMode(mode) {
    source.mode = mode;
    source.ecu = null;
    document.getElementById("ecu-row").hidden = mode !== "did";
    didFreeEntry.hidden = mode !== "did";
    selection = mode === "pid" ? [...DEFAULT_PIDS] : ["F190", "F187"];
    lastValues.clear(); lastSeen.clear(); buffer.clear();
    if (mode === "did") {
      const ecuNode = ecuSelect.querySelector("select");
      clear(ecuNode);
      ecuNode.appendChild(h("option", { value: "", text: "loading…" }));
    }
    loadPicker().then(() => { refreshTemplateOptions(); renderInstruments(); });
  }

  on(sourceSelect.querySelector("select"), "change", (event) => setSourceMode(event.target.value));
  on(displaySelect.querySelector("select"), "change", (event) => { source.display = event.target.value; renderInstruments(); });
  on(ecuSelect.querySelector("select"), "change", async (event) => { source.ecu = event.target.value; await loadPicker(); renderChips(); });
  on(document.getElementById("filter"), "input", (event) => { chip_filter = event.target.value; renderChips(); });

  // Deep links: #/live?display=chart, #/live?replay=3, #/live?start=1
  const displayParam = ctx.params.get("display");
  if (displayParam && ["gauges", "bars", "digital", "chart"].includes(displayParam)) {
    source.display = displayParam;
    displaySelect.querySelector("select").value = displayParam;
  }
  const replayParam = ctx.params.get("replay");
  loadPicker().then(async () => {
    refreshTemplateOptions();
    renderInstruments();
    if (replayParam) await loadReplay(replayParam);
    if (ctx.params.get("start") === "1") start();
  });
  loadSessions();

  return {
    dispose() {
      if (stream) stream.close();
      if (streamId) api.streamStop(streamId).catch(() => {});
      if (elapsedTimer) clearInterval(elapsedTimer);
      for (const chart of charts) chart.destroy();
      charts.length = 0;
    },
    resize() { for (const chart of charts) chart.resize(); },
  };
}
