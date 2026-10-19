// Settings (#/settings) — adapter, bus defaults, the bundled dataset, local data
// paths, the safety posture, and the AI layer (provider, key, privacy toggles).

import { h, clear, mount, on } from "../dom.js";
import { api } from "../api.js";
import {
  card, pill, emptyState, errorState, toast, banner, select, checkbox,
  field, keyValues, loadingState, tonePill, dataTable,
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

  // Loopback hosts the backend treats as local (config.LOCAL_BASE_HOSTS).
  const AI_LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"]);
  // Mirrors config._BOUNDS so the form rejects a cap before the server does.
  const AI_BOUNDS = {
    max_output_tokens_per_request: [1, 131072],
    max_tokens_per_conversation: [256, 10000000],
    max_requests_per_minute: [1, 600],
    max_live_reads_per_turn: [0, 50],
  };

  async function loadAI(pendingProvider = null) {
    try {
      const [config, listing] = await Promise.all([api.aiConfig(), api.aiProviders()]);
      const providers = listing.providers || [];
      const byId = new Map(providers.map((row) => [row.id, row]));
      clear(aiHolder);
      const view = aiCard(config, providers, byId, pendingProvider);
      aiHolder.appendChild(view.root);
      await view.init();
      // Deterministic deep link for screenshot automation, matching the other
      // views (`?auto=`, `?start=`): `?ai=test` or `?ai=refresh`.
      const auto = ctx.params?.get("ai");
      if (auto === "test") view.runTest();
      else if (auto === "refresh") view.refresh();
    } catch (error) {
      clear(aiHolder);
      aiHolder.appendChild(errorState(error, { onRetry: loadAI }));
    }
  }

  // A provider's human name without the parenthetical ("Anthropic (Claude)" ->
  // "Anthropic"), for a sentence and a one-click button.
  function shortLabel(config, provider) {
    const label = config?.provider_labels?.[provider] || provider || "provider";
    return String(label).replace(/\s*\(.*\)\s*$/, "");
  }

  function article(name) {
    return /^(LM|LLM|[AEIOU])/i.test(String(name)) ? "An" : "A";
  }

  function savedProviders(config) {
    if (Array.isArray(config.saved_key_providers)) return config.saved_key_providers;
    return Object.entries(config.keys || {})
      .filter(([, info]) => info?.configured)
      .map(([id]) => id);
  }

  function aiCard(config, providers, byId, pendingProvider = null) {
    let preset = byId.get(config.provider || "") || null;
    let testInFlight = false;
    // Set when "Save key" saved a key for a provider other than the configured
    // one; cleared as soon as the person edits the provider choice.
    let pending = pendingProvider;
    // A provider/model switch while a request is in flight must not let the older
    // response repaint the newer form.
    let listSeq = 0;
    // The endpoint the list currently on screen belongs to (null if none).
    let shownListBase = null;

    /* --- nodes ---------------------------------------------------------- */
    const statusBanner = h("div", { id: "ai-banner" });
    const presetNotes = h("p.sub", { id: "ai-preset-notes", text: preset?.notes || "" });

    const providerField = select({
      id: "ai-provider", label: "Provider", value: config.provider || "",
      options: [{ value: "", label: "— none configured —" }].concat(
        providers.map((row) => ({ value: row.id, label: providerOptionLabel(row) }))),
    });
    const providerControl = providerField.querySelector("select");

    const modelList = h("datalist", { id: "ai-model-list" });
    const modelField = field({
      id: "ai-model", label: "Model", mono: true, list: "ai-model-list",
      value: config.model || "", placeholder: preset?.default_model || "model id",
      hint: "Type any model id, even one the list does not show — a pinned snapshot or a gateway-only name.",
    });
    const modelInput = modelField.querySelector("input");
    modelField.appendChild(modelList);

    const baseField = field({
      id: "ai-base", label: "Base URL", mono: true,
      value: config.base_url || "",
      placeholder: "https://… or http://127.0.0.1:PORT/v1",
      hint: "Must be https:// unless it is a loopback host; put credentials in the key field, never the URL.",
    });
    const baseInput = baseField.querySelector("input");

    const keyField = field({
      id: "ai-key", label: "API key (write-only; never shown again)", mono: true,
      type: "password", autocomplete: "new-password", value: "",
      placeholder: preset?.key_hint || "paste a key",
      hint: "Stored 0600 under the tool's home directory — never logged, never returned.",
    });
    const keyInput = keyField.querySelector("input");
    const keyRequirement = h("p.sub", { id: "ai-key-hint" });
    const keyState = h("div.chips.chips--picker", { id: "ai-key-state" });

    const toggleDefs = [
      ["send-remote", "Send bus data to a remote model", config.send_bus_data_to_remote],
      ["redact-vin", "Redact VIN before egress", config.redact_vin],
      ["live-bus", "Allow the assistant to read the live bus", config.allow_live_bus],
      ["persist", "Save conversations locally (opt-in)", config.persist_conversations],
      ["local-only", "Local models only (block remote providers)", config.force_local_only],
    ];
    const toggles = h("div.grid.grid--form", { id: "ai-toggles" },
      toggleDefs.map(([id, label, checked]) => checkbox({ id, label, checked })));

    const caps = {
      temperature: numberField("ai-temp", "Temperature", config.temperature, 0, 2, 0.1),
      max_output_tokens_per_request: numberField("ai-max-out", "Max output tokens / request",
        config.max_output_tokens_per_request, 1, 131072),
      max_tokens_per_conversation: numberField("ai-max-conv", "Max tokens / conversation",
        config.max_tokens_per_conversation, 256, 10000000),
      max_requests_per_minute: numberField("ai-max-rpm", "Max requests / minute",
        config.max_requests_per_minute, 1, 600),
      max_live_reads_per_turn: numberField("ai-max-live", "Max live reads / turn",
        config.max_live_reads_per_turn, 0, 50),
    };
    const advanced = h("details", { id: "ai-advanced" },
      h("summary", { text: "Advanced: cost and rate caps" }),
      h("div.grid.grid--form", null, Object.values(caps).map((cap) => cap.node)));

    const formError = h("p.sub", { id: "ai-form-error" });

    /* --- model list ----------------------------------------------------- */
    const refreshBtn = h("button.btn.btn--sm", { type: "button", id: "ai-refresh",
      text: "Refresh", onClick: refresh });
    const modelsHead = h("div.row");
    const modelsChips = h("div.chips.chips--picker", { id: "ai-models" });
    const modelsNote = h("div", { id: "ai-models-note" });
    const modelsHolder = h("div.stack", null,
      h("div.row.row--between", null, modelsHead, refreshBtn),
      modelsChips, modelsNote);

    /* --- actions -------------------------------------------------------- */
    const testBtn = h("button.btn", { type: "button", id: "ai-test",
      text: "Test connection", onClick: runTest });
    const saveBtn = h("button.btn.btn--primary", { type: "button", id: "ai-save",
      text: "Save AI settings", onClick: saveSettings });
    const clearBtn = h("button.btn.btn--sm.btn--ghost", { type: "button", id: "ai-clear",
      text: "Clear provider", onClick: clearProvider });
    const saveKeyBtn = h("button.btn.btn--sm", { type: "button", id: "ai-save-key",
      text: "Save key", onClick: saveKey });
    const removeKeyBtn = h("button.btn.btn--sm.btn--ghost", { type: "button", id: "ai-remove-key",
      text: "Remove key", onClick: removeKey });
    const testHolder = h("div", { id: "ai-test-result" });

    const root = h("div.stack", null,
      statusBanner,
      providerField, presetNotes,
      modelField, modelsHolder,
      baseField, keyField, keyRequirement,
      h("div.row", null, saveKeyBtn, removeKeyBtn),
      keyState,
      toggles, advanced,
      h("div.row", null, testBtn, saveBtn, clearBtn),
      formError,
      h("p.sub", { text: "Test uses the saved key. If a key is stored and you change Base URL, Save AI settings first so the key is only sent to the saved endpoint." }),
      testHolder,
      h("div.row", null, h("button.btn.btn--sm.btn--danger", { type: "button",
        text: "Delete saved conversations", onClick: deleteConversations })),
      h("p.sub", { text: "Reads are cache-first; live reads need the setting above, an idle bus and a per-turn cap. The assistant's tools are read-only." }));

    on(providerControl, "change", () => {
      pending = null;
      applyPreset(true);
      invalidateTest();
      loadModels();
    });
    on(baseInput, "input", () => {
      updateBanner();
      invalidateTest();
      onEndpointEdit();
    });
    on(modelInput, "input", invalidateTest);
    on(toggles, "change", updateBanner);
    for (const input of [modelInput, baseInput, keyInput]) {
      on(input, "input", () => input.removeAttribute("aria-invalid"));
    }

    return { root, init, runTest, refresh };

    /* --- small helpers -------------------------------------------------- */
    function providerOptionLabel(row) {
      const parts = [row.label];
      if (row.requires_key) parts.push("key required");
      if (row.local) parts.push("runs locally");
      return parts.join(" · ");
    }

    function numberField(id, label, value, min, max, step = 1) {
      const node = field({ id, label, type: "number", value: String(value ?? ""),
        min, max, step, mono: true });
      return { node, input: node.querySelector("input") };
    }

    function flag(id) {
      const node = root.querySelector(`#${id}`);
      return Boolean(node && node.checked);
    }

    function isLocal(base, row) {
      if (row && row.format === "fake") return true;
      const text = (base || "").trim();
      if (!text) return Boolean(row && row.local);
      try { return AI_LOCAL_HOSTS.has(new URL(text).hostname); } catch { return false; }
    }

    function stamp(iso) {
      const date = new Date(iso);
      if (Number.isNaN(date.getTime())) return String(iso);
      return date.toLocaleString([], { month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit", hour12: false });
    }

    /* --- banner + provider change --------------------------------------- */
    // The server sends an explicit `state`; when the form is showing another
    // provider than the saved one, derive the state from the form + stored keys
    // so the banner never claims "ready" for a provider with no key.
    function setupState() {
      const id = providerControl.value;
      if (id === (config.provider || "") && config.state) return config.state;
      const row = byId.get(id) || null;
      if (!id) return savedProviders(config).length ? "key_without_provider" : "unconfigured";
      if (row && row.requires_key && !(config.keys || {})[id]?.configured) {
        return "provider_without_key";
      }
      return "ready";
    }

    function updateBanner() {
      clear(statusBanner);
      const state = setupState();
      const row = byId.get(providerControl.value) || null;
      const effectiveBase = baseInput.value.trim() || (row ? row.default_base_url : "");
      const local = isLocal(effectiveBase, row);
      if (state === "key_without_provider") {
        // The owner's trap: a key exists, but no provider was ever selected, so
        // "no provider configured" was technically true and completely useless.
        const id = config.suggested_provider || savedProviders(config)[0];
        const label = shortLabel(config, id);
        statusBanner.appendChild(banner(
          `${article(label)} ${label} key is saved, but no provider is selected.`, "warn",
          h("button.btn.btn--sm", { type: "button", text: `Use ${label}`,
            onClick: () => chooseProvider(id) })));
      } else if (state === "provider_without_key") {
        const label = shortLabel(config, providerControl.value || config.provider);
        statusBanner.appendChild(banner(
          `${label} is selected but has no key — paste one below and press Save key.`, "warn"));
      } else if (state === "invalid") {
        statusBanner.appendChild(banner(
          `The saved AI configuration is incomplete — ${config.state_reason || "check the provider, model and base URL"}.`,
          "warn"));
      } else {
        let kind = "info";
        let text;
        if (state === "unconfigured") {
          kind = "warn";
          text = "No provider configured. Pick a provider and paste a key, or point Base URL at a local model.";
        } else if (local) {
          text = "Local model: vehicle data does not leave this machine.";
        } else if (flag("local-only")) {
          kind = "warn";
          text = "Local models only is on: this remote provider will be refused. Turn that setting off or choose a local provider.";
        } else if (!flag("send-remote")) {
          kind = "warn";
          text = "Remote model: enable 'Send bus data to a remote model' before a task can attach vehicle data.";
        } else {
          text = `Remote model: bus data will be sent to ${effectiveBase || "the configured endpoint"}.`;
        }
        statusBanner.appendChild(banner(text, kind));
      }
      // A key just saved for a *different* provider: offer the switch explicitly
      // instead of pretending the save changed the configured provider. (With no
      // provider configured the state banner above already offers the switch.)
      if (pending && config.provider && pending !== config.provider) {
        const label = shortLabel(config, pending);
        const current = shortLabel(config, config.provider);
        statusBanner.appendChild(banner(
          `${label} key saved, but ${current} is still selected.`, "warn",
          h("button.btn.btn--sm", { type: "button", text: `Use ${label} instead`,
            onClick: () => chooseProvider(pending) })));
      }
    }

    // Select a provider explicitly via the server endpoint: it fills only empty
    // preset defaults and preserves any model or base URL already saved, so no
    // display value is written back and a custom value is never clobbered.
    async function chooseProvider(id) {
      if (providerControl.value !== id) {
        providerControl.value = id;
        applyPreset(true);
      }
      try {
        const updated = await api.aiSelectProvider(id);
        const label = shortLabel(config, id);
        // An environment override can keep another provider effective; name it
        // rather than claiming the selection took effect.
        if (updated && updated.provider && updated.provider !== id) {
          toast(`Saved ${label}; ${shortLabel(updated, updated.provider)} is set by the environment`, "warn", 0);
        } else {
          toast(`${label} selected`);
        }
        await loadAI();
      } catch (error) {
        toast(`${error.code || "error"}: ${error.message}`, "err", 0);
        if (/model|base URL/i.test(String(error?.message || ""))) modelInput.focus();
      }
    }

    function applyPreset(reset) {
      preset = byId.get(providerControl.value) || null;
      if (reset) {
        modelInput.value = preset ? (preset.default_model || "") : "";
        baseInput.value = preset ? (preset.default_base_url || "") : "";
      }
      presetNotes.textContent = preset ? (preset.notes || "") : "";
      shownListBase = null;
      fillDatalist(preset ? preset.example_models || [] : [], []);
      clear(modelsHead);
      clear(modelsChips);
      clear(modelsNote);
      modelsHead.appendChild(h("span.eyebrow", { text: "Available models" }));
      updateKeyRow();
      updateKeyState();
      updateBanner();
    }

    function updateKeyRow() {
      const row = byId.get(providerControl.value) || null;
      keyInput.placeholder = (row && row.key_hint) || "paste a key";
      const info = (config.keys || {})[providerControl.value] || null;
      const fromEnv = Boolean(info && info.source === "env");
      // An environment key wins over a file key, so saving one here would look
      // like it worked while the env value is still the one used. Disable both
      // actions and say what to change instead.
      keyInput.disabled = fromEnv;
      saveKeyBtn.disabled = fromEnv;
      removeKeyBtn.disabled = fromEnv || !(info && info.configured);
      if (!providerControl.value) {
        keyRequirement.textContent = "Choose a provider before saving a key.";
      } else if (fromEnv) {
        // The backend honours both the generic PACIFICA_OBD_AI_KEY_<ID> name and
        // the preset's declared vendor names; show both so the override is findable.
        const names = [...new Set([
          `PACIFICA_OBD_AI_KEY_${String(providerControl.value || "").toUpperCase()}`,
          ...((row && row.key_env) || []),
        ])];
        keyRequirement.textContent = `${row ? row.label : providerControl.value} is set by environment`
          + ` (${names.join(", ")}) — remove the env var to change it.`;
      } else if (row && row.requires_key && !(info && info.configured)) {
        keyRequirement.textContent = `no API key is configured for ${row.label}`;
      } else if (row && row.requires_key) {
        keyRequirement.textContent = "A saved key is required; Test uses it, not a key typed but not saved.";
      } else {
        keyRequirement.textContent = "No key needed for this provider (one is optional).";
      }
    }

    function updateKeyState() {
      clear(keyState);
      const keys = config.keys || {};
      const ids = Object.keys(keys);
      if (!ids.length) {
        keyState.appendChild(h("span.sub", { text: "No provider keys are stored." }));
        return;
      }
      for (const name of ids) {
        const info = keys[name] || {};
        const selected = name === providerControl.value;
        const label = shortLabel(config, name);
        const text = info.configured
          ? `${label}: configured ${info.hint || "…"}${info.source ? ` (${info.source})` : ""}`
          : `${label}: no key`;
        // A provider pill is also the selection control: choosing a provider and
        // saving its key are one flow, not two independent saves.
        keyState.appendChild(h("button.chip", {
          type: "button",
          "aria-pressed": selected ? "true" : "false",
          title: info.configured
            ? `Select ${label} (a key is saved)` : `Select ${label}`,
          onClick: () => chooseProvider(name),
        }, h("span.chip__label", { text })));
      }
    }

    function fillDatalist(suggested, discovered) {
      clear(modelList);
      const seen = new Set();
      for (const item of [...(suggested || []), ...(discovered || [])]) {
        const id = String(item && typeof item === "object" ? item.id : item || "").trim();
        if (!id || seen.has(id)) continue;
        seen.add(id);
        modelList.appendChild(h("option", { value: id }));
      }
    }

    /* --- model list ----------------------------------------------------- */
    async function loadModels({ refresh: reload = false } = {}) {
      const seq = (listSeq += 1);
      const id = providerControl.value || null;
      const row = byId.get(id) || null;
      clear(modelsNote);
      if (reload) {
        refreshBtn.disabled = true;
        modelsNote.appendChild(loadingState(`Asking ${row ? row.label : "the provider"} for its models…`));
      }
      let payload;
      try {
        payload = await requestModels(id, reload);
      } catch (error) {
        if (seq !== listSeq) return; // a newer request owns the form now
        refreshBtn.disabled = false;
        // Keep the last known list (and the datalist) in place: a failed refresh
        // must not erase a working list, it just reports that it could not refresh.
        clear(modelsNote);
        modelsNote.appendChild(modelsError(error, row));
        return;
      }
      if (seq !== listSeq) return; // superseded while this request was in flight
      refreshBtn.disabled = false;
      renderModels(payload, row);
    }

    async function requestModels(id, reload) {
      const params = { provider: id || undefined, refresh: reload ? 1 : undefined };
      // A cached read may target the endpoint currently in the form; a refresh
      // must use the saved one (the backend requires the configured provider).
      if (!reload) params.base_url = baseInput.value.trim() || undefined;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      try {
        return await api.aiModels(params, { signal: controller.signal });
      } finally {
        clearTimeout(timer);
      }
    }

    async function refresh() {
      if (!providerControl.value) { toast("Choose a provider first", "warn"); return; }
      if (providerControl.value !== (config.provider || "")) {
        toast("Save AI settings first to refresh this provider's model list", "warn");
        return;
      }
      if (endpointChangedSinceSave()) {
        // refresh=1 always probes the *saved* endpoint; showing its answer beside
        // an edited Base URL would mislabel the list.
        toast("Save AI settings first — Refresh fetches the saved endpoint", "warn");
        return;
      }
      await loadModels({ refresh: true });
    }

    // The endpoint the backend would use for the form values right now.
    function formEndpoint() {
      const row = byId.get(providerControl.value) || null;
      return baseInput.value.trim() || (row ? row.default_base_url : "");
    }

    function endpointChangedSinceSave() {
      const row = byId.get(config.provider || "") || null;
      const saved = config.base_url || (row ? row.default_base_url : "");
      return formEndpoint() !== saved;
    }

    // Editing Base URL invalidates the list on screen: it belongs to the old
    // endpoint. Keep the preset suggestions, drop the discovered chips, and say
    // what to do next rather than leaving a list that does not match the form.
    function onEndpointEdit() {
      if (shownListBase === null) return;
      if (formEndpoint() === shownListBase) return;
      shownListBase = null;
      const row = byId.get(providerControl.value) || null;
      fillDatalist(row ? row.example_models || [] : [], []);
      clear(modelsChips);
      clear(modelsNote);
      clear(modelsHead);
      modelsHead.appendChild(h("span.eyebrow", { text: "Available models" }));
      modelsHead.appendChild(h("span.sub", {
        text: "Base URL changed — Save AI settings, then Refresh to fetch this endpoint.",
      }));
    }

    function renderModels(payload, row) {
      const discovered = [...new Set((payload.discovered || [])
        .map((item) => (item && typeof item === "object" ? item.id : item))
        .filter(Boolean))];
      const suggested = payload.suggested && payload.suggested.length
        ? payload.suggested : (row ? row.example_models || [] : []);
      fillDatalist(suggested, discovered);
      shownListBase = payload.base_url || "";
      clear(modelsHead);
      clear(modelsChips);
      clear(modelsNote);
      const label = payload.label || (row && row.label) || "this provider";
      modelsHead.appendChild(h("span.eyebrow", { text: "Available models" }));
      modelsHead.appendChild(h("span.sub", { text: label }));
      // Name the endpoint the list actually came from: an edited Base URL must
      // never make a list for another endpoint look like it belongs here.
      if (payload.base_url) {
        modelsHead.appendChild(h("span.sub.mono", { text: payload.base_url }));
      }
      if (payload.checked_at) {
        modelsHead.appendChild(h("span.sub", { text: `as of ${stamp(payload.checked_at)}` }));
      }
      if (payload.source === "live") modelsHead.appendChild(tonePill("live", "info"));
      else if (payload.source === "cache") modelsHead.appendChild(tonePill("cached", "idle"));
      // `stale` is true for "no entry at all" too; only an actual dated entry can
      // be stale, and saying otherwise would claim a freshness problem that is
      // really just "nothing fetched yet".
      if (payload.stale && payload.checked_at) modelsHead.appendChild(pill("stale", "warn"));
      for (const id of discovered) {
        modelsChips.appendChild(h("button.chip", {
          type: "button", text: id, title: `Use ${id}`,
          onClick: () => { modelInput.value = id; modelInput.focus(); },
        }));
      }
      if (payload.supported === false) {
        const reason = payload.reason || "this endpoint does not advertise a model list";
        modelsNote.appendChild(h("p.sub", {
          text: `${label} has no model list to offer (${reason}). Type the model id you want — free text is always accepted.`,
        }));
      } else if (!discovered.length) {
        modelsNote.appendChild(h("p.sub", {
          text: payload.checked_at
            ? "The last check returned no models. Type a model id, or Refresh to ask again."
            : `No list fetched yet — Refresh to ask ${label}, or type a model id.`,
        }));
      }
    }

    function modelsError(error, row) {
      if (error && error.name === "AbortError") {
        return banner("The model list timed out after 10s — the endpoint did not answer.", "err");
      }
      const code = error?.code || "error";
      const message = error?.message || String(error);
      const hint = code === "no_key"
        ? "Save a key for this provider, then Refresh again."
        : code === "validation"
          ? "Save AI settings first, or choose the configured provider."
          : null;
      return h("div.stack", null,
        banner(`${code}: ${message}`, "err"),
        hint ? h("p.sub", { text: hint }) : null,
        row ? h("p.sub", { text: `Suggestions for ${row.label} are still available in the model field.` }) : null);
    }

    /* --- save / clear --------------------------------------------------- */
    function numberOrNull(input) {
      const text = input.value.trim();
      if (text === "") return null;
      const value = Number(text);
      return Number.isFinite(value) ? value : NaN;
    }

    function formPayload() {
      const id = providerControl.value || null;
      const model = modelInput.value.trim();
      const base = baseInput.value.trim() || null;
      const payload = {};
      // Only send provider/model/base_url when they actually changed: an
      // explicit provider or base_url invalidates the cached model list, so an
      // unrelated Save must not silently drop a list the user just fetched.
      if (id !== (config.provider || null)) payload.provider = id;
      if (model !== (config.model || "")) payload.model = model;
      if (base !== (config.base_url || null)) payload.base_url = base;
      payload.send_bus_data_to_remote = flag("send-remote");
      payload.redact_vin = flag("redact-vin");
      payload.allow_live_bus = flag("live-bus");
      payload.persist_conversations = flag("persist");
      payload.force_local_only = flag("local-only");
      for (const [key, cap] of Object.entries(caps)) {
        const value = numberOrNull(cap.input);
        if (value !== null && Number.isFinite(value)) payload[key] = value;
      }
      return payload;
    }

    function validate() {
      clearValidation();
      const id = providerControl.value;
      const row = byId.get(id) || null;
      const url = baseInput.value.trim();
      if (url) {
        let parsed = null;
        try { parsed = new URL(url); } catch { parsed = null; }
        if (!parsed) {
          // Mirror the backend: a bare scheme is "no host", anything else that
          // does not parse is "not http(s)".
          const bare = /^https?:\/\/[\/?#]*$/i.test(url);
          return { field: baseInput, message: bare
            ? "base_url must include a host name"
            : "base_url must be an http(s) URL" };
        }
        if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
          return { field: baseInput, message: "base_url must be an http(s) URL" };
        }
        if (!parsed.hostname) {
          return { field: baseInput, message: "base_url must include a host name" };
        }
        if (parsed.username || parsed.password) {
          return { field: baseInput,
            message: "base_url must not contain credentials; use the key field instead" };
        }
        if (parsed.protocol === "http:" && !AI_LOCAL_HOSTS.has(parsed.hostname)) {
          return { field: baseInput,
            message: "an http:// base_url is only allowed for a loopback host; use https:// for a remote endpoint" };
        }
      } else if (id && row && row.format !== "fake" && !row.default_base_url) {
        return { field: baseInput, message: "base_url is required for this provider" };
      }
      if (id && id !== "fake" && !modelInput.value.trim() && row && !row.default_model) {
        return { field: modelInput, message: "a model id is required once a provider is chosen" };
      }
      for (const [key, cap] of Object.entries(caps)) {
        if (key === "temperature") continue;
        const value = numberOrNull(cap.input);
        if (value === null) continue;
        const [low, high] = AI_BOUNDS[key];
        // The backend caps are integers; a fractional value would be rejected as
        // a whole by Pydantic, so reject it here with the same bound wording.
        if (!Number.isInteger(value) || value < low || value > high) {
          return { field: cap.input,
            message: `${key} must be a whole number between ${low} and ${high}` };
        }
      }
      const temperature = numberOrNull(caps.temperature.input);
      if (temperature !== null
          && (!Number.isFinite(temperature) || temperature < 0 || temperature > 2)) {
        return { field: caps.temperature.input, message: "temperature must be between 0 and 2" };
      }
      return null;
    }

    function clearValidation() {
      formError.textContent = "";
      for (const input of [modelInput, baseInput, keyInput]) {
        input.removeAttribute("aria-invalid");
      }
    }

    function showProblem(problem) {
      if (!problem) return false;
      clearValidation();
      if (problem.field) {
        problem.field.setAttribute("aria-invalid", "true");
        problem.field.focus();
      }
      formError.textContent = problem.message;
      toast(problem.message, "warn", 0);
      return true;
    }

    function highlight(error) {
      const message = String(error?.message || "");
      if (/base_url/i.test(message)) {
        baseInput.setAttribute("aria-invalid", "true");
        baseInput.focus();
      } else if (/model/i.test(message)) {
        modelInput.setAttribute("aria-invalid", "true");
        modelInput.focus();
      } else if (/key/i.test(message)) {
        keyInput.focus();
      }
    }

    async function saveSettings() {
      if (showProblem(validate())) return;
      try {
        await api.aiSetConfig(formPayload());
        toast("AI settings saved");
        await loadAI();
      } catch (error) {
        toast(`${error.code || "error"}: ${error.message}`, "err", 0);
        highlight(error);
      }
    }

    async function clearProvider() {
      try {
        await api.aiSetConfig({ provider: null, base_url: null });
        toast("Provider cleared");
        await loadAI();
      } catch (error) {
        toast(`${error.code || "error"}: ${error.message}`, "err", 0);
      }
    }

    async function saveKey() {
      const id = providerControl.value;
      const value = keyInput.value;
      if (!id) { toast("Choose a provider before saving a key", "warn"); return; }
      if (!value) { toast("Paste a key first", "warn"); keyInput.focus(); return; }
      const label = shortLabel(config, id);
      // Values typed but not yet saved: a key save reloads the card, so carry a
      // non-empty model/base URL over instead of silently discarding the draft.
      const draftModel = modelInput.value.trim();
      const draftBase = baseInput.value.trim() || null;
      try {
        const result = await api.aiSetKey({ provider: id, key: value });
        keyInput.value = "";
        const hint = result.key?.hint || "hidden";
        // The backend selects the saved provider when none was configured, and
        // reports why it did not switch an existing one. The unselected case
        // reloads with `pending`, which renders an actionable "Use X instead".
        if (result.selected === false) {
          toast(`Key saved (${hint}) — ${result.reason || "not selected"}`, "warn", 0);
          // Nothing was selected before, so this draft *is* the user configuring
          // this provider (a custom endpoint with no preset defaults, say):
          // persist it so the reload does not discard what they typed. When
          // another provider is configured the draft belongs to the key's
          // provider and must not overwrite the configured one.
          if (!config.provider && (draftModel || draftBase)) {
            try { await api.aiSetConfig({ provider: id, model: draftModel, base_url: draftBase }); }
            catch (error) { toast(`Settings not saved: ${error.message}`, "warn", 0); }
          }
        } else if (result.selected === true) {
          const patch = {};
          if (draftModel && draftModel !== (config.model || "")) patch.model = draftModel;
          if (draftBase && draftBase !== (config.base_url || null)) patch.base_url = draftBase;
          if (Object.keys(patch).length) {
            try { await api.aiSetConfig(patch); }
            catch (error) { toast(`Settings not saved: ${error.message}`, "warn", 0); }
          }
          toast(`Key saved (${hint}) — ${label} selected`);
        } else {
          // An older server does not report `selected`: never claim a switch.
          toast(`Key saved (${hint})`);
        }
        await loadAI(result.selected === false ? id : null);
      } catch (error) {
        toast(`${error.code || "error"}: ${error.message}`, "err", 0);
      }
    }

    async function removeKey() {
      const id = providerControl.value;
      if (!id) return;
      try {
        await api.aiDeleteKey(id);
        toast("Key removed");
        await loadAI();
      } catch (error) {
        toast(`${error.code || "error"}: ${error.message}`, "err", 0);
      }
    }

    async function deleteConversations() {
      try {
        await api.aiDeleteConversations();
        toast("Saved conversations deleted");
      } catch (error) {
        toast(`${error.code || "error"}: ${error.message}`, "err");
      }
    }

    /* --- test connection ------------------------------------------------ */
    function formSignature() {
      return `${providerControl.value}|${modelInput.value.trim()}|${baseInput.value.trim()}`;
    }

    function invalidateTest() {
      formError.textContent = "";
      // While a test is in flight its completion is discarded by the signature
      // check, so the loading block can stay until then.
      if (!testInFlight) clear(testHolder);
    }

    // The test uses the *saved* key. A changed-but-unsaved endpoint would make
    // the backend send that key to an endpoint the user has not committed to, so
    // require a save first whenever a credential is stored.
    function testEndpointGuard() {
      const id = providerControl.value;
      const info = (config.keys || {})[id];
      if (!info || !info.configured) return null; // no stored credential to leak
      const formBase = baseInput.value.trim();
      if (!formBase) return null; // empty means the saved/preset endpoint
      const row = byId.get(id) || null;
      const trusted = id === (config.provider || "")
        ? (config.effective_base_url || (row ? row.default_base_url : ""))
        : (row ? row.default_base_url : "");
      if (formBase === trusted) return null;
      return "Save AI settings before testing: a stored key is only sent to the saved endpoint.";
    }

    async function runTest() {
      if (testInFlight) return;
      if (showProblem(validate())) return;
      const blocked = testEndpointGuard();
      if (blocked) { showProblem({ message: blocked }); return; }
      const signature = formSignature();
      testInFlight = true;
      testBtn.disabled = true;
      testBtn.setAttribute("aria-disabled", "true");
      clear(testHolder);
      testHolder.appendChild(loadingState("Testing the provider…"));
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      try {
        const result = await api.aiTest({
          provider: providerControl.value || null,
          model: modelInput.value.trim(),
          base_url: baseInput.value.trim() || null,
          probe: true,
        }, { signal: controller.signal });
        if (signature !== formSignature()) return; // the form moved on
        renderTest(result);
        if (result && result.ok) loadModels();
      } catch (error) {
        if (signature !== formSignature()) return;
        if (error && error.name === "AbortError") {
          clear(testHolder);
          testHolder.appendChild(banner("The test timed out after 10s — the endpoint did not answer.", "err"));
        } else {
          renderTestError(error);
        }
      } finally {
        clearTimeout(timer);
        testInFlight = false;
        testBtn.disabled = false;
        testBtn.removeAttribute("aria-disabled");
      }
    }

    function renderTest(result) {
      clear(testHolder);
      const checks = (result && result.checks) || [];
      const models = [...new Set(((result && result.models) || []).map(String))];
      if (result && result.ok) {
        testHolder.appendChild(banner(models.length
          ? `Reachable — ${models.length} model(s)` : "Reachable", "ok"));
      } else {
        const error = (result && result.error) || {};
        testHolder.appendChild(banner(
          `${error.code || "error"}: ${error.message || "the test failed"}`, "err"));
        const hint = hintFor(error.code, (result && result.base_url) || baseInput.value.trim());
        if (hint) testHolder.appendChild(h("p.sub", { text: hint }));
        if (error.code === "no_key") keyInput.focus();
      }
      if (models.length) {
        testHolder.appendChild(h("p.sub", { text: "Click a discovered model to use it:" }));
        testHolder.appendChild(h("div.chips.chips--picker", null, models.map((id) =>
          h("button.chip", { type: "button", text: id, title: `Use ${id}`,
            onClick: () => { modelInput.value = id; modelInput.focus(); } }))));
      }
      if (checks.length) {
        testHolder.appendChild(dataTable({
          columns: [
            { label: "Check", key: "name", mono: true },
            { label: "Result", render: (row) => pill(row.ok ? "ok" : "failed", row.ok ? "ok" : "crit") },
            { label: "Detail", render: (row) => h("span.sub", { text: row.detail || "—" }) },
          ],
          rows: checks,
        }));
      }
    }

    function renderTestError(error) {
      clear(testHolder);
      const code = error?.code || "error";
      testHolder.appendChild(banner(`${code}: ${error?.message || String(error)}`, "err"));
      const hint = hintFor(code, baseInput.value.trim());
      if (hint) testHolder.appendChild(h("p.sub", { text: hint }));
      if (code === "no_key") keyInput.focus();
    }

    function hintFor(code, endpoint) {
      switch (code) {
        case "no_key":
          return "Save a key above, then Test again — the test uses the saved key, not one typed but unsaved.";
        case "auth":
          return "The provider rejected the key — check it, then Save key again.";
        case "network":
        case "timeout":
          return `Could not reach ${(endpoint || "the endpoint").trim()} — is the server running?`;
        case "bad_request":
          return "The endpoint answered, but not as a chat API — check Base URL.";
        default:
          return null;
      }
    }

    /* --- lifecycle ------------------------------------------------------ */
    async function init() {
      applyPreset(false);
      await loadModels();
    }
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
