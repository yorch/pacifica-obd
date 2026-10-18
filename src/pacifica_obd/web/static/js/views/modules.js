// Modules & DTCs (#/modules) — discovery, the module tree, all-module DTCs and
// the offline module map. Reads only: no clear-DTC affordance exists here.

import { h, clear, mount, on } from "../dom.js";
import { api } from "../api.js";
import { navigate } from "../router.js";
import {
  card, cardBody, pill, emptyState, errorState, toast, dataTable, field, select,
  checkbox, progressBar, cellWithCopy, untrustedBadge,
} from "../components.js";
import { escapeControls, statusClass, seriesColorValue } from "../format.js";

export function render(root, ctx) {
  const timers = [];
  const state = {
    modules: null, dtc: null, info: null, map: null,
    filter: { text: "", bus: "", status: "", hasDtc: false },
    busy: false,
  };

  const busField = select({ id: "disc-bus", label: "Bus", value: "can-c",
    options: [{ value: "can-c", label: "can-c (HS 500k)" }, { value: "can-ihs", label: "can-ihs (MS 125k)" }] });
  const startField = field({ id: "disc-start", label: "Start ID", value: "700", mono: true });
  const endField = field({ id: "disc-end", label: "End ID", value: "7FF", mono: true });
  const timeoutField = field({ id: "disc-timeout", label: "Timeout (ms)", value: "100", mono: true, type: "number", min: 1, max: 65535 });
  const identifyBox = checkbox({ id: "disc-identify", label: "Identify (VIN/part)" });
  const progress = progressBar();
  const feed = h("div.feed", null, h("div.sub", { text: "No scan yet." }));

  const discoverButton = h("button.btn.btn--primary", { type: "button", text: "Discover", onClick: () => run("modules") });
  const dtcButton = h("button.btn", { type: "button", text: "All-module DTCs", onClick: () => run("dtc") });
  const infoButton = h("button.btn", { type: "button", text: "Identification", onClick: () => run("info") });
  const scanButton = h("button.btn", { type: "button", text: "Scan everything", onClick: () => run("all") });
  const cancelButton = h("button.btn.btn--danger.btn--sm", { type: "button", text: "Cancel", disabled: true, onClick: cancelActive });

  const filterText = field({ id: "mod-filter", label: "Filter", placeholder: "module, code, name…" });
  const filterBus = select({ id: "mod-bus", label: "Bus", value: "",
    options: [{ value: "", label: "all" }, { value: "can-c", label: "can-c" }, { value: "can-ihs", label: "can-ihs" }] });
  const filterStatus = select({ id: "mod-status", label: "Status", value: "",
    options: [{ value: "", label: "any" }, { value: "ok", label: "ok" }, { value: "no_response", label: "no response" },
      { value: "negative", label: "negative" }, { value: "malformed", label: "malformed" }] });
  const filterDtc = checkbox({ id: "mod-hasdtc", label: "has DTC only" });

  const treeHolder = h("div");
  const mapHolder = h("div");

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Modules & DTCs" }),
      h("div.sub", { text: "Probe the CAN buses for UDS modules, then read each module's trouble codes and identification block. Every target is kept, including the ones that did not answer." })));

  const formCard = card({ eyebrow: "UDS discovery", title: "Scan", body: h("div", null,
    h("div.row", null, busField, startField, endField, timeoutField, identifyBox),
    h("div.row", { style: { marginTop: "10px" } }, discoverButton, dtcButton, infoButton, scanButton, cancelButton),
    h("div", { style: { marginTop: "10px" } }, progress),
    h("div", { style: { marginTop: "10px" } }, feed)) });

  const filterCard = card({ eyebrow: "Filter", title: "Results", body: h("div.row", null,
    filterText, filterBus, filterStatus, filterDtc),
    actions: h("span.sub", { id: "mod-summary", text: "nothing scanned" }) });

  const mapCard = card({ eyebrow: "Offline", title: "Module map (bundled dataset)",
    actions: h("button.btn.btn--sm", { type: "button", text: "Load map", onClick: loadMap }) });

  mount(root, head, formCard, filterCard, card({ eyebrow: "Tree", title: "Modules & codes", body: treeHolder }),
    mapCard, card({ eyebrow: "DTCs", title: "Flat list", flush: true, body: h("div", { id: "dtc-flat" }) }));

  /* ---------------------------------------------------------------- actions */

  function params() {
    return {
      bus: busField.querySelector("select").value,
      start: document.getElementById("disc-start").value,
      end: document.getElementById("disc-end").value,
      timeout_ms: Number(document.getElementById("disc-timeout").value) || 100,
      identify: document.getElementById("disc-identify").checked,
    };
  }

  async function cancelActive() {
    const id = ctx.status?.status?.job?.holder_id;
    if (!id) { toast("nothing running", "warn"); return; }
    try { await api.cancelJob(id); toast("Cancel requested", "warn"); } catch (error) { toast(error.message, "err"); }
  }

  function log(text, status) {
    feed.appendChild(h("div.feed__row", null,
      h("span.feed__ts", { text: new Date().toLocaleTimeString([], { hour12: false }) }),
      h("span", { text }),
      status ? pill(status, status) : null));
    feed.scrollTop = feed.scrollHeight;
  }

  function pollProgress() {
    const tick = setInterval(async () => {
      try {
        const { jobs } = await api.jobs();
        for (const job of jobs.filter((j) => j.state === "running")) {
          if (job.progress?.request_id) {
            progress.update((Number(String(job.progress.request_id).slice(-2), 16) || 0) / 255);
          }
        }
      } catch { /* ignore */ }
    }, 500);
    timers.push(tick);
    return () => clearInterval(tick);
  }

  async function run(which) {
    if (state.busy) return;
    state.busy = true;
    const stopPolling = pollProgress();
    for (const button of [discoverButton, dtcButton, infoButton, scanButton]) button.disabled = true;
    cancelButton.disabled = false;
    const p = params();
    try {
      if (which === "modules" || which === "all") {
        log(`discovering ${p.bus} ${p.start}–${p.end}…`);
        state.modules = await api.udsModules(p);
        log(`${state.modules.modules.length} module(s) responded`, state.modules.modules.length ? "ok" : "no_response");
      }
      if (which === "dtc" || which === "all") {
        log("reading DTCs (UDS 0x19 0x02)…");
        state.dtc = await api.udsDtcAll(p);
        log(`${state.dtc.total_dtcs} DTC(s) across ${state.dtc.modules.length} module(s)`,
          state.dtc.total_dtcs ? "negative" : "ok");
      }
      if (which === "info" || which === "all") {
        log("reading the identification block (F186–F191)…");
        state.info = await api.udsModuleInfo(p);
        log(`${state.info.modules.length} identification rows`);
      }
      progress.update(1);
      renderTree();
    } catch (error) {
      log(`${error.code || "error"}: ${error.message}`);
      feed.appendChild(errorState(error, { onRetry: () => run(which) }));
      toast(error.message, "err", 0);
    } finally {
      stopPolling();
      state.busy = false;
      cancelButton.disabled = true;
      for (const button of [discoverButton, dtcButton, infoButton, scanButton]) button.disabled = false;
      ctx.refreshStatus();
    }
  }

  async function loadMap() {
    try {
      state.map = await api.moduleMap();
      renderMap();
    } catch (error) { toast(error.message, "err", 0); }
  }

  /* ----------------------------------------------------------------- render */

  function mergedModules() {
    const byId = new Map();
    const push = (bus, request_id, extra) => {
      const key = `${bus}:${request_id}`;
      const row = byId.get(key) || { bus, request_id, dtcs: [], status: null, identification: {} };
      Object.assign(row, extra);
      byId.set(key, row);
      return row;
    };
    for (const module of state.modules?.modules || []) {
      push(params().bus, module.request_id, { response_id: module.response_id, kind: module.kind, vin: module.vin, part_number: module.part_number });
    }
    for (const record of state.dtc?.modules || []) {
      const row = push(record.bus, record.request_id, { response_id: record.response_id });
      row.status = record.status;
      row.dtcs = record.dtcs || [];
      row.raw = record.raw;
    }
    for (const record of state.info?.modules || []) {
      const row = push(record.bus, record.module, {});
      row.identification[record.did] = record;
    }
    return [...byId.values()];
  }

  function passFilter(row) {
    const { text, bus, status, hasDtc } = state.filter;
    if (bus && row.bus !== bus) return false;
    if (status && row.status && row.status !== status) return false;
    if (hasDtc && !(row.dtcs || []).length) return false;
    if (text) {
      const haystack = [row.request_id, row.response_id, row.status,
        ...(row.dtcs || []).map((d) => `${d.code} ${d.description || ""}`),
        ...Object.entries(row.identification).map(([did, rec]) => `${did} ${rec.name || ""} ${rec.ascii || ""}`)]
        .join(" ").toLowerCase();
      if (!haystack.includes(text.toLowerCase())) return false;
    }
    return true;
  }

  function renderTree() {
    const rows = mergedModules();
    const summary = document.getElementById("mod-summary");
    if (summary) summary.textContent = rows.length ? `${rows.length} target(s)` : "nothing scanned";
    clear(treeHolder);
    if (!rows.length) {
      treeHolder.appendChild(emptyState({ icon: "⌗", title: "No modules scanned yet",
        hint: "Press Discover. If nothing answers, check the ignition is ON, the bus is right, and the SGW is not blocking.",
        action: h("button.btn.btn--primary", { type: "button", text: "Discover", onClick: () => run("modules") }) }));
      clear(document.getElementById("dtc-flat"));
      return;
    }
    const visible = rows.filter(passFilter);
    const byBus = new Map();
    for (const row of visible) {
      if (!byBus.has(row.bus)) byBus.set(row.bus, []);
      byBus.get(row.bus).push(row);
    }
    for (const [bus, busRows] of [...byBus.entries()].sort()) {
      const group = h("div", null, h("h3", { text: `${bus} · ${busRows.length} module(s)` }));
      for (const row of busRows.sort((a, b) => a.request_id.localeCompare(b.request_id))) {
        group.appendChild(moduleNode(row));
      }
      treeHolder.appendChild(group);
    }
    renderFlatDtc(rows);
  }

  function moduleNode(row) {
    const dtcCount = (row.dtcs || []).length;
    const details = h("details", { open: dtcCount > 0 },
      h("summary", null,
        h("span.mono", { text: `${row.request_id} → ${row.response_id || "?"}` }),
        " ", h("span.sub", { text: row.kind || "module" }),
        " ", row.status ? pill(row.status, row.status) : null,
        " ", h("span", { class: `pill ${dtcCount ? "pill--crit" : "pill--ok"}`, text: `${dtcCount} DTC` })));
    const body = h("div", { style: { paddingLeft: "12px" } });
    if (row.dtcs?.length) {
      body.appendChild(dataTable({
        columns: [
          { key: "code", label: "Code", mono: true, render: (dtc) => h("span", null,
            h("b", { text: dtc.code }), dtc.failure_type ? `-${dtc.failure_type}` : "") },
          { key: "description", label: "Description", render: (dtc) => dtc.description || h("span.sub", { text: "no dataset text" }) },
          { key: "status", label: "Status", render: (dtc) => pill(dtc.status, dtc.status) },
          { key: "flags", label: "Flags", render: (dtc) => h("span", null,
            (dtc.flags || []).map((flag) => h("span", { class: `pill ${statusClass(flag)}`, text: flag, style: { marginRight: "4px" } }))) },
          { key: "action", label: "AI", render: (dtc) => h("a.pill.pill--ghost", {
            title: "ask the read-only assistant", text: "explain",
            href: `#/assistant?ask=${encodeURIComponent(`Explain DTC ${dtc.code} in one or two sentences.`)}` }) },
        ],
        rows: row.dtcs,
      }));
    } else {
      body.appendChild(h("p.sub", { text: row.status === "no_response" ? "No response from this module." : "No codes." }));
    }
    if (Object.keys(row.identification).length) {
      body.appendChild(h("h3", { text: "Identification" }));
      body.appendChild(dataTable({
        columns: [
          { key: "did", label: "DID", mono: true },
          { key: "name", label: "Name" },
          { key: "hex", label: "Hex", render: (rec) => cellWithCopy(rec.hex || "") },
          { key: "ascii", label: "ASCII", render: (rec) => h("span", null,
            h("span.mono", { text: escapeControls(rec.ascii || "") }), " ", untrustedBadge()) },
          { key: "status", label: "Status", render: (rec) => pill(rec.status, rec.status) },
        ],
        rows: Object.values(row.identification),
      }));
    }
    details.appendChild(body);
    return details;
  }

  function renderFlatDtc(rows) {
    const holder = document.getElementById("dtc-flat");
    clear(holder);
    const flat = [];
    for (const row of rows.filter(passFilter)) {
      for (const dtc of row.dtcs || []) {
        flat.push({ ...dtc, module: row.request_id, bus: row.bus, status: dtc.status || row.status });
      }
    }
    if (!flat.length) {
      holder.appendChild(emptyState({ icon: "✓", title: "No trouble codes",
        hint: rows.length ? "Neither stored nor pending codes were reported by the modules that answered." : "Run a scan first." }));
      return;
    }
    holder.appendChild(dataTable({
      columns: [
        { key: "bus", label: "Bus" },
        { key: "module", label: "Module", mono: true },
        { key: "code", label: "Code", mono: true, render: (d) => h("b", { text: d.code }) },
        { key: "failure_type", label: "FTB", mono: true },
        { key: "description", label: "Description", render: (d) => d.description || "—" },
        { key: "status", label: "Status", render: (d) => pill(d.status, d.status) },
        { key: "flags", label: "Flags", render: (d) => (d.flags || []).join(", ") || "—" },
      ],
      rows: flat, empty: "No codes.",
      rowClass: (d) => statusClass(d.status) === "pill--crit" ? "row--crit" : "",
    }));
  }

  function renderMap() {
    clear(mapHolder);
    if (!state.map) return;
    for (const [bus, rows] of Object.entries(state.map.bundled || {}).sort()) {
      mapHolder.appendChild(h("h3", { text: `${bus} · ${rows.length} known module(s)` }));
      mapHolder.appendChild(dataTable({
        columns: [
          { key: "module", label: "Module" }, { key: "ecu", label: "ECU", mono: true },
          { key: "request", label: "Request", mono: true }, { key: "response", label: "Response", mono: true },
          { key: "powertrain", label: "Powertrain" },
        ],
        rows, empty: "none",
      }));
    }
    if (state.map.external) {
      mapHolder.appendChild(h("h3", { text: `external ECU DB · ${state.map.external.path}` }));
      mapHolder.appendChild(dataTable({
        columns: [
          { key: "ecu", label: "ECU" }, { key: "name", label: "Name" }, { key: "bus", label: "Bus" },
          { key: "request", label: "Request", mono: true }, { key: "response", label: "Response", mono: true },
        ],
        rows: state.map.external.modules, empty: "none",
      }));
    }
  }

  on(document.getElementById("mod-filter"), "input", (event) => { state.filter.text = event.target.value; renderTree(); });
  on(filterBus.querySelector("select"), "change", (event) => { state.filter.bus = event.target.value; renderTree(); });
  on(filterStatus.querySelector("select"), "change", (event) => { state.filter.status = event.target.value; renderTree(); });
  on(filterDtc.querySelector("input"), "change", (event) => { state.filter.hasDtc = event.target.checked; renderTree(); });

  renderTree();

  // Deep link: #/modules?auto=1 runs discovery + DTCs + identification on load.
  if (ctx.params.get("auto") === "1") run("all");

  return {
    dispose() { for (const timer of timers) clearInterval(timer); },
    resize() {},
  };
}
