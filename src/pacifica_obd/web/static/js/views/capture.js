// Capture (#/capture) — the full snapshot (modules + DTCs + identification).
// A partial capture is shown as partial, never as a failure.

import { h, clear, mount } from "../dom.js";
import { api } from "../api.js";
import { navigate } from "../router.js";
import { card, pill, emptyState, errorState, toast, field, select, progressBar } from "../components.js";
import { dateTimeOf } from "../format.js";

export function render(root, ctx) {
  const state = { result: null, busy: false };

  const busField = select({ id: "cap-bus", label: "Bus", value: "both",
    options: [{ value: "both", label: "both" }, { value: "can-c", label: "can-c" }, { value: "can-ihs", label: "can-ihs" }] });
  const startField = field({ id: "cap-start", label: "Start ID", value: "700", mono: true });
  const endField = field({ id: "cap-end", label: "End ID", value: "7FF", mono: true });
  const timeoutField = field({ id: "cap-timeout", label: "Timeout (ms)", value: "100", mono: true, type: "number" });
  const vinField = field({ id: "cap-vin", label: "VIN tag (optional)", mono: true, placeholder: "attach stored rows to this VIN" });
  const progress = progressBar();
  const feed = h("div.feed", null, h("div.sub", { text: "No capture yet." }));
  const resultHolder = h("div");

  const runButton = h("button.btn.btn--primary", { type: "button", text: "Start capture", onClick: run });
  const cancelButton = h("button.btn.btn--danger.btn--sm", { type: "button", text: "Cancel", disabled: true, onClick: cancelActive });

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Capture" }),
      h("div.sub", { text: "Discovers every module, reads its DTCs and identification block, and stores everything in the local database. Reads only." })));

  mount(root, head,
    card({ eyebrow: "Snapshot", title: "Full capture", body: h("div", null,
      h("div.row", null, busField, startField, endField, timeoutField, vinField),
      h("div.row.row--spaced", runButton, cancelButton),
      h("div.row--spaced", progress),
      h("div.row--spaced", feed)) }),
    card({ eyebrow: "Result", title: "Summary", body: resultHolder }));

  function log(text, status) {
    feed.appendChild(h("div.feed__row", null,
      h("span.feed__ts", { text: new Date().toLocaleTimeString([], { hour12: false }) }),
      h("span", { text }), status ? pill(status, status) : null));
    feed.scrollTop = feed.scrollHeight;
  }

  async function cancelActive() {
    const id = ctx.status?.status?.job?.holder_id;
    if (!id) { toast("nothing running", "warn"); return; }
    try { await api.cancelJob(id); toast("Cancel requested", "warn"); } catch (error) { toast(error.message, "err"); }
  }

  async function run() {
    if (state.busy) return;
    state.busy = true;
    runButton.disabled = true;
    cancelButton.disabled = false;
    const tick = setInterval(async () => {
      try {
        const { jobs } = await api.jobs();
        for (const job of jobs.filter((j) => j.state === "running")) {
          if (job.progress?.modules) progress.update(Math.min(0.95, job.progress.modules / 40));
          if (job.progress?.request_id) log(`module ${job.progress.request_id}`);
        }
      } catch { /* ignore */ }
    }, 600);
    try {
      const result = await api.udsCapture({
        bus: busField.querySelector("select").value,
        start: document.getElementById("cap-start").value,
        end: document.getElementById("cap-end").value,
        timeout_ms: Number(document.getElementById("cap-timeout").value) || 100,
        vin: document.getElementById("cap-vin").value || null,
      });
      state.result = result;
      progress.update(1);
      const dtcs = (result.dtcs || []).reduce((sum, item) => sum + (item.dtcs || []).length, 0);
      log(`${result.modules.length} module(s), ${dtcs} DTC(s), ${result.readings.length} reading(s)`,
        result.aborted ? "partial" : "ok");
      renderResult();
      toast(result.aborted ? `Partial capture: ${result.aborted}` : "Capture complete", result.aborted ? "warn" : "ok");
    } catch (error) {
      log(`${error.code || "error"}: ${error.message}`);
      feed.appendChild(errorState(error, { onRetry: run }));
      toast(error.message, "err", 0);
    } finally {
      clearInterval(tick);
      state.busy = false;
      runButton.disabled = false;
      cancelButton.disabled = true;
      ctx.refreshStatus();
    }
  }

  function renderResult() {
    clear(resultHolder);
    const result = state.result;
    if (!result) {
      resultHolder.appendChild(emptyState({ icon: "⤓", title: "Nothing captured yet",
        hint: "Press Start capture to read every module on the selected buses." }));
      return;
    }
    const dtcs = (result.dtcs || []).reduce((sum, item) => sum + (item.dtcs || []).length, 0);
    if (result.aborted) {
      resultHolder.appendChild(h("div.banner.banner--warn", { class: "mb-5" },
        h("span", { text: `Partial capture — the adapter stopped mid-run: ${result.aborted}` })));
    }
    resultHolder.appendChild(h("div.row", null,
      h("span", { class: "pill pill--info", text: `${result.modules.length} modules` }),
      h("span", { class: `pill ${dtcs ? "pill--crit" : "pill--ok"}`, text: `${dtcs} DTCs` }),
      h("span", { class: "pill pill--idle", text: `${result.readings.length} readings` }),
      result.vin ? h("span.mono", { text: result.vin }) : null,
      result.powertrain ? pill(result.powertrain, "ok") : null,
      h("span.sub", { text: dateTimeOf(new Date().toISOString()) })));
    resultHolder.appendChild(h("div.row.row--spaced",
      h("a.btn.btn--sm", { href: "#/db?table=dtcs", text: "Open DTCs in the database" }),
      h("a.btn.btn--sm", { href: "#/reports", text: "Build a report" }),
      h("a.btn.btn--sm.btn--ghost", { href: "#/modules", text: "See the module tree" })));
  }

  renderResult();
  return { dispose() {}, resize() {} };
}
