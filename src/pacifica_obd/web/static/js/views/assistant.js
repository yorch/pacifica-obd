// AI assistant — #/assistant
//
// A read-only diagnostics copilot. Tokens stream over SSE; the tool timeline
// shows each read-only call; the cost meter shows tokens and an estimate. There
// is no write affordance anywhere on this screen, and the badge says so.

import { h, clear, mount, on } from "../dom.js";
import { api, post, ApiError } from "../api.js";
import { postStream } from "../stream.js";
import { card, pill, banner, toast, modal, untrustedBadge, emptyState, keyValues, checkbox } from "../components.js";

const SUGGESTED = [
  ["Pre-trip health check", "pretrip"],
  ["Explain the codes on this car", "diagnose"],
  ["What changed between my last two sessions?", "compare"],
  ["Write a report for my mechanic", "report"],
];

const READ_ONLY_BADGE = "This assistant can read; it cannot write.";

export function render(root, ctx) {
  const transcript = h("div.transcript", { "aria-live": "polite" });
  const timeline = h("div.timeline");
  const meter = h("div.meter");
  const chipRow = h("div.chips");
  const notice = h("div");
  let stream = null;
  let conversation = null;
  let buffer = "";
  let frame = null;
  let stopButton = null;
  const state = { assistant: null };

  mount(root,
    h("div.view__head", null,
      h("div", null, h("h1", { text: "Assistant" }),
        h("div.sub", { text: "A diagnostics copilot over this tool's read-only data. Bring your own key or run a local model." })),
      h("div.row", null, pill("read-only", "ok"), h("span.sub", { text: READ_ONLY_BADGE }))),
    chipRow,
    notice,
    card({ eyebrow: "Conversation", title: "Ask about this vehicle",
      actions: h("div.row", null,
        h("button.btn.btn--sm.btn--ghost", { type: "button", text: "What will be sent", onClick: previewEgress }),
        stopButton = h("button.btn.btn--sm.btn--danger", { type: "button", text: "Stop", disabled: true, onClick: stop }),
        h("button.btn.btn--sm", { type: "button", text: "New", onClick: reset })),
      body: h("div", null, transcript) }),
    h("div.grid.grid--2", null,
      card({ eyebrow: "Cost", title: "Token meter", body: meter }),
      card({ eyebrow: "Tools", title: "Activity (read-only)", body: timeline })),
    card({ eyebrow: "Start", title: "Suggested starts",
      body: h("div.chips", null, SUGGESTED.map(([label, task]) =>
        h("button.chip", { type: "button", text: label,
          onClick: () => send("", task) }))) }),
    composer());

  timeline.appendChild(emptyState({ icon: "sparkles", title: "No tool calls yet",
    hint: "Turn on the bounded tool loop and ask a question; every read-only call is listed here." }));

  loadChips();
  renderMeter(null);
  renderNotice();
  showEmptyTranscript();

  // Deterministic deep link for screenshots: ask a question on load.
  const ask = ctx.params?.get("ask");
  if (ask) setTimeout(() => send(ask, ctx.params?.get("task") || null), 60);

  return { dispose: stop, resize() {} };

  function composer() {
    const input = h("textarea.composer__input", { rows: 3, placeholder: "Ask a question about the codes, a module, a DID or your stored sessions…" });
    const toolsBox = checkbox({ id: "assistant-tools", flush: true,
      label: "Use the bounded tool loop (opt-in; read-only tools)" });
    const toolsToggle = toolsBox.querySelector("input");
    return card({ eyebrow: "Prompt", title: "Message", body: h("div.stack", null,
      input,
      h("div.row.row--between", null, toolsBox,
        h("button.btn.btn--primary", { type: "button", text: "Send", onClick: () => send(input.value, null, toolsToggle.checked) })),
      h("p.sub", { text: "Vehicle data is fenced as untrusted before it reaches any model." })) });
  }

  function showEmptyTranscript() {
    if (transcript.children.length) return;
    mount(transcript, emptyState({
      icon: "sparkles", title: "No messages yet",
      hint: "Ask about the codes, a module or your stored sessions — or start from one of the suggestions below. The assistant can read; it cannot write.",
    }));
  }

  async function loadChips() {
    clear(chipRow);
    try {
      const config = await api.aiConfig();
      const state = configState(config);
      const saved = savedProviders(config);
      const presetLabel = config.preset?.label || null;
      const model = config.effective_model || config.model || "—";
      const chips = [
        `provider: ${presetLabel || config.provider || "none"}`,
        `model: ${model}`,
        config.is_local ? "local model" : (config.provider ? "remote model" : "no model"),
        config.redact_vin ? "VIN redacted" : "VIN visible",
        config.persist_conversations ? "history saved" : "history off",
      ];
      // A saved key is not a configured provider: name the key instead of letting
      // the "provider: none" chip imply there is nothing to act on.
      if (state === "key_without_provider" && saved.length) {
        chips.splice(1, 0, `key saved: ${saved.map((id) => providerShort(config, id)).join(", ")}`);
      }
      for (const text of chips) chipRow.appendChild(pill(text, "idle"));
    } catch {
      chipRow.appendChild(pill("AI configuration unavailable", "idle"));
    }
  }

  // The server sends an explicit `state`; derive it from the payload when talking
  // to an older server so a saved key is never misreported as "no provider".
  function configState(config) {
    if (config.state) return config.state;
    const saved = savedProviders(config);
    if (!config.provider) return saved.length ? "key_without_provider" : "unconfigured";
    if (config.requires_key && !config.keys?.[config.provider]?.configured) {
      return "provider_without_key";
    }
    return "ready";
  }

  function savedProviders(config) {
    if (Array.isArray(config.saved_key_providers)) return config.saved_key_providers;
    return Object.entries(config.keys || {})
      .filter(([, info]) => info?.configured)
      .map(([id]) => id);
  }

  function providerShort(config, provider) {
    const label = config?.provider_labels?.[provider] || provider || "provider";
    return String(label).replace(/\s*\(.*\)\s*$/, "");
  }

  function article(name) {
    return /^(LM|LLM|[AEIOU])/i.test(String(name)) ? "An" : "A";
  }

  function renderNotice() {
    clear(notice);
    api.aiConfig().then((config) => {
      const state = configState(config);
      if (state === "key_without_provider") {
        const provider = config.suggested_provider || savedProviders(config)[0];
        const label = providerShort(config, provider);
        // Selecting needs a model; a custom endpoint with no default model is
        // sent to Settings where the model field lives. An existing saved model
        // wins over the preset default, matching the server's preserve-nonempty
        // selection rule.
        const model = config.model || config.defaults?.models?.[provider] || "";
        const action = model
          ? h("button.btn.btn--sm", { type: "button", text: `Use ${label}`,
              onClick: () => selectSavedProvider(config, provider) })
          : h("a.btn.btn--sm", { href: "#/settings", text: "Open Settings" });
        notice.appendChild(banner(
          `${article(label)} ${label} key is saved, but no provider is selected — Use ${label}.`,
          "warn", action));
        return;
      }
      if (state === "provider_without_key") {
        const label = providerShort(config, config.provider);
        notice.appendChild(banner(`${label} is selected but has no key — add one.`, "warn",
          h("a.btn.btn--sm", { href: "#/settings", text: "Add key" })));
        return;
      }
      if (state === "invalid") {
        notice.appendChild(banner(
          `The AI configuration is incomplete — ${config.state_reason || "open Settings to fix it"}.`,
          "warn", h("a.btn.btn--sm", { href: "#/settings", text: "Open Settings" })));
        return;
      }
      if (state === "unconfigured") {
        notice.appendChild(banner("No provider is configured. Open Settings → AI to add a key or point at a local model.", "warn",
          h("a.btn.btn--sm", { href: "#/settings", text: "Open Settings" })));
        return;
      }
      // Ready, but two policy facts can still stop a request: local-only mode
      // refuses a remote provider outright, and remote tasks need consent.
      if (config.force_local_only && !config.is_local) {
        notice.appendChild(banner("Local models only is on: this remote provider will be refused. Choose a local model or turn the setting off.", "warn",
          h("a.btn.btn--sm", { href: "#/settings", text: "Open Settings" })));
      } else if (!config.is_local && !config.send_bus_data_to_remote) {
        notice.appendChild(banner("Remote model: enable 'send bus data to a remote model' before a task can attach vehicle data.", "info",
          h("a.btn.btn--sm", { href: "#/settings", text: "Open Settings" })));
      }
    }).catch(() => {});
  }

  async function selectSavedProvider(config, provider) {
    try {
      // Only the id: the server fills preset defaults and preserves any model or
      // base URL already saved, so a redacted display value is never written back.
      const updated = await api.aiSelectProvider(provider);
      const label = providerShort(config, provider);
      // An environment override can keep another provider effective; say so
      // instead of claiming the click took effect.
      if (updated && updated.provider && updated.provider !== provider) {
        toast(`Saved ${label}; ${providerShort(updated, updated.provider)} is set by the environment`, "warn", 0);
      } else {
        toast(`${label} selected`);
      }
      loadChips();
      renderNotice();
    } catch (error) { showError(error); }
  }

  function bubble(role) {
    const body = h("div.bubble__body");
    const node = h("div", { class: `bubble bubble--${role}` },
      h("div.bubble__role", { text: role === "user" ? "You" : "Assistant" }), body);
    transcript.appendChild(node);
    transcript.scrollTop = transcript.scrollHeight;
    return body;
  }

  function flush() {
    frame = null;
    const target = state.assistant;
    if (target) {
      target.textContent = buffer;
      transcript.scrollTop = transcript.scrollHeight;
    }
  }

  function send(text, task, tools = false) {
    if (stream) { toast("A turn is already running", "warn"); return; }
    // Only the placeholder is cleared: a running conversation accumulates.
    if (transcript.querySelector(".empty")) clear(transcript);
    if (text) bubble("user").textContent = text;
    const assistant = bubble("assistant");
    state.assistant = assistant;
    buffer = "";
    clear(timeline);
    startButtonState(true);
    const payload = text ? { message: text, tools } : { tools };
    const path = task ? `/api/ai/tasks/${task}` : "/api/ai/chat";
    // `collect=1` is a non-streaming drain of the same engine, used by headless
    // screenshot automation (a streaming fetch body is not pumped under
    // `--virtual-time-budget`). The live UI always streams.
    if (ctx.params?.get("collect") === "1") {
      post(`${path}?collect=1`, payload).then((result) => {
        for (const item of result.events || []) handleEvent(item.event, item.data);
        stream = null;
        startButtonState(false);
        flushNow();
      }).catch((error) => { showError(error); startButtonState(false); });
      return;
    }
    stream = postStream(path, payload, {
      onEvent: (event, data) => handleEvent(event, data),
      onError: (error) => { showError(error); },
      onClose: () => { stream = null; startButtonState(false); flushNow(); },
    });
  }

  function handleEvent(event, data) {
    if (event === "meta") {
      conversation = data.conversation_id;
      clear(timeline);
      timeline.appendChild(h("div.sub", { text: `context: ${data.context} · tools: ${data.tools ? "on" : "off"} · ${data.redact_vin ? "VIN redacted" : "VIN visible"}` }));
    } else if (event === "token") {
      buffer += data.text || "";
      if (!frame) frame = setTimeout(flush, 16);
    } else if (event === "tool_call") {
      timeline.appendChild(h("div.tool", null,
        h("span.mono", { text: data.name }), " ",
        h("span.sub.mono", { text: JSON.stringify(data.args || {}) })));
    } else if (event === "tool_result") {
      const host = timeline.querySelector(".tool:last-child");
      const line = h("div.tool__result", null,
        h("span.sub", { text: data.summary || "" }), " ", untrustedBadge());
      (host || timeline).appendChild(line);
    } else if (event === "usage") {
      renderMeter(data);
    } else if (event === "warning") {
      notice.appendChild(banner(data.message || data.code, "warn"));
    } else if (event === "error") {
      showError(data);
    } else if (event === "done") {
      renderMeter(data);
      if (data.invalid_citations && data.invalid_citations.length) {
        notice.appendChild(banner(`The model cited evidence that was not attached: ${data.invalid_citations.join(", ")}`, "warn"));
      }
    }
  }

  function renderMeter(usage) {
    clear(meter);
    if (!usage) {
      mount(meter, emptyState({ icon: "gauge", title: "No usage yet",
        hint: "Token counts and an estimated cost appear after the first answer." }));
      return;
    }
    meter.appendChild(keyValues([
      ["Input tokens", usage.input_tokens], ["Output tokens", usage.output_tokens],
      ["Total", usage.total_tokens],
      ["Est. USD", typeof usage.usd_estimate === "number" ? usage.usd_estimate.toFixed(4) : "—"],
      ["Requests", usage.requests],
    ]));
    meter.appendChild(h("p.sub", { text: "An estimate, not a bill. Local models are free." }));
  }

  function showError(error) {
    let code = error instanceof ApiError ? error.code : error?.code;
    let message = error instanceof ApiError ? `${error.code}: ${error.message}`
      : (error?.message || String(error));
    let retry = error?.detail?.retry_after_s || error?.retry_after_s;
    // postStream surfaces a non-2xx body as a plain Error whose message is the
    // raw error envelope; decode it so a no_key/no_provider failure is recognised.
    if (!code && typeof message === "string" && message.trim().startsWith("{")) {
      try {
        const parsed = JSON.parse(message);
        if (parsed && parsed.error) {
          code = parsed.error.code;
          message = `${parsed.error.code}: ${parsed.error.message}`;
          retry = parsed.error.detail?.retry_after_s || retry;
        }
      } catch { /* not an envelope: keep the raw text */ }
    }
    // A missing provider or key is a setup problem, not a failure of this turn:
    // point at Settings instead of leaving a bare error with no next step.
    const needsSetup = code === "no_key" || code === "no_provider";
    const text = retry ? `${message} (retry in ${retry}s)` : message;
    notice.appendChild(banner(text, needsSetup ? "warn" : "err",
      needsSetup ? h("a.btn.btn--sm", { href: "#/settings", text: "Open Settings" }) : null));
    toast(text, needsSetup ? "warn" : "err", 0);
  }

  function startButtonState(running) {
    if (stopButton) stopButton.disabled = !running;
  }

  function stop() {
    if (stream) { stream.close(); stream = null; toast("Stopped", "warn"); }
    startButtonState(false);
  }

  function reset() {
    stop();
    clear(transcript); clear(timeline); clear(notice);
    conversation = null;
    renderMeter(null);
    showEmptyTranscript();
  }

  function flushNow() {
    if (frame) { clearTimeout(frame); frame = null; }
    flush();
  }

  async function previewEgress() {
    try {
      const preview = await api.aiWhatWillBeSent("(next message)");
      const body = h("div", null,
        keyValues([
          ["Provider", preview.provider_label || preview.provider],
          ["Model", preview.model], ["Local", preview.local ? "yes" : "no"],
          ["VIN redaction", preview.redact_vin ? "on" : "off"],
          ["Remote bus data", preview.send_bus_data_to_remote ? "allowed" : "off"],
        ], { sans: true }),
        h("div.eyebrow", { text: "system prompt" }),
        h("pre.code-block", { text: preview.system }),
        h("div.eyebrow", { text: "user message" }),
        h("pre.code-block", { text: preview.user }),
        h("p.sub", { text: `${(preview.tools || []).length} read-only tool schemas are attached. Tool results are fetched later.` }));
      modal({ title: "What will be sent", content: body });
    } catch (error) { showError(error); }
  }
}
