// Dashboard (#/) — the app's front door: live status, the "Scan all modules"
// call to action, the pre-trip health check, KPI tiles and recent sessions.

import { h, clear, mount } from "../dom.js";
import { api } from "../api.js";
import { navigate } from "../router.js";
import {
  card, cardBody, kpi, pill, emptyState, errorState, toast, feed, feedRow,
  dataTable, verdict, loadingState, keyValues, tonePill,
} from "../components.js";
import { dateTimeOf } from "../format.js";

const HINTS = {
  adapter: "Plug in the vLinker FS and check `ls /dev/cu.usb*`; then Connect.",
  not_found: "The resource does not exist.",
  internal: "Something went wrong server-side; the message is above.",
};

function verdictBlock(word, reasons) {
  const kind = word === "READY" ? "ok" : word === "DO NOT DRIVE" ? "crit" : "warn";
  return verdict({
    kind, word, meta: `checked ${dateTimeOf(new Date().toISOString())}`, reasons,
  });
}

function computeVerdict(readiness, dtc) {
  const reasons = [];
  const codes = (dtc?.modules || []).flatMap((m) => m.dtcs || []);
  const critical = codes.filter((c) => /^[BC]/.test(c.code || ""));
  if (critical.length) reasons.push(`${critical.length} safety-related code(s): ${critical.map((c) => c.code).join(", ")}`);
  if (dtc?.total_dtcs) reasons.push(`${dtc.total_dtcs} DTC(s) across ${dtc.modules?.length || 0} module(s)`);
  if (readiness && readiness.available === false) reasons.push("readiness monitors unavailable");
  else if (readiness && !readiness.ready) reasons.push("readiness monitors not complete or MIL on");
  const verdict = critical.length ? "DO NOT DRIVE" : (reasons.length ? "ATTENTION" : "READY");
  return { verdict, reasons: reasons.length ? reasons : ["no codes, readiness complete"] };
}

