// Database browser (#/db) — every table the tool has ever stored, with filters,
// a paginated grid, a detail drawer, CSV export, and a charted replay of samples.
//
// Strictly read-only: the server opens SQLite `mode=ro` and only ever SELECTs.

import { h, clear, mount, on, download } from "../dom.js";
import { api, rawResponse, query } from "../api.js";
import { navigate } from "../router.js";
import {
  card, pill, emptyState, errorState, toast, dataTable, field,
  drawer, icon, loadingState,
} from "../components.js";
import { dateTimeOf, truncate } from "../format.js";
import { TimeSeriesChart } from "../charts.js";

const FIELD_SPECS = {
  vehicles: [
    ["vin", "VIN"], ["make", "Make"], ["model", "Model"], ["nickname", "Nickname"], ["q", "Search"],
  ],
  sessions: [
    ["vin", "VIN"], ["command", "Command"], ["status", "Status"], ["from", "From (ts ≥)"], ["to", "To (ts ≤)"],
  ],
  events: [
    ["session_id", "Session"], ["kind", "Kind"], ["name", "Name"], ["from", "From"], ["to", "To"],
  ],
  modules: [
    ["vin", "VIN"], ["bus", "Bus"], ["request_id", "Request"], ["kind", "Kind"], ["q", "Search"],
  ],
  dtcs: [
    ["vin", "VIN"], ["bus", "Bus"], ["module", "Module"], ["code", "Code"], ["status", "Status"],
    ["failure_type", "FTB"], ["from", "From"], ["to", "To"],
  ],
  readings: [
    ["vin", "VIN"], ["module", "Module"], ["did", "DID"], ["name", "Name"], ["category", "Category"], ["status", "Status"],
  ],
  samples: [
    ["vin", "VIN"], ["module", "Module"], ["did", "DID"], ["name", "Name"], ["status", "Status"],
    ["from", "From"], ["to", "To"],
  ],
};

const BIG_COLUMNS = new Set(["detail", "args", "raw", "flags"]);

