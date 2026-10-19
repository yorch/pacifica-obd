// Raw console — #/raw
//
// One bounded command per submit. The server classifies every command into a
// tier (read / confirm / deny) and that decision is the single source of truth:
// the badge is rendered from `GET /api/raw/allowlist?command=…`, and `POST
// /api/raw` sends a read command straight away or returns a single-use confirm
// token for the confirm tier. A deny never reaches the adapter.

import { h, mount, on, debounce } from "../dom.js";
import { api, ApiError } from "../api.js";
import { escapeControls } from "../format.js";
import {
  card, pill, banner, errorState, toast, typedConfirm, dataTable, emptyState, tierPill,
  loadingState,
} from "../components.js";

// Read = neutral, confirm = amber, deny = red. Never colour alone: the badge
// always carries the tier word and a glyph (components.js:tierPill).
const POINTERS = {
  security: { href: "#/security", label: "Open Security 0x27" },
  writes: { href: "#/writes", label: "Open Guarded writes" },
};

export function render(root, ctx) {
  const params = ctx.params || new URLSearchParams();
  const health = ctx.status?.health;
  const writesEnabled = health ? !health.read_only : false;

  const holders = {
    badge: h("div.row"),
    result: h("div"),
    history: h("div"),
    allowlist: h("div"),
  };
  const history = [];

  const input = h("input.mono", {
    id: "raw-command", type: "text", autocomplete: "off", spellcheck: "false",
    placeholder: "22F190 or ATI", value: params.get("command") || "",
    "aria-label": "raw command",
  });
  const sendButton = h("button.btn.btn--primary", {
    type: "button", text: "Send", onClick: () => send(),
  });
  on(input, "input", debounce(() => updateTier(), 150));
  on(input, "keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); send(); }
  });

  function fieldNode() {
    return h("div", null,
      h("div.field", null,
        h("label", { for: "raw-command", text: "Command (hex bytes or one AT command)" }),
        input),
      h("div.row.row--spaced", sendButton,
        h("button.btn.btn--ghost.btn--sm", {
          type: "button", text: "Reset addressing",
          title: "Send ATD (set all adapter settings to their defaults)",
          onClick: () => { input.value = "ATD"; send(); },
        })),
      holders.badge);
  }

  mount(root,
    h("div.view__head", null,
      h("div", null, h("h1", { text: "Raw console" }),
        h("div.sub", { text: "Send one bounded command to the adapter. Every command is classified before it is sent; a deny never reaches the bus." }))),
    banner(writesEnabled
      ? "Write mode: the confirm tier sends only after a typed acknowledgement. Read-tier commands send immediately."
      : "Read-only: only the read-tier commands send. Anything the server classifies as confirm or deny is refused.",
      writesEnabled ? "warn" : "info"),
    card({ eyebrow: "Command", title: "Send a raw command", body: fieldNode() }),
    card({ eyebrow: "Response", title: "Adapter lines and parsed frames", body: holders.result }),
    card({ eyebrow: "Session", title: "History", body: holders.history }),
    card({ eyebrow: "Reference", title: "Tiers", body: holders.allowlist }));

  mount(holders.result, emptyState({
    icon: "»", title: "Nothing sent yet",
    hint: "Type a command above and press Send. A 22 <DID> read needs the header set first (ATSH 7E0).",
  }));
  renderHistory();
  loadAllowlist();
  updateTier();

  return { dispose() {}, resize() {} };

  /* ------------------------------------------------------------- tier badge */
  async function updateTier() {
    const command = input.value.trim();
    if (!command) { mount(holders.badge); return; }
    try {
      const payload = await api.rawAllowlist(command);
      if (input.value.trim() !== command) return; // a newer keystroke won
      mount(holders.badge, tierLine(payload.decision));
    } catch { mount(holders.badge); }
  }

  function tierLine(decision) {
    if (!decision) return h("div");
    const pointer = decision.pointer ? POINTERS[decision.pointer] : null;
    const word = decision.tier === "invalid" ? "deny" : decision.tier;
    return h("div", { class: `tiernote tiernote--${word}`, role: "status" },
      h("div.row", null, tierPill(decision.tier),
        h("span.sub", { text: escapeControls(decision.reason) })),
      decision.note ? h("p.sub", null, escapeControls(decision.note), " ",
        pointer ? h("a", { href: pointer.href, text: pointer.label }) : null) : null,
      decision.tier === "confirm" && !writesEnabled
        ? h("p.sub", { text: "This server is read-only, so a confirm-tier command cannot be sent." })
        : null);
  }

  /* -------------------------------------------------------------- sending */
  async function send() {
    const command = input.value.trim();
    if (!command) { toast("Type a command first", "warn"); return; }
    sendButton.disabled = true;
    mount(holders.result, loadingState("Sending one command to the adapter…"));
    try {
      const result = await api.raw({ command, timeout_ms: 100 });
      if (result.confirm_token) {
        mount(holders.result, previewCard(result));
        openConfirm(result);
      } else {
        mount(holders.result, resultCard(result));
        pushHistory({ command: result.command, tier: result.tier, outcome: "sent" });
      }
    } catch (error) {
      const tier = error?.detail?.tier || "deny";
      mount(holders.result, errorState(error, { onRetry: send }));
      pushHistory({ command, tier, outcome: error instanceof ApiError ? error.code : "error" });
      if (error instanceof ApiError && error.kind === "refused") {
        mount(holders.badge, tierLine({
          tier: "deny", reason: error.message, note: error.detail?.note,
          pointer: error.detail?.pointer,
        }));
      }
    } finally {
      sendButton.disabled = false;
    }
  }

  function previewCard(result) {
    return h("div", null,
      tierPill("confirm"),
      h("div.wirerequest", null,
        h("div.eyebrow", { text: "exact bytes (nothing sent yet)" }),
        h("code.mono.code-line", { text: result.request })),
      h("p.sub", { text: escapeControls(result.note || result.reason || "") }),
      h("p.sub", { text: `Token expires in ${result.expires_in_s || 120}s. The bytes are held server-side.` }),
      h("button.btn.btn--danger", { type: "button", text: "Confirm and send",
        disabled: writesEnabled ? undefined : true, onClick: () => openConfirm(result) }));
  }

  function openConfirm(result) {
    typedConfirm({
      title: "Send this raw command?",
      request: result.request,
      module: result.acknowledgement,
      warning: result.warning || "The exact bytes go to the adapter and cannot be undone.",
      hint: `Type the exact bytes (${result.acknowledgement}) to send them.`,
      onConfirm: (close) => { close(); runConfirm(result); },
    });
  }

  async function runConfirm(result) {
    mount(holders.result, loadingState("Sending the confirmed bytes…"));
    try {
      const response = await api.rawConfirm({
        confirm_token: result.confirm_token, acknowledgement: result.acknowledgement,
      });
      mount(holders.result, resultCard(response));
      pushHistory({ command: response.command, tier: response.tier, outcome: "sent" });
      toast(`Sent ${response.command}`, "ok");
    } catch (error) {
      mount(holders.result, errorState(error));
      pushHistory({ command: result.command, tier: "confirm",
        outcome: error instanceof ApiError ? error.code : "error" });
      toast(error instanceof ApiError ? `${error.code}: ${error.message}` : String(error), "err", 0);
    }
  }

  function resultCard(result) {
    const frames = result.frames || [];
    const audit = result.audit;
    return h("div", null,
      h("div.row", null, tierPill(result.tier),
        pill(result.sent ? "sent" : "not sent", result.sent ? "ok" : "idle")),
      h("pre.code-block", {
        text: (result.lines || []).map((line) => escapeControls(line)).join("\n") || "(no lines)",
      }),
      frames.length
        ? dataTable({
          columns: [{ key: "response_id", label: "Response", mono: true },
                    { key: "uds", label: "UDS payload", mono: true }],
          rows: frames, empty: "no frames",
        })
        : h("p.sub", { text: "No framed UDS response (an AT command, or the module did not answer)." }),
      audit ? h("p.sub", { text: audit.stored
        ? `Audit event written (session ${audit.session_id}).`
        : "Persistence is disabled, so this send was not written to the audit log." }) : null);
  }

  /* -------------------------------------------------------------- history */
  function pushHistory(entry) {
    history.unshift({ ...entry, at: new Date() });
    renderHistory();
  }

  function renderHistory() {
    if (!history.length) {
      mount(holders.history, emptyState({ icon: "terminal", title: "No commands sent in this session",
        hint: "Every command you send is listed here with its tier and outcome." }));
      return;
    }
    mount(holders.history, dataTable({
      columns: [
        { key: "at", label: "Time", render: (row) => row.at.toLocaleTimeString([], { hour12: false }) },
        { key: "command", label: "Command", mono: true },
        { key: "tier", label: "Tier", render: (row) => tierPill(row.tier) },
        { key: "outcome", label: "Outcome", mono: true },
      ],
      rows: history, empty: "no commands",
    }));
  }

  /* ------------------------------------------------------------ allowlist */
  async function loadAllowlist() {
    mount(holders.allowlist, loadingState("Loading the command tiers…"));
    try {
      const payload = await api.rawAllowlist();
      mount(holders.allowlist, allowlistPanel(payload));
    } catch (error) {
      mount(holders.allowlist, errorState(error, { onRetry: loadAllowlist }));
    }
  }

  function allowlistPanel(payload) {
    const read = payload.read || {};
    const deny = payload.deny || {};
    return h("div.stack", null,
      h("div", null,
        h("div.row", null, tierPill("read"), h("span.sub", { text: "allowed with no confirmation" })),
        h("p.sub", null, codeList((read.obd_modes || []).map((row) => `${row.code} ${row.label}`))),
        h("p.sub", null, codeList((read.uds_services || []).map((row) => `0x${row.code} ${row.label}`))),
        h("p.sub", null, "AT: ", codeList([...(read.at_exact || []), ...(read.at_argument || [])]))),
      h("div", null,
        h("div.row", null, tierPill("confirm"), h("span.sub", { text: "typed acknowledgement + confirm token" })),
        h("p.sub", { text: (payload.confirm || {}).rule || "" })),
      h("div", null,
        h("div.row", null, tierPill("deny"), h("span.sub", { text: "never sent, in any mode" })),
        denyTable(deny)));
  }

  // A list of wire tokens, set in mono so a hex code reads as data, not prose.
  function codeList(items) {
    const wrap = h("span");
    items.forEach((item, index) => {
      if (index) wrap.appendChild(h("span.sub", { text: " · " }));
      wrap.appendChild(h("code.mono", { text: item }));
    });
    return wrap;
  }

  function denyTable(deny) {
    const rows = [
      ...(deny.obd_modes || []).map((row) => ({ item: `mode ${row.code}`, reason: row.reason })),
      ...(deny.uds_services || []).map((row) => ({ item: `0x${row.code}`, reason: row.reason })),
      ...(deny.at_exact || []).map((row) => ({ item: row.command, reason: row.reason })),
      ...(deny.at_prefixes || []).map((row) => ({ item: `${row.prefix}…`, reason: row.reason })),
    ];
    return dataTable({
      columns: [{ key: "item", label: "Denied", mono: true },
                { key: "reason", label: "Reason" }],
      rows, empty: "none",
    });
  }
}
