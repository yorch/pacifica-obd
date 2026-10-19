// Guarded writes — #/writes
//
// write-did (0x2E) and routine (0x31), two-phase: preview (reads the current
// value, shows the exact bytes, returns a single-use token) → typed confirm →
// send. In read-only mode the whole screen explains that the routes are absent.

import { h, mount } from "../dom.js";
import { api, ApiError } from "../api.js";
import {
  card, pill, banner, errorState, toast, typedConfirm, emptyState, keyValues, loadingState, field, select,
} from "../components.js";

const SGW = "The Security Gateway usually blocks writes on 2018+ vehicles. "
  + "This tool cannot undo a write; record the original value first.";

export function render(root, ctx) {
  const params = ctx.params || new URLSearchParams();
  const health = ctx.status?.health;
  const writesEnabled = health ? !health.read_only : false;

  const holders = {
    writePreview: h("p.sub", { text: "Preview reads the current value and shows the exact bytes. Nothing is written yet." }),
    writeResult: h("div"),
    routinePreview: h("p.sub", { text: "Preview shows the exact 0x31 request. The routine runs only after a typed confirmation." }),
    routineResult: h("div"),
  };

  const write = {
    module: field_("w-module", "Module request ID", "744", params.get("module") || "744"),
    did: field_("w-did", "DID (4 hex)", "F190", params.get("did") || "F190"),
    data: field_("w-data", "Data (even-length hex)", "01", params.get("data") || "01"),
  };
  const routine = {
    module: field_("r-module", "Module request ID", "744", "744"),
    rid: field_("r-rid", "Routine ID (4 hex)", "0203", "0203"),
    sub: selectField("r-sub", "Sub-function", [
      ["1", "1 start"], ["2", "2 stop"], ["3", "3 request results"], ["0", "0 (unsigned)"]], "1"),
    data: field_("r-data", "Data (optional hex)", "", ""),
  };

  const writeCard = card({ eyebrow: "Service 2E", title: "Write data identifier", actions:
    h("button.btn.btn--sm", { type: "button", text: "Preview", onClick: previewWrite }),
    body: h("div.stack", null, h("div.grid.grid--form", null,
      [write.module.node, write.did.node, write.data.node]),
    holders.writePreview, holders.writeResult) });

  const routineCard = card({ eyebrow: "Service 31", title: "Routine control", actions:
    h("button.btn.btn--sm", { type: "button", text: "Preview", onClick: previewRoutine }),
    body: h("div.stack", null, h("div.grid.grid--form", null,
      [routine.module.node, routine.rid.node, routine.sub.node, routine.data.node]),
    holders.routinePreview, holders.routineResult) });

  mount(root,
    h("div.view__head", null,
      h("div", null, h("h1", { text: "Guarded writes" }),
        h("div.sub", { text: "0x2E write-data-by-identifier and 0x31 routine-control. Each is previewed, then confirmed by typing the module id." }))),
    banner(writesEnabled ? `Write mode. ${SGW}`
      : "Read-only: the write routes are not registered on this server.",
      writesEnabled ? "warn" : "info"));

  // In read-only mode the routes do not exist, so the screen shows a refusal
  // rather than two forms whose Preview button would only ever 404.
  if (writesEnabled) {
    root.appendChild(writeCard);
    root.appendChild(routineCard);
    if (params.get("auto") === "1") previewWrite();
    if (params.get("auto") === "routine") previewRoutine();
  } else {
    root.appendChild(card({
      eyebrow: "Writes", title: "Not available on this server",
      body: emptyState({
        icon: "ban",
        hint: "Restart the server with --allow-writes to register 0x2E and 0x31. Every write then needs a typed confirmation, and the Security Gateway may still refuse it.",
      }),
    }));
  }

  return { dispose() {}, resize() {} };

  function field_(id, label, placeholder, value) {
    const node = field({ id, label, value: value || "", placeholder, mono: true });
    return { node, get: () => node.querySelector("input").value.trim() };
  }

  function selectField(id, label, options, value) {
    const node = select({ id, label, value, options: options.map(([optValue, optLabel]) => ({ value: optValue, label: optLabel })) });
    return { node, get: () => node.querySelector("select").value };
  }

  async function previewWrite() {
    mount(holders.writePreview, loadingState("Previewing the write (nothing is written)…"));
    try {
      const body = {
        module: write.module.get(), did: write.did.get(), data: write.data.get(),
        bus: "can-c", timeout_ms: 100,
      };
      const preview = await api.writeDidPreview(body);
      mount(holders.writePreview,
        previewCard(preview, () => confirmWrite(preview)));
      // Deterministic deep link for screenshots: open the typed confirm modal.
      if (params.get("confirm") === "1") confirmWrite(preview);
    } catch (error) { mount(holders.writePreview, errorState(error, { onRetry: previewWrite })); }
  }

  async function previewRoutine() {
    mount(holders.routinePreview, loadingState("Previewing the routine…"));
    try {
      const preview = await api.routinePreview({
        module: routine.module.get(), rid: routine.rid.get(),
        sub: Number(routine.sub.get()), data: routine.data.get(),
        bus: "can-c", timeout_ms: 100,
      });
      mount(holders.routinePreview,
        previewCard(preview, () => confirmRoutine(preview)));
    } catch (error) { mount(holders.routinePreview, errorState(error, { onRetry: previewRoutine })); }
  }

  function previewCard(preview, onConfirm) {
    const current = preview.current
      ? h("div", null, h("div.eyebrow", { text: "current value" }),
        h("code.mono.code-line", { text: preview.current }))
      : h("p.sub", { text: "The current value is not readable; record it by other means before writing." });
    return h("div.stack", null,
      pill(preview.service || "preview", "warn"),
      h("div.wirerequest", null, h("div.eyebrow", { text: "exact wire request" }),
        h("code.mono.code-line", { text: preview.request })),
      keyValues([["Module", preview.module], ["Bus", preview.bus], ["Response", preview.response_id]]),
      current,
      h("p.sub", { text: `Token expires in ${preview.expires_in_s || 120}s.` }),
      h("button.btn.btn--danger", { type: "button", text: "Confirm and send",
        disabled: writesEnabled ? undefined : true, onClick: onConfirm }));
  }

  function confirmWrite(preview) {
    typedConfirm({
      title: "Write this value?",
      request: preview.request, module: preview.module, warning: SGW,
      hint: `Type ${preview.module} to write ${preview.request}.`,
      onConfirm: (close) => { close(); runWrite(preview); },
    });
  }

  function confirmRoutine(preview) {
    typedConfirm({
      title: "Run this routine?",
      request: preview.request, module: preview.module, warning: SGW,
      hint: `Type ${preview.module} to send ${preview.request}.`,
      onConfirm: (close) => { close(); runRoutine(preview); },
    });
  }

  async function runWrite(preview) {
    mount(holders.writeResult, loadingState("Writing the value…"));
    try {
      const result = await api.writeDidConfirm({
        confirm_token: preview.confirm_token, acknowledgement: preview.module,
      });
      mount(holders.writeResult, resultCard(result));
      toast(`Write: ${result.status}`, result.status === "ok" ? "ok" : "warn");
    } catch (error) { fail(holders.writeResult, error); }
  }

  async function runRoutine(preview) {
    mount(holders.routineResult, loadingState("Running the routine…"));
    try {
      const result = await api.routineConfirm({
        confirm_token: preview.confirm_token, acknowledgement: preview.module,
      });
      mount(holders.routineResult, resultCard(result));
      toast(`Routine: ${result.status}`, result.status === "ok" ? "ok" : "warn");
    } catch (error) { fail(holders.routineResult, error); }
  }

  function resultCard(result) {
    const audit = result.audit || {};
    // Only claim the trail exists when the server said it was stored; auditing is
    // best-effort and a write must not be reported as recorded when it was not.
    const auditNote = audit.stored
      ? "The outcome was written to the local audit log."
      : "The local audit log did not record this outcome — note it down.";
    return h("div", null,
      pill(result.status, result.status === "ok" ? "ok" : "crit"),
      keyValues([["Module", result.module], ["Bus", result.bus],
        ["Request", result.request], ["Raw", result.raw]]),
      h("p.sub", { text: auditNote }));
  }

  function fail(holder, error) {
    mount(holder, errorState(error));
    toast(error instanceof ApiError ? `${error.code}: ${error.message}` : String(error), "err", 0);
  }
}