export function render(root, ctx) {
  const timers = [];
  const disposers = [];

  const body = h("div");
  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Dashboard" }),
      h("div.sub", { text: "Adapter status, a one-press all-module scan, and a pre-trip health check." })),
    h("div.row", null,
      h("button.btn", { type: "button", text: "Refresh", onClick: () => load() }),
      h("button.btn.btn--primary", { type: "button", id: "scan-cta", text: "Scan all modules",
        onClick: () => runScan() })));

  const kpis = h("div.grid.grid--kpi", { hidden: true });
  const statusCard = card({ eyebrow: "Adapter", title: "Connection", body: h("div", { id: "adapter-body" }, loadingState("Reading adapter status…")),
    actions: h("a.btn.btn--sm.btn--ghost", { href: "#/settings", text: "Settings" }) });
  const ctaCard = card({
    eyebrow: "Primary action", title: "All-module scan",
    actions: h("button.btn.btn--sm.btn--danger", { type: "button", id: "scan-cancel", text: "Cancel", disabled: true, onClick: cancelActive }),
    body: h("div", null,
      h("p.sub", { text: "Discovers every UDS module on the selected buses (1003 / 22F100 / 22F190), then reads DTCs from each of them. Reads only — nothing is cleared or written." }),
      h("div", { id: "scan-progress" }, feed({ id: "scan-feed", placeholder: "Not run yet." }))),
  });
  const pretripCard = card({
    eyebrow: "Pre-trip", title: "Health check",
    actions: h("button.btn.btn--sm", { type: "button", id: "pretrip-btn", text: "Run check", onClick: () => runPretrip() }),
    body: h("div", { id: "pretrip-body" }, h("p.sub", { text: "Readiness + all-module DTCs + a live snapshot, judged into one verdict." })),
  });
  const sessionsCard = card({ eyebrow: "Local database", title: "Recent sessions", flush: true,
    body: h("div", null, loadingState("Reading recent sessions…", { panel: true })),
    actions: h("a.btn.btn--sm.btn--ghost", { href: "#/db", text: "Open database" }) });

  mount(root, head, h("div", null, kpis), h("div.grid.grid--2", null, statusCard, ctaCard),
    pretripCard, sessionsCard);

  /* ------------------------------------------------------------- helpers */

  function setKpis(status, dtc, pids) {
    clear(kpis);
    kpis.hidden = false;
    const adapter = status?.health?.adapter || {};
    const voltage = adapter.voltage;
    kpis.appendChild(kpi({ label: "Adapter voltage", value: voltage ? voltage.value : null,
      unit: voltage ? voltage.unit : "V", foot: tonePill(adapter.attached ? "attached" : "not connected",
        adapter.attached ? "ok" : "idle") }));
    kpis.appendChild(kpi({ label: "Stored DTCs", value: dtc ? dtc.total_dtcs : null,
      foot: dtc ? (dtc.total_dtcs ? tonePill("faults present", "crit") : h("span.sub", { text: "none" })) : h("span.sub", { text: "not read" }) }));
    kpis.appendChild(kpi({ label: "Modules seen", value: dtc ? (dtc.modules?.length ?? 0) : null,
      foot: h("span.sub", { text: dtc ? `${(dtc.modules || []).filter((m) => m.status === "ok").length} answered ok` : "not scanned" }) }));
    kpis.appendChild(kpi({ label: "Supported PIDs", value: pids ?? null,
      foot: h("a", { href: "#/live", text: "open live data" }) }));
    kpis.appendChild(kpi({ label: "Powertrain", value: adapter.powertrain || null,
      foot: h("span.sub", { text: adapter.powertrain ? "detected from responding modules" : "no hybrid-only module answered" }) }));
  }

  function setAdapterBody(status, error) {
    const holder = document.getElementById("adapter-body");
    clear(holder);
    if (error) { holder.appendChild(errorState(error, { onRetry: () => load() })); return; }
    const adapter = status?.health?.adapter || {};
    if (!adapter.attached) {
      holder.appendChild(emptyState({
        icon: "plug", title: "vLinker FS not detected",
        hint: "Plug the adapter into the OBD-II port, turn the ignition on, then press Connect.",
        action: h("div.row", null,
          h("button.btn.btn--primary", { type: "button", text: "Connect", onClick: async () => {
            try { await api.connect({}); toast("Adapter opened"); } catch (e) { toast(e.message, "err", 0); }
            load();
          } }),
          h("a.btn", { href: "#/settings", text: "Ports & settings" })),
      }));
      return;
    }
    holder.appendChild(keyValues([
      ["Port", adapter.port || "—"],
      ["Status", adapter.status || "—"],
      ["Protocol", adapter.protocol || "—"],
      ["Adapter", adapter.adapter || "—"],
      ["Bus", status?.status?.bus || "can-c"],
    ]));
  }

  function setSessions(payload, error) {
    const holder = cardBody(sessionsCard);
    clear(holder);
    if (error) { holder.appendChild(errorState(error, { onRetry: () => load() })); return; }
    if (!payload.available) {
      holder.appendChild(emptyState({ icon: "database", title: "No local database found",
        hint: payload.db_path ? `Looked at ${payload.db_path}` : "Persistence may be disabled (--no-log)." }));
      return;
    }
    holder.appendChild(dataTable({
      columns: [
        { key: "id", label: "ID", mono: true, num: true },
        { key: "started_at", label: "Started", render: (row) => dateTimeOf(row.started_at) },
        { key: "command", label: "Command", mono: true },
        { key: "status", label: "Status", render: (row) => pill(row.status || "—", row.status) },
        { key: "vin", label: "VIN", mono: true, render: (row) => row.vin || "—" },
      ],
      rows: payload.rows, empty: "No sessions recorded yet.",
      emptyTitle: "No sessions yet",
      onRowClick: (row) => navigate("/db", { table: "sessions", highlight: row.id }),
    }));
  }

  /* --------------------------------------------------------- scan action */

  let scanning = false;

  async function pollJobs(render) {
    for (;;) {
      await new Promise((resolve) => timers.push(setTimeout(resolve, 500)));
      try {
        const { jobs } = await api.jobs();
        const running = jobs.filter((job) => job.state === "running" || job.state === "queued");
        if (!running.length) return;
        render(running);
      } catch { /* keep polling */ }
    }
  }

  async function runScan({ silent = false } = {}) {
    if (scanning) return null;
    scanning = true;
    const feed_ = document.getElementById("scan-feed");
    const cancelButton = document.getElementById("scan-cancel");
    clear(feed_);
    if (cancelButton) cancelButton.disabled = false;
    feedRow(feed_, "scanning modules on can-c…");
    pollJobs((jobs) => {
      for (const job of jobs) {
        if (job.progress && Object.keys(job.progress).length) {
          feedRow(feed_, `${job.name}: ${JSON.stringify(job.progress)}`);
        }
      }
    });
    try {
      const modules = await api.udsModules({ bus: "can-c", start: "700", end: "7FF", timeout_ms: 100 });
      feedRow(feed_, `discovery: ${modules.modules.length} module(s) responded`, "ok");
      ctx.refreshStatus();
      const dtc = await api.udsDtcAll({ bus: "can-c", start: "700", end: "7FF", timeout_ms: 100 });
      feedRow(feed_, `DTCs: ${dtc.total_dtcs} across ${dtc.modules.length} module(s)`,
        dtc.total_dtcs ? "negative" : "ok");
      lastDtc = dtc;
      lastModules = modules;
      setKpis(ctx.status, dtc, lastPids);
      if (!silent) toast(`Scan complete: ${dtc.total_dtcs} DTC(s)`, dtc.total_dtcs ? "warn" : "ok");
      return { modules, dtc };
    } catch (error) {
      feed_.appendChild(errorState(error));
      if (!silent) toast(error.message, "err", 0);
      return null;
    } finally {
      scanning = false;
      if (cancelButton) cancelButton.disabled = true;
    }
  }

  async function cancelActive() {
    const id = ctx.status?.status?.job?.holder_id;
    if (!id) { toast("nothing running", "warn"); return; }
    try { await api.cancelJob(id); toast("Cancel requested", "warn"); }
    catch (error) { toast(error.message, "err"); }
  }

  async function runPretrip() {
    const holder = document.getElementById("pretrip-body");
    clear(holder);
    holder.appendChild(loadingState("Running readiness, all-module DTCs and a live snapshot…"));
    try {
      const [readiness, dtc, scan] = await Promise.all([
        api.obdReadiness().catch((error) => ({ available: false, reason: error.message })),
        api.udsDtcAll({ bus: "can-c", start: "700", end: "7FF", timeout_ms: 100 }),
        api.obdScan().catch(() => null),
      ]);
      const { verdict, reasons } = computeVerdict(readiness, dtc);
      clear(holder);
      holder.appendChild(verdictBlock(verdict, reasons));
      if (readiness?.monitors?.length) {
        holder.appendChild(h("div.row", null, readiness.monitors.map((m) =>
          tonePill(`${m.name} · ${m.complete ? "complete" : "incomplete"}`, m.complete ? "ok" : "warn"))));
      }
      if (scan?.stored_dtcs?.length) {
        holder.appendChild(h("div.sub", { text: `PCM codes: ${scan.stored_dtcs.map((d) => d.code).join(", ")}` }));
      }
      lastDtc = dtc;
      setKpis(ctx.status, dtc, lastPids);
    } catch (error) {
      clear(holder);
      holder.appendChild(errorState(error, { onRetry: runPretrip }));
    }
  }

  /* ---------------------------------------------------------------- load */

  let lastDtc = null;
  let lastModules = null;
  let lastPids = null;

  async function load() {
    setAdapterBody(null, null);
    try {
      const [status, health] = await Promise.all([api.status(), api.health()]);
      setAdapterBody({ status, health }, null);
    } catch (error) {
      setAdapterBody(null, error);
    }
    try {
      const dtc = await api.obdDtc();
      lastDtc = { total_dtcs: dtc.total, modules: [] };
      setKpis(ctx.status, lastDtc, lastPids);
    } catch {
      setKpis(ctx.status, lastDtc, lastPids);
    }
    try {
      const pids = await api.obdPids();
      lastPids = pids.pids.length;
      setKpis(ctx.status, lastDtc, lastPids);
    } catch { /* pid count is decorative */ }
    try {
      const sessions = await api.dbTable("sessions", { limit: 5, order: "id", dir: "desc" });
      setSessions(sessions, null);
    } catch (error) {
      setSessions(null, error);
    }
  }

  load();

  const interval = setInterval(() => { if (!scanning) refreshKpisOnly(); }, 10000);
  async function refreshKpisOnly() {
    try {
      const status = await api.status();
      const health = await api.health();
      ctx.status = { status, health };
      setKpis({ status, health }, lastDtc, lastPids);
    } catch { /* ignore */ }
  }

  return {
    dispose() {
      clearInterval(interval);
      for (const timer of timers) clearTimeout(timer);
      for (const dispose of disposers) dispose();
    },
    resize() { /* charts live on other screens */ },
  };
}
