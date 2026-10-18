// DIDs / module info (#/dids) — read arbitrary DIDs and the F186–F191 block.

import { h, clear, mount, on } from "../dom.js";
import { api } from "../api.js";
import {
  card, cardBody, pill, emptyState, errorState, toast, dataTable, field, select,
  cellWithCopy, untrustedBadge,
} from "../components.js";
import { escapeControls } from "../format.js";

// The identification block plus the supplier id: real, readable DID numbers.
// The bundled dataset's per-ECU "dids" entries carry a *name* and unit, not a DID
// number, so they are shown as a reference list and never offered as readable.
const KNOWN_DIDS = ["F100", "F186", "F187", "F188", "F189", "F18A", "F18C", "F190", "F191"];

export function render(root, ctx) {
  const state = { ecu: null, selection: ["F190", "F187"], results: null, adaptations: null,
                  dataset: [], busy: false };

  const ecuSelect = select({ id: "did-ecu", label: "ECU", value: "", options: [{ value: "", label: "loading…" }] });
  const moduleField = field({ id: "did-module", label: "Module request ID", value: "7E0", mono: true });
  const busField = select({ id: "did-bus", label: "Bus", value: "can-c",
    options: [{ value: "can-c", label: "can-c" }, { value: "can-ihs", label: "can-ihs" }] });
  const timeoutField = field({ id: "did-timeout", label: "Timeout (ms)", value: "100", mono: true, type: "number" });
  const filterField = field({ id: "did-filter", label: "Filter DIDs", placeholder: "name or id…" });
  const chips = h("div.chips");
  const freeField = field({ id: "did-free", label: "Add DID", placeholder: "F190", mono: true });
  const resultsHolder = h("div");
  const adaptationsHolder = h("div");
  const datasetHolder = h("div");
  let filter = "";
  let available = KNOWN_DIDS.map((did) => ({ key: did, label: did, unit: "" }));

  const readButton = h("button.btn.btn--primary", { type: "button", text: "Read DIDs", onClick: () => read() });
  const infoButton = h("button.btn", { type: "button", text: "Read identification block",
    onClick: () => read(KNOWN_DIDS.filter((did) => did !== "F100")) });
  const adaptationsButton = h("button.btn.btn--ghost", { type: "button", text: "Show adaptations", onClick: loadAdaptations });

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "DIDs / module info" }),
      h("div.sub", { text: "Read data identifiers (UDS 0x22) from one module. Pick from the identification block or type any 4-hex DID — the bundled dataset names data items but carries no DID numbers." })));

  mount(root, head,
    card({ eyebrow: "Target", title: "Module & DIDs", body: h("div", null,
      h("div.row", null, ecuSelect, moduleField, busField, timeoutField),
      filterField, chips, h("div.row.row--spaced", freeField,
        h("button.btn.btn--sm", { type: "button", text: "Add", onClick: addFree }),
        readButton, infoButton, adaptationsButton),
      h("p.sub", { text: "DIDs are addressed by number. The bundled dataset lists per-ECU data items by name and unit but carries no DID numbers, so free entry is the way to read anything beyond the identification block." })) }),
    card({ eyebrow: "Results", title: "Data identifiers", flush: true, body: resultsHolder }),
    card({ eyebrow: "Offline · dataset", title: "Data items for this ECU", flush: true, body: datasetHolder }),
    card({ eyebrow: "Offline · dataset", title: "Adaptations (writable configuration — read-only listing)", flush: true, body: adaptationsHolder }));

  /* ------------------------------------------------------------------ data */

  async function loadEcus() {
    try {
      const { ecus } = await api.ecus();
      const node = ecuSelect.querySelector("select");
      clear(node);
      node.appendChild(h("option", { value: "", text: "— choose —" }));
      for (const ecu of ecus) {
        node.appendChild(h("option", { value: ecu.ecu,
          text: `${ecu.ecu}${ecu.name ? " — " + ecu.name : ""} (${ecu.dids} DID${ecu.dids === 1 ? "" : "s"})${ecu.request ? " · " + ecu.request : ""}` }));
      }
      const first = ecus.find((ecu) => ecu.request);
      if (first) { state.ecu = first.ecu; node.value = first.ecu; document.getElementById("did-module").value = first.request || "7E0"; }
      await loadDataset();
    } catch (error) { toast(error.message, "err", 0); }
  }

  async function loadDataset() {
    try {
      if (!state.ecu) { renderDataset(); return; }
      const payload = await api.dids(state.ecu);
      state.dataset = payload.dids || [];
    } catch { state.dataset = []; }
    renderDataset();
  }

  function renderDataset() {
    clear(datasetHolder);
    if (!state.dataset.length) {
      datasetHolder.appendChild(emptyState({ icon: "⧉", title: "No dataset data items",
        hint: state.ecu ? `The bundled dataset has no entries for ${state.ecu}.` : "Choose an ECU." }));
      return;
    }
    datasetHolder.appendChild(dataTable({
      columns: [
        { key: "name", label: "Name", render: (row) => row.name || row.kind || "—" },
        { key: "kind", label: "Kind", mono: true },
        { key: "unit", label: "Unit", render: (row) => row.unit || h("span.sub", { text: "—" }) },
      ],
      rows: state.dataset, empty: "none",
    }));
  }

  function renderChips() {
    clear(chips);
    const rows = available.filter((row) => !filter
      || row.key.toLowerCase().includes(filter) || row.label.toLowerCase().includes(filter));
    if (!rows.length) { chips.appendChild(h("span.sub", { text: "no match" })); }
    for (const row of rows.slice(0, 120)) {
      const active = state.selection.includes(row.key);
      chips.appendChild(h("button.chip", { type: "button", "aria-pressed": active ? "true" : "false",
        onClick: () => { toggle(row.key); } },
        h("span", { text: `${row.key} · ${row.label}` })));
    }
    if (state.selection.length) {
      chips.appendChild(h("span.sub", { text: `selected: ${state.selection.join(", ")}` }));
    }
  }

  function toggle(key) {
    const index = state.selection.indexOf(key);
    if (index >= 0) state.selection.splice(index, 1);
    else state.selection.push(key);
    renderChips();
  }

  function addFree() {
    const input = document.getElementById("did-free");
    const value = (input.value || "").trim().toUpperCase().replace(/^0X/, "");
    if (!/^[0-9A-F]{4}$/.test(value)) { toast("a DID is 4 hex characters, e.g. F190", "err"); return; }
    if (!state.selection.includes(value)) state.selection.push(value);
    input.value = "";
    renderChips();
  }

  /* ------------------------------------------------------------------ read */

  async function read(dids) {
    if (state.busy) { toast("a read is already running", "warn"); return; }
    const list = (dids || state.selection).slice();
    if (!list.length) { toast("select at least one DID", "warn"); return; }
    state.busy = true;
    readButton.disabled = true;
    infoButton.disabled = true;
    clear(resultsHolder);
    resultsHolder.appendChild(h("div", { class: "pad" }, h("span.sub", { text: "Reading…" })));
    try {
      const payload = await api.udsReadDid({
        module: document.getElementById("did-module").value,
        bus: busField.querySelector("select").value,
        dids: list,
        timeout_ms: Number(document.getElementById("did-timeout").value) || 100,
      });
      state.results = payload;
      renderResults();
    } catch (error) {
      clear(resultsHolder);
      resultsHolder.appendChild(h("div", { class: "pad" }, errorState(error, { onRetry: () => read(list) })));
      toast(error.message, "err", 0);
    } finally {
      state.busy = false;
      readButton.disabled = false;
      infoButton.disabled = false;
    }
  }

  function renderResults() {
    clear(resultsHolder);
    const records = state.results?.dids || [];
    if (!records.length) {
      resultsHolder.appendChild(emptyState({ icon: "⧉", title: "Nothing read", hint: "Choose a module and DIDs, then press Read." }));
      return;
    }
    resultsHolder.appendChild(h("div.toolbar", null,
      h("span.sub", { text: `module ${state.results.module} on ${state.results.bus}` }),
      state.results.vin ? h("span.mono", { text: `VIN ${state.results.vin}` }) : null));
    resultsHolder.appendChild(dataTable({
      columns: [
        { key: "did", label: "DID", mono: true },
        { key: "name", label: "Name", render: (rec) => rec.name || "—" },
        { key: "hex", label: "Hex", render: (rec) => cellWithCopy(rec.hex || "") },
        { key: "ascii", label: "ASCII", render: (rec) => h("span", null,
          h("span.mono", { text: escapeControls(rec.ascii || "") }), " ",
          rec.ascii ? untrustedBadge() : null) },
        { key: "status", label: "Status", render: (rec) => pill(rec.status, rec.status) },
        { key: "raw", label: "Raw", mono: true, render: (rec) => cellWithCopy(rec.raw || "") },
      ],
      rows: records, empty: "no rows",
      rowClass: (rec) => rec.status === "ok" ? "row--ok" : "row--warn",
    }));
    const refused = records.filter((rec) => rec.status !== "ok");
    if (refused.length) {
      resultsHolder.appendChild(h("div.pad",
        h("span.sub", { text: `${refused.length} DID(s) did not return ok — per-DID status is shown verbatim above.` })));
    }
  }

  async function loadAdaptations() {
    if (!state.ecu) return;
    try {
      const payload = await api.adaptations(state.ecu);
      clear(adaptationsHolder);
      if (!payload.adaptations.length) {
        adaptationsHolder.appendChild(emptyState({ icon: "⚙", title: "No adaptations listed", hint: `The dataset has none for ${state.ecu}.` }));
        return;
      }
      adaptationsHolder.appendChild(dataTable({
        columns: [
          { key: "name", label: "Name" }, { key: "group", label: "Group" },
          { key: "request", label: "Request", mono: true }, { key: "bit", label: "Bit", mono: true },
          { key: "size", label: "Size", mono: true },
          { key: "values", label: "Allowed values", render: (row) => (row.values || []).join(", ") || h("span.sub", { text: "numeric/free-form" }) },
        ],
        rows: payload.adaptations, empty: "none",
      }));
    } catch (error) { toast(error.message, "err", 0); }
  }

  /* ---------------------------------------------------------------- wiring */

  on(ecuSelect.querySelector("select"), "change", async (event) => {
    state.ecu = event.target.value;
    const ecu = (await api.ecus()).ecus.find((item) => item.ecu === state.ecu);
    if (ecu?.request) document.getElementById("did-module").value = ecu.request;
    await loadDataset();
  });
  on(document.getElementById("did-filter"), "input", (event) => { filter = event.target.value; renderChips(); });

  renderChips();
  renderDataset();
  loadEcus();

  return { dispose() {}, resize() {} };
}
