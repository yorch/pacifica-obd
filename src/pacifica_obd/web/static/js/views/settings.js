// Settings (#/settings) — adapter, bus defaults, the bundled dataset, data paths,
// and the read-only safety statement. All read-only except Connect/Disconnect.

import { h, clear, mount, on } from "../dom.js";
import { api } from "../api.js";
import { card, cardBody, pill, emptyState, errorState, toast, banner, select, dataTable } from "../components.js";

export function render(root, ctx) {
  const adapterHolder = h("div");
  const portsHolder = h("div");
  const datasetHolder = h("div");
  const dataHolder = h("div");
  const busHolder = h("div");

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Settings" }),
      h("div.sub", { text: "Adapter and bus configuration, the bundled dataset summary, and where local data lives." })));

  mount(root, head,
    card({ eyebrow: "Safety", title: "Read-only build", body: h("div", null,
      banner("This build has no write path: no clear-DTC, no write-DID, no routine, no 0x27 unlock, and no AI provider. Every endpoint is a read.", "info"),
      h("p.sub", { text: "Status colour is never the only signal: each pill carries the machine status text the CLI emits." })) }),
    card({ eyebrow: "Adapter", title: "Connection", actions: h("div.row", null,
      h("button.btn.btn--sm", { type: "button", text: "Read info", onClick: readInfo }),
      h("button.btn.btn--sm", { type: "button", text: "Connect", onClick: () => connect(true) }),
      h("button.btn.btn--sm.btn--ghost", { type: "button", text: "Disconnect", onClick: () => connect(false) })),
      body: adapterHolder }),
    card({ eyebrow: "Bus", title: "Default bus", body: busHolder }),
    card({ eyebrow: "Host", title: "Serial ports (passive enumeration)", body: portsHolder }),
    card({ eyebrow: "Dataset", title: "Bundled pacifica.json", body: datasetHolder }),
    card({ eyebrow: "Data", title: "Local files", body: dataHolder }));

  /* ----------------------------------------------------------------- adapter */

  async function readInfo() {
    clear(adapterHolder);
    adapterHolder.appendChild(h("span.sub", { text: "Reading adapter info…" }));
    try {
      const info = await api.info();
      clear(adapterHolder);
      adapterHolder.appendChild(h("dl.kv", null, [
        ["Port", info.port], ["Status", info.status], ["Protocol", info.protocol],
        ["Adapter", info.adapter], ["Voltage", info.voltage ? `${info.voltage.value ?? info.voltage} ${info.voltage.unit || ""}` : "—"],
        ["VIN", info.vin || "—"],
      ].flatMap(([key, value]) => [h("dt", { text: key }), h("dd", { text: String(value ?? "—") })])));
    } catch (error) {
      clear(adapterHolder);
      adapterHolder.appendChild(errorState(error, { onRetry: readInfo }));
    }
  }

  async function connect(on_) {
    try {
      if (on_) await api.connect({}); else await api.disconnect();
      toast(on_ ? "Adapter opened" : "Adapter closed");
      ctx.refreshStatus();
      readInfo();
    } catch (error) { toast(error.message, "err", 0); }
  }

  async function loadPorts() {
    try {
      const payload = await api.ports();
      clear(portsHolder);
      if (!payload.ports.length) {
        portsHolder.appendChild(emptyState({ icon: "🔌", title: "No candidate serial ports",
          hint: "Plug in the vLinker FS and check `ls /dev/cu.usb*`." }));
        return;
      }
      portsHolder.appendChild(h("div.row", null, payload.ports.map((port) => h("span.pill.pill--idle.mono", { text: port }))));
    } catch (error) { clear(portsHolder); portsHolder.appendChild(errorState(error, { onRetry: loadPorts })); }
  }

  /* ------------------------------------------------------------------- bus */

  const busSelect = select({ id: "set-bus", label: "Default bus", value: "can-c",
    options: [{ value: "can-c", label: "can-c (HS 500k, pins 6/14)" }, { value: "can-ihs", label: "can-ihs (MS 125k, pins 3/11)" }] });
  busHolder.appendChild(h("div.row", null, busSelect,
    h("button.btn.btn--sm", { type: "button", text: "Apply", onClick: async () => {
      try { await api.bus(busSelect.querySelector("select").value); toast("Bus updated"); ctx.refreshStatus(); }
      catch (error) { toast(error.message, "err"); }
    } })));
  busHolder.appendChild(h("p.sub", { text: "Reads use an ATCRA receive filter pointing at the responder; ISO-TP flow control is always left automatic." }));

  /* --------------------------------------------------------------- dataset */

  async function loadDataset() {
    try {
      const info = await api.datasetInfo();
      clear(datasetHolder);
      datasetHolder.appendChild(h("dl.kv", null, [
        ["Path", info.path], ["Modules", info.modules], ["Buses", (info.buses || []).join(", ")],
        ["Powertrains", (info.powertrains || []).join(", ")], ["ECUs with DIDs", info.ecus_with_dids],
        ["DIDs", info.dids], ["Adaptations", info.adaptations], ["DTC texts", info.dtc_texts],
        ["seed_key", info.seed_key ? "present" : "absent"],
      ].flatMap(([key, value]) => [h("dt", { text: key }), h("dd", { text: String(value ?? "—") })])));
    } catch (error) { clear(datasetHolder); datasetHolder.appendChild(errorState(error, { onRetry: loadDataset })); }
  }

  /* ------------------------------------------------------------------ data */

  async function loadData() {
    try {
      const [schema, info] = await Promise.all([api.dbSchema(), api.health().catch(() => null)]);
      clear(dataHolder);
      dataHolder.appendChild(h("dl.kv", null, [
        ["Database", schema.db_path || "—"],
        ["Available", schema.available ? "yes" : "no"],
        ["Tables", (schema.tables || []).map((table) => `${table.name} (${table.rows})`).join(", ") || "—"],
        ["Server", `${location.origin} · v${info?.version || "?"}`],
        ["Read-only", String(info?.read_only ?? true)],
        ["Auth", info?.auth_required ? "bearer token required" : "none (loopback only)"],
      ].flatMap(([key, value]) => [h("dt", { text: key }), h("dd", { text: String(value ?? "—") })])));
      dataHolder.appendChild(h("p.sub", { text: "The database browser opens SQLite read-only (mode=ro) and issues SELECTs only." }));
    } catch (error) { clear(dataHolder); dataHolder.appendChild(errorState(error, { onRetry: loadData })); }
  }

  readInfo();
  loadPorts();
  loadDataset();
  loadData();

  return { dispose() {}, resize() {} };
}
