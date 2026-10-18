// Reports (#/reports) — generate a dump from the vehicle or from a stored
// session, preview/print/download it (json/xml/html/zip), and compare sessions.

import { h, clear, mount, on, downloadBlob } from "../dom.js";
import { api, rawResponse } from "../api.js";
import {
  card, cardBody, pill, emptyState, errorState, toast, dataTable, field, select,
  checkbox,
} from "../components.js";
import { dateTimeOf, timeOf } from "../format.js";

export function render(root, ctx) {
  const state = { reports: [], sessions: [] };

  const busField = select({ id: "rep-bus", label: "Bus", value: "both",
    options: [{ value: "both", label: "both" }, { value: "can-c", label: "can-c" }, { value: "can-ihs", label: "can-ihs" }] });
  const startField = field({ id: "rep-start", label: "Start ID", value: "700", mono: true });
  const endField = field({ id: "rep-end", label: "End ID", value: "7FF", mono: true });
  const timeoutField = field({ id: "rep-timeout", label: "Timeout (ms)", value: "100", mono: true, type: "number" });
  const genericBox = checkbox({ id: "rep-generic", label: "Generic pass", checked: true });
  const udsBox = checkbox({ id: "rep-uds", label: "UDS pass", checked: true });
  const formats = { json: true, xml: true, html: true };
  const formatRow = h("div.row", null, ["json", "xml", "html"].map((fmt) =>
    h("button.chip", { type: "button", "aria-pressed": "true", onClick: (event) => {
      formats[fmt] = !formats[fmt];
      event.currentTarget.setAttribute("aria-pressed", formats[fmt] ? "true" : "false");
    } }, h("span", { text: fmt }))));

  const generateButton = h("button.btn.btn--primary", { type: "button", text: "Generate from vehicle", onClick: generate });
  const sessionField = select({ id: "rep-session", label: "Stored session", value: "", options: [{ value: "", label: "loading…" }] });
  const fromSessionButton = h("button.btn", { type: "button", text: "From stored session", onClick: fromSession });

  const listHolder = h("div", { id: "rep-list" });
  const previewHolder = h("div");

  const compareA = select({ id: "cmp-a", label: "Session A", value: "", options: [] });
  const compareB = select({ id: "cmp-b", label: "Session B", value: "", options: [] });
  const compareButton = h("button.btn", { type: "button", text: "Compare", onClick: compare });
  const compareHolder = h("div");

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Reports" }),
      h("div.sub", { text: "A report is written under the tool's data directory and can be previewed, printed to PDF from your browser, downloaded, or zipped." })));

  mount(root, head,
    card({ eyebrow: "Generate", title: "From the vehicle", body: h("div", null,
      h("div.row", null, busField, startField, endField, timeoutField, genericBox, udsBox),
      h("div.row.row--spaced", formatRow, generateButton)) }),
    card({ eyebrow: "Generate", title: "From a stored session (no adapter)", body: h("div.row", null, sessionField, fromSessionButton) }),
    card({ eyebrow: "This session", title: "Generated reports", flush: true, body: listHolder }),
    card({ eyebrow: "Preview", title: "Review report", body: previewHolder }),
    card({ eyebrow: "Compare", title: "Structural diff of two sessions", body: h("div", null,
      h("div.row", null, compareA, compareB, compareButton), compareHolder) }));

  /* ------------------------------------------------------------------ list */

  async function refreshList() {
    try {
      const payload = await api.reports();
      state.reports = payload.reports;
      renderList();
    } catch (error) { toast(error.message, "err", 0); }
  }

  function renderList() {
    clear(listHolder);
    if (!state.reports.length) {
      listHolder.appendChild(emptyState({ icon: "⎙", title: "Nothing captured yet",
        hint: "Generate a report from the vehicle or from a stored session." }));
      return;
    }
    listHolder.appendChild(dataTable({
      columns: [
        { key: "id", label: "ID", mono: true },
        { key: "source", label: "Source" },
        { key: "created", label: "Created", render: (row) => dateTimeOf(row.created) },
        { key: "vin", label: "VIN", mono: true, render: (row) => row.vehicle?.vin || "—" },
        { key: "totals", label: "Totals", render: (row) => `modules ${row.totals?.modules ?? 0} · DTCs ${row.totals?.dtcs ?? 0}` },
        { key: "aborted", label: "Partial", render: (row) => row.aborted ? pill("partial", "partial") : pill("complete", "ok") },
        { key: "actions", label: "", render: (row) => h("span.row", null,
            h("button.btn.btn--sm", { type: "button", text: "Preview", onClick: () => preview(row) }),
            h("button.btn.btn--sm", { type: "button", text: "Print", onClick: () => printReport(row) }),
            row.formats.includes("json") ? h("button.btn.btn--sm.btn--ghost", { type: "button", text: "JSON", onClick: () => save(row, "json") }) : null,
            row.formats.includes("xml") ? h("button.btn.btn--sm.btn--ghost", { type: "button", text: "XML", onClick: () => save(row, "xml") }) : null,
            row.formats.includes("html") ? h("button.btn.btn--sm.btn--ghost", { type: "button", text: "HTML", onClick: () => save(row, "html") }) : null,
            h("button.btn.btn--sm.btn--ghost", { type: "button", text: "ZIP", onClick: () => save(row, "zip") })) },
      ],
      rows: state.reports, empty: "no reports",
    }));
  }

  async function refreshMeta() {
    try {
      const payload = await api.reports();
      state.reports = payload.reports;
      renderList();
    } catch (error) { toast(error.message, "err", 0); }
  }

  /* -------------------------------------------------------------- generate */

  async function generate() {
    const selected = Object.entries(formats).filter(([, on_]) => on_).map(([fmt]) => fmt);
    if (!selected.length) { toast("choose at least one format", "warn"); return; }
    generateButton.disabled = true;
    generateButton.textContent = "Generating…";
    try {
      const meta = await api.reportDump({
        bus: busField.querySelector("select").value,
        start: document.getElementById("rep-start").value,
        end: document.getElementById("rep-end").value,
        timeout_ms: Number(document.getElementById("rep-timeout").value) || 100,
        generic: document.getElementById("rep-generic").checked,
        uds: document.getElementById("rep-uds").checked,
        formats: selected,
      });
      toast(meta.aborted ? `Report written (partial): ${meta.aborted}` : "Report written", meta.aborted ? "warn" : "ok");
      await refreshMeta();
      preview(meta);
    } catch (error) {
      toast(error.message, "err", 0);
    } finally {
      generateButton.disabled = false;
      generateButton.textContent = "Generate from vehicle";
    }
  }

  async function fromSession() {
    const id = sessionField.querySelector("select").value;
    if (!id) { toast("choose a session", "warn"); return; }
    try {
      const meta = await api.reportFromSession({ session_id: Number(id), formats: ["json", "html"] });
      toast("Report rebuilt from the stored session");
      await refreshMeta();
      preview(meta);
    } catch (error) { toast(error.message, "err", 0); }
  }

  // Report documents are addressed by an unguessable id and exempt from the
  // bearer check (a browser cannot attach a header to an iframe/anchor
  // navigation). Everything that acts still needs the token; see web/app.py.
  function reportDocumentUrl(row, kind) {
    if (kind === "zip") return `/api/reports/${row.id}/zip`;
    if (kind === "html") return `/api/reports/${row.id}/html`;
    return `/api/reports/${row.id}/download/${kind}`;
  }

  function preview(row) {
    clear(previewHolder);
    previewHolder.appendChild(h("div.row", null,
      h("span.mono", { text: row.id }),
      row.aborted ? pill("partial", "partial") : pill("complete", "ok"),
      h("a.btn.btn--sm.btn--ghost", { href: reportDocumentUrl(row, "html"), target: "_blank",
        rel: "noopener", text: "Open in a tab" }),
      h("button.btn.btn--sm", { type: "button", text: "Print / Save as PDF", onClick: () => printReport(row) })));
    previewHolder.appendChild(h("iframe", { title: `Report ${row.id}`,
      src: reportDocumentUrl(row, "html"),
      style: { width: "100%", height: "620px", border: "1px solid var(--border)",
               borderRadius: "8px", background: "#fff" } }));
  }

  function printReport(row) {
    // window.print() on the report document itself, via a same-origin iframe.
    const frame = h("iframe", { title: "print", src: reportDocumentUrl(row, "html"),
      style: { position: "fixed", right: "0", bottom: "0", width: "1px", height: "1px",
               opacity: "0", border: "0" } });
    frame.addEventListener("load", () => {
      try { frame.contentWindow.focus(); frame.contentWindow.print(); }
      catch (error) { toast(`could not print: ${error.message}`, "warn"); }
      setTimeout(() => frame.remove(), 2000);
    });
    document.body.appendChild(frame);
  }

  async function save(row, kind) {
    // Download through fetch so a failure surfaces as a toast, not as a file
    // containing an error body.
    try {
      const response = await rawResponse(reportDocumentUrl(row, kind));
      const extension = kind === "zip" ? "zip" : kind;
      downloadBlob(`pacifica-report-${row.id}.${extension}`, await response.blob());
      toast(`${extension.toUpperCase()} downloaded`);
    } catch (error) {
      toast(`${error.code || "error"}: ${error.message}`, "err", 0);
    }
  }

  /* --------------------------------------------------------------- compare */

  async function loadSessions() {
    try {
      const payload = await api.dbTable("sessions", { limit: 50, order: "id", dir: "desc" });
      state.sessions = payload.rows;
      for (const [node, extra] of [[sessionField, false], [compareA, false], [compareB, false]]) {
        const select_ = node.querySelector("select");
        clear(select_);
        select_.appendChild(h("option", { value: "", text: payload.rows.length ? "— choose —" : "no sessions" }));
        for (const row of payload.rows) {
          select_.appendChild(h("option", { value: row.id, text: `#${row.id} · ${row.command} · ${timeOf(row.started_at)}` }));
        }
        void extra;
      }
      if (payload.rows.length >= 2) {
        compareB.querySelector("select").value = String(payload.rows[0].id);
        compareA.querySelector("select").value = String(payload.rows[1].id);
      }
    } catch { /* database may be empty */ }
  }

  async function compare() {
    const a = compareA.querySelector("select").value;
    const b = compareB.querySelector("select").value;
    if (!a || !b) { toast("choose two sessions", "warn"); return; }
    clear(compareHolder);
    compareHolder.appendChild(h("span.sub", { text: "Comparing…" }));
    try {
      const diff = await api.reportCompare(Number(a), Number(b));
      clear(compareHolder);
      const section = (label, rows) => h("div", null, h("h3", { text: label }),
        rows.length ? dataTable({ columns: [
          { key: "0", label: "Module", mono: true }, { key: "1", label: "Code", mono: true }, { key: "2", label: "Status" },
        ], rows: rows.map((row) => ({ 0: row[0], 1: row[1], 2: row[2] })), empty: "none" })
        : h("p.sub", { text: "none" }));
      compareHolder.appendChild(h("div.row", null,
        pill(`${diff.dtcs.appeared.length} appeared`, diff.dtcs.appeared.length ? "negative" : "ok"),
        pill(`${diff.dtcs.cleared.length} cleared`, "ok"),
        pill(`${diff.dtcs.unchanged} unchanged`, "no_response"),
        pill(`totals Δ dtcs ${diff.totals_delta.dtcs ?? 0}`, "no_response")));
      compareHolder.appendChild(section("Codes that appeared (B)", diff.dtcs.appeared));
      compareHolder.appendChild(section("Codes that cleared (A)", diff.dtcs.cleared));
      if (diff.identification_changed.length) {
        compareHolder.appendChild(h("h3", { text: "Identification changed" }));
        compareHolder.appendChild(dataTable({
          columns: [
            { key: "module", label: "Module", mono: true }, { key: "did", label: "DID", mono: true },
            { key: "before", label: "Before" }, { key: "after", label: "After" },
          ], rows: diff.identification_changed, empty: "none",
        }));
      }
    } catch (error) {
      clear(compareHolder);
      compareHolder.appendChild(errorState(error, { onRetry: compare }));
    }
  }

  on(document.getElementById("rep-bus"), "change", () => {});

  refreshMeta();
  loadSessions();

  return { dispose() {}, resize() {} };
}
