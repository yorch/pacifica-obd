// AI assistant — #/assistant
//
// A read-only diagnostics copilot. Tokens stream over SSE; the tool timeline
// shows each read-only call; the cost meter shows tokens and an estimate. There
// is no write affordance anywhere on this screen, and the badge says so.

import { h, clear, mount, on } from "../dom.js";
import { api, post, ApiError } from "../api.js";
import { postStream } from "../stream.js";
import { card, pill, banner, errorState, toast, modal, untrustedBadge } from "../components.js";

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
    h("div.grid.grid--two", null,
      card({ eyebrow: "Cost", title: "Token meter", body: meter }),
      card({ eyebrow: "Tools", title: "Activity (read-only)", body: timeline })),
    card({ eyebrow: "Start", title: "Suggested starts",
      body: h("div.row", null, SUGGESTED.map(([label, task]) =>
        h("button.btn.btn--sm.btn--ghost", { type: "button", text: label,
          onClick: () => send("", task) }))) }),
    composer());

  loadChips();
  renderMeter(null);
  renderNotice();

  // Deterministic deep link for screenshots: ask a question on load.
  const ask = ctx.params?.get("ask");
  if (ask) setTimeout(() => send(ask, ctx.params?.get("task") || null), 60);

  return { dispose: stop, resize() {} };

  function composer() {
    const input = h("textarea.composer__input", { rows: 3, placeholder: "Ask a question about the codes, a module, a DID or your stored sessions…" });
    const toolsToggle = h("input", { type: "checkbox" });
    return card({ eyebrow: "Prompt", title: "Message", body: h("div", null,
      input,
      h("div.row.row--between", null,
        h("label.field.field--check", null, toolsToggle,
          h("span", { text: "Use the bounded tool loop (opt-in; read-only tools)" })),
        h("button.btn", { type: "button", text: "Send", onClick: () => send(input.value, null, toolsToggle.checked) })),
      h("p.sub", { text: "Vehicle data is fenced as untrusted before it reaches any model." })) });
  }

  async function loadChips() {
    clear(chipRow);
    try {
      const config = await api.aiConfig();
      const chips = [
        `provider: ${config.provider || "none"}`,
        `model: ${config.model || "—"}`,
        config.is_local ? "local model" : "remote model",
        config.redact_vin ? "VIN redacted" : "VIN visible",
        config.persist_conversations ? "history saved" : "history off",
      ];
      for (const text of chips) chipRow.appendChild(h("span.pill.pill--idle", { text }));
    } catch {
      chipRow.appendChild(h("span.pill.pill--idle", { text: "no provider configured" }));
    }
  }

  function renderNotice() {
    clear(notice);
    api.aiConfig().then((config) => {
      if (!config.provider) {
        notice.appendChild(banner("No provider is configured. Open Settings → AI to add a key or point at a local model.", "warn",
          h("a.btn.btn--sm", { href: "#/settings", text: "Open Settings" })));
      }
    }).catch(() => {});
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
      const last = timeline.querySelector(".tool:last-child .tool__result");
      const line = h("div.tool__result", null,
        h("span.sub", { text: data.summary || "" }), " ", untrustedBadge());
      if (last) last.replaceWith(line); else timeline.appendChild(line);
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
    if (!usage) { meter.appendChild(h("span.sub", { text: "No usage yet." })); return; }
    meter.appendChild(h("dl.kv", null, [
      ["Input tokens", usage.input_tokens], ["Output tokens", usage.output_tokens],
      ["Total", usage.total_tokens],
      ["Est. USD", typeof usage.usd_estimate === "number" ? usage.usd_estimate.toFixed(4) : "—"],
      ["Requests", usage.requests],
    ].flatMap(([key, value]) => [h("dt", { text: key }),
                                 h("dd.mono", { text: String(value ?? "—") })])));
    meter.appendChild(h("p.sub", { text: "An estimate, not a bill. Local models are free." }));
  }

  function showError(error) {
    const message = error instanceof ApiError ? `${error.code}: ${error.message}`
      : (error?.message || String(error));
    const retry = error?.detail?.retry_after_s || error?.retry_after_s;
    notice.appendChild(banner(retry ? `${message} (retry in ${retry}s)` : message, "err"));
    toast(message, "err", 0);
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
  }

  function flushNow() {
    if (frame) { clearTimeout(frame); frame = null; }
    flush();
  }

  async function previewEgress() {
    try {
      const preview = await api.aiWhatWillBeSent("(next message)");
      const body = h("div", null,
        h("dl.kv", null, [["Provider", preview.provider_label || preview.provider],
          ["Model", preview.model], ["Local", preview.local ? "yes" : "no"],
          ["VIN redaction", preview.redact_vin ? "on" : "off"],
          ["Remote bus data", preview.send_bus_data_to_remote ? "allowed" : "off"]]
          .flatMap(([key, value]) => [h("dt", { text: key }),
                                      h("dd", { text: String(value ?? "—") })])),
        h("div.eyebrow", { text: "system prompt" }),
        h("pre.code-block", { text: preview.system }),
        h("div.eyebrow", { text: "user message" }),
        h("pre.code-block", { text: preview.user }),
        h("p.sub", { text: `${(preview.tools || []).length} read-only tool schemas are attached. Tool results are fetched later.` }));
      modal({ title: "What will be sent", content: body });
    } catch (error) { showError(error); }
  }
}
