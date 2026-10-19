// Settings (#/settings) — adapter, bus defaults, the bundled dataset, local data
// paths, the safety posture, and the AI layer (provider, key, privacy toggles).

import { h, clear, mount } from "../dom.js";
import { api } from "../api.js";
import {
  card, pill, emptyState, errorState, toast, banner, select, checkbox,
  field, keyValues, loadingState, tonePill,
} from "../components.js";

export function render(root, ctx) {
  const adapterHolder = h("div", null, loadingState("Reading adapter info…"));
  const portsHolder = h("div", null, loadingState("Enumerating serial ports…"));
  const datasetHolder = h("div", null, loadingState("Reading the bundled dataset…"));
  const dataHolder = h("div", null, loadingState("Looking at the local database…"));
  const busHolder = h("div");
  const safetyHolder = h("div", null, loadingState("Reading the safety posture…"));
  const aiHolder = h("div", null, loadingState("Reading the AI configuration…"));

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Settings" }),
      h("div.sub", { text: "Adapter and bus configuration, the bundled dataset summary, where local data lives, the safety posture and the AI layer." })));

  mount(root, head,
    card({ eyebrow: "Safety", title: "Safety posture", body: safetyHolder }),
    card({ eyebrow: "AI", title: "Assistant provider", body: aiHolder }),
    card({ eyebrow: "Adapter", title: "Connection", actions: h("div.row", null,
      h("button.btn.btn--sm", { type: "button", text: "Read info", onClick: readInfo }),
      h("button.btn.btn--sm", { type: "button", text: "Connect", onClick: () => connect(true) }),
      h("button.btn.btn--sm.btn--ghost", { type: "button", text: "Disconnect", onClick: () => connect(false) })),
      body: adapterHolder }),
    card({ eyebrow: "Bus", title: "Default bus", body: busHolder }),
    card({ eyebrow: "Host", title: "Serial ports (passive enumeration)", body: portsHolder }),
    card({ eyebrow: "Dataset", title: "Bundled pacifica.json", body: datasetHolder }),
    card({ eyebrow: "Data", title: "Local files", body: dataHolder }));

  /* -------------------------------------------------------------- safety */

  async function loadSafety() {
    try {
      const info = await api.settings();
      const toggle = checkbox({ id: "strict-seed", label: "Strict 0x27 seed re-read (opt-in)",
        checked: info.strict_seed_reread });
      clear(safetyHolder);
      safetyHolder.appendChild(h("div.stack", null,
        h("div.row", null, pill(info.read_only ? "read-only" : "writes enabled", info.read_only ? "ok" : "warn")),
        h("p.sub", { text: `Enabled by: ${info.safety?.enabled_by || "—"}` }),
        h("p.sub", { text: info.safety?.sgw || "" }),
        toggle,
        h("p.sub", { text: "Strict mode re-reads the seed and compares it before sending. It spends a second seed request, which can advance some ECUs' bad-attempt counter — enable it only for a module whose seed is known to be stable." }),
        h("button.btn.btn--sm", { type: "button", text: "Apply", onClick: async () => {
          const checked = safetyHolder.querySelector("#strict-seed").checked;
          try { await api.putSettings({ strict_seed_reread: checked }); toast("Safety setting updated"); }
          catch (error) { toast(error.message, "err"); }
        } })));
    } catch (error) { clear(safetyHolder); safetyHolder.appendChild(errorState(error, { onRetry: loadSafety })); }
  }

  /* ------------------------------------------------------------------ AI */

  async function loadAI() {
    try {
      const [config, providers] = await Promise.all([api.aiConfig(), api.aiProviders()]);
      clear(aiHolder);
      const providerSelect = select({ id: "ai-provider", label: "Provider",
        value: config.provider || "",
        options: [{ value: "", label: "— none configured —" }].concat(
          (providers.providers || []).map((row) => ({
            value: row.id, label: `${row.label}${row.requires_key ? " (key required)" : ""}`,
          })))});
      const model = textField("ai-model", "Model", config.model || "", "model id");
      const baseUrl = textField("ai-base", "Base URL (compatible APIs)", config.base_url || "",
        "https://… or a local endpoint");
      const key = textField("ai-key", "API key (write-only; never shown again)", "",
        "paste a key", { type: "password", autocomplete: "new-password",
          hint: "Stored 0600 under the tool's home directory — never logged, never returned." });
      const keyState = h("div.row");
      const builds = [];
      for (const [id, label, value] of [
        ["send-remote", "Send bus data to a remote model", config.send_bus_data_to_remote],
        ["redact-vin", "Redact VIN before egress", config.redact_vin],
        ["live-bus", "Allow the assistant to read the live bus", config.allow_live_bus],
        ["persist", "Save conversations locally (opt-in)", config.persist_conversations],
        ["local-only", "Local models only (block remote providers)", config.force_local_only],
      ]) builds.push(checkbox({ id, label, checked: value }));

      const save = h("button.btn.btn--primary", { type: "button", text: "Save AI settings", onClick: async () => {
        try {
          await api.aiSetConfig({
            provider: providerSelect.querySelector("select").value || null,
            model: model.input.value.trim(),
            base_url: baseUrl.input.value.trim() || null,
            send_bus_data_to_remote: flag("send-remote"),
            redact_vin: flag("redact-vin"),
            allow_live_bus: flag("live-bus"),
            persist_conversations: flag("persist"),
            force_local_only: flag("local-only"),
          });
          toast("AI settings saved");
          loadAI();
        } catch (error) { toast(`${error.code || "error"}: ${error.message}`, "err", 0); }
      } });
      const saveKey = h("button.btn.btn--sm", { type: "button", text: "Save key", onClick: async () => {
        const provider = providerSelect.querySelector("select").value;
        const value = key.input.value;
        if (!provider || !value) { toast("Choose a provider and paste a key", "warn"); return; }
        try {
          const result = await api.aiSetKey({ provider, key: value });
          key.input.value = "";
          toast(`Key saved (${result.key.hint})`);
          loadAI();
        } catch (error) { toast(`${error.code || "error"}: ${error.message}`, "err", 0); }
      } });
      const removeKey = h("button.btn.btn--sm.btn--ghost", { type: "button", text: "Remove key", onClick: async () => {
        const provider = providerSelect.querySelector("select").value;
        if (!provider) return;
        try { await api.aiDeleteKey(provider); toast("Key removed"); loadAI(); }
        catch (error) { toast(error.message, "err"); }
      } });

      for (const [name, info] of Object.entries(config.keys || {})) {
        keyState.appendChild(pill(`${name}: ${info.configured ? `configured ${info.hint}` : "no key"}${info.source ? ` (${info.source})` : ""}`, "idle"));
      }

      aiHolder.appendChild(h("div.stack", null,
        banner(config.provider
          ? (config.is_local ? "Local model: vehicle data does not leave this machine."
            : "Remote model: enable 'send bus data to a remote model' before a task can attach vehicle data.")
          : "No provider configured. Paste a key, or point the base URL at a local model.", config.provider ? "info" : "warn"),
        providerSelect, model.node, baseUrl.node, key.node,
        h("div.row", null, saveKey, removeKey), keyState,
        h("div.grid.grid--form", null, builds),
        h("div.row", null, save,
          h("button.btn.btn--sm.btn--danger", { type: "button", text: "Delete saved conversations",
            onClick: async () => {
              try { await api.aiDeleteConversations(); toast("Saved conversations deleted"); }
              catch (error) { toast(error.message, "err"); }
            } })),
        h("p.sub", { text: "Reads are cache-first; live reads need the setting above, an idle bus and a per-turn cap. The assistant's tools are read-only." })));

      function flag(id) { return Boolean(aiHolder.querySelector(`#${id}`).checked); }
    } catch (error) { clear(aiHolder); aiHolder.appendChild(errorState(error, { onRetry: loadAI })); }
  }

  function textField(id, label, value, placeholder, extra = {}) {
    const node = field({ id, label, value, placeholder, mono: true, ...extra });
    return { node, input: node.querySelector("input") };
  }

  /* ------------------------------------------------------------- adapter */

  async function readInfo() {
    clear(adapterHolder);
    adapterHolder.appendChild(loadingState("Reading adapter info…"));
    try {
      const info = await api.info();
      clear(adapterHolder);
      adapterHolder.appendChild(keyValues([
        ["Port", info.port], ["Status", info.status], ["Protocol", info.protocol],
        ["Adapter", info.adapter],
        ["Voltage", info.voltage ? `${info.voltage.value ?? info.voltage} ${info.voltage.unit || ""}` : "—"],
        ["VIN", info.vin || "—"],
      ]));
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
        portsHolder.appendChild(emptyState({ icon: "plug", title: "No candidate serial ports",
          hint: "Plug in the vLinker FS and check `ls /dev/cu.usb*`." }));
        return;
      }
      portsHolder.appendChild(h("div.row", null, payload.ports.map((port) => tonePill(port, "idle"))));
    } catch (error) { clear(portsHolder); portsHolder.appendChild(errorState(error, { onRetry: loadPorts })); }
  }

  /* ------------------------------------------------------------------ bus */

  const busSelect = select({ id: "set-bus", label: "Default bus", value: "can-c", mono: true,
    options: [{ value: "can-c", label: "can-c (HS 500k, pins 6/14)" }, { value: "can-ihs", label: "can-ihs (MS 125k, pins 3/11)" }] });
  busHolder.appendChild(h("div.stack", null,
    h("div.row", null, busSelect,
      h("button.btn.btn--sm", { type: "button", text: "Apply", onClick: async () => {
        try { await api.bus(busSelect.querySelector("select").value); toast("Bus updated"); ctx.refreshStatus(); }
        catch (error) { toast(error.message, "err"); }
      } })),
    h("p.sub", { text: "Reads use an ATCRA receive filter pointing at the responder; ISO-TP flow control is always left automatic." })));

  /* -------------------------------------------------------------- dataset */

  async function loadDataset() {
    try {
      const info = await api.datasetInfo();
      clear(datasetHolder);
      datasetHolder.appendChild(keyValues([
        ["Path", info.path], ["Modules", info.modules], ["Buses", (info.buses || []).join(", ")],
        ["Powertrains", (info.powertrains || []).join(", ")], ["ECUs with DIDs", info.ecus_with_dids],
        ["DIDs", info.dids], ["Adaptations", info.adaptations], ["DTC texts", info.dtc_texts],
        ["seed_key", info.seed_key ? "present" : "absent"],
      ]));
    } catch (error) { clear(datasetHolder); datasetHolder.appendChild(errorState(error, { onRetry: loadDataset })); }
  }

  /* ----------------------------------------------------------------- data */

  async function loadData() {
    try {
      const [schema, info] = await Promise.all([api.dbSchema(), api.health().catch(() => null)]);
      clear(dataHolder);
      dataHolder.appendChild(keyValues([
        ["Database", schema.db_path || "—"],
        ["Available", schema.available ? "yes" : "no"],
        ["Tables", (schema.tables || []).map((table) => `${table.name} (${table.rows})`).join(", ") || "—"],
        ["Server", `${location.origin} · v${info?.version || "?"}`],
        ["Write surface", info?.write_enabled ? "enabled" : "read-only"],
        ["Auth", info?.auth_required ? "bearer token required" : "none (loopback only)"],
      ]));
      dataHolder.appendChild(h("p.sub", { text: "The database browser opens SQLite read-only (mode=ro) and issues SELECTs only." }));
    } catch (error) { clear(dataHolder); dataHolder.appendChild(errorState(error, { onRetry: loadData })); }
  }

  readInfo();
  loadPorts();
  loadDataset();
  loadData();
  loadSafety();
  loadAI();

  return { dispose() {}, resize() {} };
}
