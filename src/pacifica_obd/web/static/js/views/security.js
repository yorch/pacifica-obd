// Security (0x27) — #/security
//
// Explain → read seed → preview the key (wire bytes shown) → typed confirm →
// send. The server holds one seed under a 30 s lease; the page never causes a
// second seed request unless the operator turned on strict seed re-read.

import { h, mount, on } from "../dom.js";
import { api, ApiError } from "../api.js";
import {
  card, pill, banner, errorState, toast, dataTable, typedConfirm, field, keyValues, loadingState,
} from "../components.js";

const NUM = (value) => (value === null || value === undefined ? "—" : String(value));

export function render(root, ctx) {
  const params = ctx.params || new URLSearchParams();
  const [health, status] = [ctx.status?.health, ctx.status?.status];
  const writesEnabled = health ? !health.read_only : false;

  const hold = {
    explain: h("p.sub", { text: "Offline: picks the algorithm for this target from the bundled selector table. Reads nothing from the vehicle." }),
    seed: h("p.sub", { text: "Reads the seed (0x27 <level>): a session request and the seed request go to the module. No key is sent." }),
    preview: h("p.sub", { text: "Reads the seed, computes the key and shows the exact wire bytes. Still nothing sent." }),
    result: h("p.sub", { text: "The reviewed key is sent here, after a typed confirmation." }),
  };

  const fields = {
    module: input("module", "Module request ID", "744", params.get("module") || "744"),
    level: input("level", "Level (odd)", "1", params.get("level") || "1", "number"),
    device: input("device-id", "Device ID (optional)", "11871", params.get("device_id") || ""),
    variant: input("variant", "Variant (optional)", "64",
      params.has("variant") ? (params.get("variant") || "") : "64"),
    supplier: input("supplier", "Supplier (optional)", "00C3", params.get("supplier") || ""),
    timeout: input("timeout", "Timeout (ms)", "100", params.get("timeout_ms") || "100", "number"),
  };

  const head = h("div.view__head", null,
    h("div", null, h("h1", { text: "Security access 0x27" }),
      h("div.sub", { text: "One seed, held under a short lease; a key is computed and shown before it is sent. A wrong key can lock a module." })));

  mount(root, head,
    banner(writesEnabled
      ? "Writes enabled. Explain and seed are always read-only; the send step needs a typed confirmation."
      : "Read-only: explain and seed work; the send step is disabled on this server.", writesEnabled ? "warn" : "info"),
    card({ eyebrow: "Target", title: "Explain the algorithm choice (offline)", actions:
      h("button.btn.btn--sm", { type: "button", text: "Explain", onClick: explain }),
      body: h("div.stack", null, form(Object.values(fields)), hold.explain) }),
    card({ eyebrow: "Read", title: "Read the seed (no key is sent)", actions:
      h("button.btn.btn--sm", { type: "button", text: "Read seed", onClick: readSeed }),
      body: hold.seed }),
    card({ eyebrow: "Review", title: "Compute the key and show the wire bytes", actions:
      h("button.btn.btn--sm", { type: "button", text: "Preview key", onClick: preview }),
      body: hold.preview }),
    card({ eyebrow: "Send", title: "Send the reviewed key", body: hold.result }));

  if (params.get("auto") === "explain") explain();
  if (params.get("auto") === "preview") preview();

  return { dispose() {}, resize() {} };

  /* ---------------------------------------------------------------- forms */
  function form(entries) {
    return h("div.grid.grid--form", null, entries.map((field) => field.node));
  }

  function input(id, label, placeholder, value, type = "text") {
    const node = field({ id: `sec-${id}`, label, value: value || "", placeholder, mono: true, type });
    return { id, node, get: () => node.querySelector("input").value.trim() };
  }

  function target() {
    const body = {
      module: fields.module.get(),
      level: Number(fields.level.get() || "1"),
      bus: status?.bus || "can-c",
      timeout_ms: Number(fields.timeout.get() || "100"),
    };
    const device = fields.device.get();
    const variant = fields.variant.get();
    const supplier = fields.supplier.get();
    if (device) body.device_id = device;
    if (variant) body.variant_id = variant;
    if (supplier) body.supplier = supplier;
    return body;
  }

  function note(holder, node) { mount(holder, node); }

  /* ------------------------------------------------------------- explain */
  async function explain() {
    note(hold.explain, loadingState("Explaining the algorithm choice…"));
    try {
      const body = target();
      delete body.bus;
      delete body.timeout_ms;
      const info = await api.securityExplain(body);
      note(hold.explain, explainCard(info));
    } catch (error) { note(hold.explain, errorState(error, { onRetry: explain })); }
  }

  function explainCard(info) {
    const status = info.status;
    if (status === "resolved") {
      return h("div", null,
        pill("resolved", "ok"),
        keyValues([
          ["ECU", info.ecu], ["Request ID", info.request_id], ["Algorithm", info.algorithm],
        ]));
    }
    const rows = (info.candidates || []).map((candidate) => ({
      algorithm: candidate.algorithm,
      selectors: (candidate.selectors || []).map((s) => `${s.field}=${s.value}`).join(", "),
    }));
    return h("div", null,
      pill(status, "warn"),
      h("p.sub", { text: info.reason || "no single algorithm could be chosen" }),
      rows.length ? dataTable({
        columns: [{ key: "algorithm", label: "Candidate algorithm", mono: true },
                  { key: "selectors", label: "Selectors", mono: true }],
        rows, empty: "no candidates",
      }) : null,
      h("p.sub", { text: "Refusing: no key is computed or sent while the choice is unresolved." }));
  }

  /* ---------------------------------------------------------------- seed */
  async function readSeed() {
    note(hold.seed, loadingState("Reading the seed (no key is sent)…"));
    try {
      const result = await api.securitySeed(target());
      note(hold.seed, resultCard(result));
    } catch (error) { note(hold.seed, errorState(error, { onRetry: readSeed })); }
  }

  /* ------------------------------------------------------------- preview */
  async function preview() {
    note(hold.preview, loadingState("Reading the seed and computing the key…"));
    try {
      const result = await api.securityPreview(target());
      note(hold.preview, previewCard(result));
    } catch (error) { note(hold.preview, errorState(error, { onRetry: preview })); }
  }

  function previewCard(result) {
    if (!result.confirm_token) return explainCard(result);
    const send = h("button.btn.btn--danger", {
      type: "button", text: "Confirm and send", disabled: !writesEnabled ? true : undefined,
      onClick: () => ask(result),
    });
    return h("div.stack", null,
      pill(result.status, "warn"),
      keyValues([
        ["Algorithm", result.algorithm], ["Seed", result.seed], ["Key", result.key],
      ]),
      h("div.wirerequest", null,
        h("div.eyebrow", { text: "exact wire request (nothing sent yet)" }),
        h("code.mono.code-line", { text: result.request })),
      h("p.sub", { text: `Token expires in ${result.expires_in_s || 30}s; the seed is held, not re-read.` }),
      h("div.row", null, send,
        h("button.btn.btn--ghost.btn--sm", { type: "button", text: "Re-read seed",
          onClick: readSeed })));
  }

  function ask(result) {
    typedConfirm({
      title: "Send the reviewed key?",
      request: result.request,
      module: result.module,
      warning: "A wrong key can lock the ECU. The Security Gateway usually blocks writes on 2018+ vehicles.",
      hint: result.strict
        ? `Type ${result.module} to send the key that was previewed. The seed is re-read first; if it changed, nothing is sent.`
        : `Type ${result.module} to send the key that was previewed. The held seed is not re-read — a wrong key can lock a module.`,
      onConfirm: (close) => { close(); send(result); },
    });
  }

  async function send(result) {
    note(hold.result, loadingState("Sending the reviewed key…"));
    try {
      const response = await api.securityUnlock({
        confirm_token: result.confirm_token, acknowledgement: result.module,
      });
      note(hold.result, unlockCard(response));
      toast(response.unlocked ? "Security access granted" : `Key result: ${response.status}`,
        response.unlocked ? "ok" : "warn");
    } catch (error) {
      const message = error instanceof ApiError ? `${error.code}: ${error.message}` : String(error);
      note(hold.result, errorState(error));
      toast(message, "err", 0);
    }
  }

  function unlockCard(result) {
    const ok = result.unlocked === true;
    return h("div", null,
      pill(result.status, ok ? "ok" : "warn"),
      keyValues([
        ["Module", result.module], ["Level", result.level], ["Unlocked", ok ? "yes" : "no"],
        ["Seed now", result.seed], ["Raw", result.raw],
        ["Strict re-read", result.re_read ? "yes" : "no"],
      ]),
      !ok && result.sent ? h("p.sub", { text: "The key was refused. The module is not left unlocked." }) : null);
  }

  // The seed-only card: no key was computed, so none is claimed.
  function resultCard(result) {
    return h("div", null,
      pill(result.status, result.status === "ok" || result.status === "no_key_provider" ? "warn" : "crit"),
      keyValues([
        ["Module", result.module], ["Seed", result.seed], ["Raw", result.raw],
        ["Note", "seed dumped only; no key was computed or sent"],
      ]));
  }
}