export async function render(root, ctx) {
  const state = {
    schema: null, table: ctx.params.get("table") || "vehicles",
    filters: {}, limit: 50, offset: 0, order: null, dir: "desc",
    rows: [], total: 0, dbPath: null, available: true,
  };
  const timers = [];

  const tabs = h("div.tabs", { role: "tablist" });
  const filtersHolder = h("div.row");
  const gridHolder = h("div", { id: "db-grid" });
  const pagerHolder = h("div.pager");

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Database" }),
      h("div.sub", { id: "db-path", text: "local SQLite database" })),
    h("div.row", null,
      h("button.btn", { type: "button", text: "Export CSV", onClick: exportCsv }),
      h("button.btn", { type: "button", text: "Refresh", onClick: () => load() })));

  mount(root, head,
    card({ eyebrow: "Tables", title: "Browse", flush: true, body: h("div", null, tabs,
      h("div", { class: "pad-t" }, filtersHolder)) }),
    card({ eyebrow: "Rows", title: "Records", flush: true, body: h("div", null, gridHolder, pagerHolder) }));

  /* ------------------------------------------------------------------ load */

  async function loadSchema() {
    try {
      state.schema = await api.dbSchema();
      state.available = state.schema.available;
      state.dbPath = state.schema.db_path;
      document.getElementById("db-path").textContent = state.available
        ? `read-only · ${state.schema.db_path}`
        : `no database at ${state.schema.db_path}`;
      renderTabs();
    } catch (error) { toast(error.message, "err", 0); }
  }

  function renderTabs() {
    clear(tabs);
    const tables = state.schema?.tables || [];
    for (const table of tables) {
      tabs.appendChild(h("button.tab", { role: "tab", type: "button",
        "aria-selected": table.name === state.table ? "true" : "false",
        onClick: () => { state.table = table.name; state.offset = 0; state.order = null; state.filters = {};
          navigate("/db", { table: table.name }); renderTabs(); renderFilters(); load(); } },
        h("span", { text: table.name }),
        h("span.count", { text: String(table.rows) })));
    }
    if (!tables.length) {
      tabs.appendChild(h("span.sub", { text: "no tables" }));
    }
  }

  function renderFilters() {
    clear(filtersHolder);
    const spec = FIELD_SPECS[state.table] || [];
    for (const [key, label] of spec) {
      const input = field({ id: `db-f-${key}`, label, mono: /id|vin|did|code|bus|ftb/i.test(key),
        value: state.filters[key] || "" });
      on(input.querySelector("input"), "change", (event) => {
        state.filters[key] = event.target.value; state.offset = 0; load();
      });
      on(input.querySelector("input"), "keydown", (event) => {
        if (event.key === "Enter") { state.filters[key] = event.target.value; state.offset = 0; load(); }
      });
      filtersHolder.appendChild(input);
    }
    filtersHolder.appendChild(h("div.field.field--tight", null, h("label", { text: "Rows" }),
      h("select.mono", { id: "db-limit" },
        [25, 50, 100, 250].map((n) => h("option", { value: n, text: String(n), selected: n === state.limit ? true : undefined })))));
    on(document.getElementById("db-limit"), "change", (event) => { state.limit = Number(event.target.value); state.offset = 0; load(); });
    filtersHolder.appendChild(h("button.btn.btn--sm.btn--ghost", { type: "button", text: "Clear filters",
      onClick: () => { state.filters = {}; state.offset = 0; renderFilters(); load(); } }));
  }

  async function load() {
    if (!state.available) {
      clear(gridHolder);
      gridHolder.appendChild(emptyState({ icon: "▦", title: "No local database found",
        hint: `${state.dbPath || ""} — persistence may be disabled (--no-log / PACIFICA_OBD_NO_LOG=1).` }));
      clear(pagerHolder);
      return;
    }
    clear(gridHolder);
    gridHolder.appendChild(loadingState("Reading rows…", { panel: true }));
    try {
      const payload = await api.dbTable(state.table, {
        ...state.filters, limit: state.limit, offset: state.offset,
        order: state.order || undefined, dir: state.dir,
      });
      state.rows = payload.rows; state.total = payload.total; state.columns = payload.columns;
      renderGrid();
      renderPager();
    } catch (error) {
      clear(gridHolder);
      gridHolder.appendChild(errorState(error, { onRetry: load }));
    }
  }

  function renderGrid() {
    clear(gridHolder);
    if (!state.rows.length) {
      gridHolder.appendChild(emptyState({ icon: "▦", title: "No rows match", hint: "Adjust the filters above." }));
      return;
    }
    const sortBy = (name) => {
      state.dir = state.order === name && state.dir === "desc" ? "asc" : "desc";
      state.order = name; state.offset = 0; load();
    };
    const columns = (state.columns || Object.keys(state.rows[0])).map((name) => {
      const mono = /id|vin|did|code|bus|module|request|ts|hex|raw|ftb|type/i.test(name);
      return {
        key: name, label: headerOf(name), mono,
        num: /^(id|session_id)$/.test(name),
        render: (row) => renderCell(name, row, mono),
        sortable: true,
        sorted: state.order === name ? state.dir : null,
        onSort: () => sortBy(name),
      };
    });
    if (state.table === "samples") {
      columns.push({ key: "__chart", label: "", render: (row) => h("button.btn.btn--sm", {
        type: "button", text: "Chart",
        onClick: (event) => { event.stopPropagation(); chartRow(row); } }) });
    }
    gridHolder.appendChild(dataTable({
      columns, rows: state.rows, empty: "no rows",
      onRowClick: (row) => openDrawer(row),
    }));
  }

  // A database column is snake_case; the header shows words, with the acronyms
  // the rest of the UI already uses (VIN, DID, DTC, FTB).
  const ACRONYMS = new Set(["vin", "id", "did", "dtc", "ftb", "uds", "ecu", "gps", "url", "api"]);
  function headerOf(name) {
    return name.split("_").map((word) => (ACRONYMS.has(word) ? word.toUpperCase()
      : word.charAt(0).toUpperCase() + word.slice(1))).join(" ");
  }

  function renderCell(name, row, mono) {
    const value = row[name];
    if (value === null || value === undefined) return h("span.sub", { text: "—" });
    if (BIG_COLUMNS.has(name)) {
      const text = String(value);
      return h("span", null, h("span.mono", { text: truncate(text, 60), title: text }), " ",
        h("button.cellcopy", { type: "button", title: "Copy", "aria-label": "Copy this value",
          onClick: (event) => { event.stopPropagation(); navigator.clipboard?.writeText(text); } },
          icon("copy", { size: 13 })));
    }
    // A timestamp reads as prose time, the same shape the dashboard shows.
    if (/(^|_)(at|seen|ts)$/.test(name)) return h("span", { text: dateTimeOf(value) });
    if (/status/.test(name)) return pill(value, value);
    if (typeof value === "string" && value.length > 60) {
      return h("span.mono", { text: truncate(value, 60), title: value });
    }
    return mono ? h("span.mono", { text: String(value) }) : h("span", { text: String(value) });
  }

  function renderPager() {
    clear(pagerHolder);
    const from = state.total ? state.offset + 1 : 0;
    const to = Math.min(state.offset + state.limit, state.total);
    pagerHolder.appendChild(h("span", { text: `${from}–${to} of ${state.total}` }));
    pagerHolder.appendChild(h("button.btn.btn--sm", { type: "button", disabled: state.offset <= 0 ? true : undefined,
      title: "Previous page", "aria-label": "Previous page",
      onClick: () => { state.offset = Math.max(0, state.offset - state.limit); load(); } },
      icon("chevron-left", { size: 13 }), h("span", { text: "Prev" })));
    pagerHolder.appendChild(h("button.btn.btn--sm", { type: "button", disabled: to >= state.total ? true : undefined,
      title: "Next page", "aria-label": "Next page",
      onClick: () => { state.offset += state.limit; load(); } },
      h("span", { text: "Next" }), icon("chevron-right", { size: 13 })));
  }

  /* ---------------------------------------------------------------- drawer */

  function openDrawer(row) {
    const content = h("div", null,
      h("h3", { text: "Row" }),
      h("dl.kv", null, Object.entries(row).flatMap(([key, value]) => [
        h("dt", { text: key }),
        h("dd", { text: truncate(String(value ?? "—"), 400) }),
      ])));
    if (state.table === "sessions") {
      const holder = h("div", null, loadingState("Loading children…"));
      content.appendChild(h("h3", { text: "Children" }));
      content.appendChild(holder);
      api.dbSession(row.id).then((payload) => {
        clear(holder);
        for (const [name, rows] of Object.entries(payload.rows || {})) {
          holder.appendChild(h("div.sub", { text: `${name}: ${rows.length}` }));
          if (rows.length) holder.appendChild(h("pre.json", { text: JSON.stringify(rows.slice(0, 20), null, 2) }));
        }
      }).catch((error) => { clear(holder); holder.appendChild(errorState(error)); });
    }
    if (state.table === "vehicles" && row.vin) {
      const holder = h("div", null, loadingState("Loading summary…"));
      content.appendChild(h("h3", { text: "Summary" }));
      content.appendChild(holder);
      api.dbVehicleSummary(row.vin).then((payload) => {
        clear(holder);
        holder.appendChild(h("div.sub", { text: Object.entries(payload.counts || {}).map(([key, n]) => `${key}: ${n}`).join(" · ") }));
        holder.appendChild(h("pre.json", { text: JSON.stringify(payload.dtcs?.slice(0, 20) || [], null, 2) }));
      }).catch((error) => { clear(holder); holder.appendChild(errorState(error)); });
    }
    content.appendChild(h("pre.json", { text: JSON.stringify(row, null, 2) }));
    drawer({ title: `${state.table} row #${row.id ?? ""}`, content });
  }

  async function chartRow(row) {
    const holder = h("div", null, loadingState("Loading the stored series…"));
    drawer({ title: `Series · ${row.module || "?"}:${row.did || "?"}`, content: holder });
    try {
      const payload = await api.dbSeries({ vin: row.vin, module: row.module, did: row.did, max_points: 2000 });
      const series = payload.series.slice(0, 6);
      if (!series.length) { clear(holder); holder.appendChild(emptyState({ icon: "◉", title: "No numeric samples", hint: "This signal has no stored numbers to chart." })); return; }
      clear(holder);
      series.forEach((serie) => {
        const wrap = h("div", { class: "mb-8" },
          h("div.eyebrow", { text: `${serie.name || serie.id}${serie.unit ? " (" + serie.unit + ")" : ""} · ${serie.raw_count} raw → ${serie.points.length} points${serie.downsampled ? " (lttb)" : ""}` }),
          h("div.legend"), h("div.chart-holder"));
        holder.appendChild(wrap);
        const chart = new TimeSeriesChart(wrap.querySelector(".chart-holder"), { height: 200 });
        chart.setSeries([{ key: serie.id, label: serie.name || serie.id, unit: serie.unit }]);
        chart.setRows({ xs: serie.points.map(([t]) => t), series: [{ values: serie.points.map(([, v]) => v) }] });
        timers.push({ chart });
      });
    } catch (error) {
      clear(holder);
      holder.appendChild(errorState(error));
    }
  }

  /* ------------------------------------------------------------------- csv */

  async function exportCsv() {
    try {
      const response = await rawResponse(`/api/db/csv${query({ table: state.table, ...state.filters })}`);
      const text = await response.text();
      download(`pacifica-${state.table}.csv`, text, "text/csv");
      toast("CSV exported");
    } catch (error) { toast(error.message, "err", 0); }
  }

  /* ------------------------------------------------------------------ boot */

  await loadSchema();
  renderFilters();
  if (state.available) await load();
  else await load();

  return {
    dispose() {
      for (const entry of timers) if (entry.chart) entry.chart.destroy();
      timers.length = 0;
    },
    resize() { for (const entry of timers) if (entry.chart) entry.chart.resize(); },
  };
}
