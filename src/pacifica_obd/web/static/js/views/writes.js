// Guarded writes — #/writes
//
// write-did (0x2E) and routine (0x31), two-phase: preview (reads the current
// value, shows the exact bytes, returns a single-use token) → typed confirm →
// send. In read-only mode the whole screen explains that the routes are absent.

import { h, mount } from "../dom.js";
import { api, ApiError } from "../api.js";
import {
  card, pill, banner, errorState, toast, typedConfirm,
} from "../components.js";

const SGW = "The Security Gateway usually blocks writes on 2018+ vehicles. "
  + "This tool cannot undo a write; record the original value first.";

export function render(root, ctx) {
  const params = ctx.params || new URLSearchParams();
  const health = ctx.status?.health;
  const writesEnabled = health ? !health.read_only : false;

  const holders = {
    writePreview: h("div"), writeResult: h("div"),
    routinePreview: h("div"), routineResult: h("div"),
  };

  const write = {
    module: field("w-module", "Module request ID", "744", params.get("module") || "744"),
    did: field("w-did", "DID (4 hex)", "F190", params.get("did") || "F190"),
    data: field("w-data", "Data (even-length hex)", "01", params.get("data") || "01"),
  };
  const routine = {
    module: field("r-module", "Module request ID", "744", "744"),
    rid: field("r-rid", "Routine ID (4 hex)", "0203", "0203"),
    sub: selectField("r-sub", "Sub-function", [
      ["1", "1 start"], ["2", "2 stop"], ["3", "3 request results"], ["0", "0 (unsigned)"]], "1"),
    data: field("r-data", "Data (optional hex)", "", ""),
  };

  mount(root,
    h("div.view__head", null,
      h("div", null, h("h1", { text: "Guarded writes" }),
        h("div.sub", { text: "0x2E write-data-by-identifier and 0x31 routine-control. Each is previewed, then confirmed by typing the module id." }))),
    banner(writesEnabled ? `Write mode. ${SGW}`
      : "Read-only: the write routes are not registered on this server. Restart with --allow-writes to enable them.",
      writesEnabled ? "warn" : "info"),
    card({ eyebrow: "0x2E", title: "Write data identifier", actions:
      h("button.btn.btn--sm", { type: "button", text: "Preview", onClick: previewWrite }),
      body: h("div", null, form([write.module.node, write.did.node, write.data.node]),
        holders.writePreview, holders.writeResult) }),
    card({ eyebrow: "0x31", title: "Routine control", actions:
      h("button.btn.btn--sm", { type: "button", text: "Preview", onClick: previewRoutine }),
      body: h("div", null, form([routine.module.node, routine.rid.node, routine.sub.node,
        routine.data.node]), holders.routinePreview, holders.routineResult) }));

  if (params.get("auto") === "1") previewWrite();
  if (params.get("auto") === "routine") previewRoutine();

  return { dispose() {}, resize() {} };

  function field(id, label, placeholder, value) {
    const node = h("div.field", null, h("label", { for: id, text: label }),
      h("input.mono", { id, type: "text", placeholder, value: value || "" }));
    return { node, get: () => node.querySelector("input").value.trim() };
  }

  function selectField(id, label, options, value) {
    const node = h("div.field", null, h("label", { for: id, text: label }),
      h("select", { id }, options.map(([optValue, optLabel]) =>
        h("option", { value: optValue, text: optLabel, selected: optValue === value ? true : undefined }))));
    return { node, get: () => node.querySelector("select").value };
  }

  function form(nodes) { return h("div.grid.grid--form", null, nodes); }

  async function previewWrite() {
    mount(holders.writePreview, h("span.sub", { text: "Previewing…" }));
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
    mount(holders.routinePreview, h("span.sub", { text: "Previewing…" }));
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
      ? h("div", null, h("span.sub", { text: "current value" }),
        h("code.mono.code-line", { text: preview.current }))
      : h("p.sub", { text: "The current value is not readable; record it by other means before writing." });
    return h("div", null,
      pill(preview.service || "preview", "warn"),
      h("div.modal__request", null, h("div.eyebrow", { text: "exact wire request" }),
        h("code.mono.code-line", { text: preview.request })),
      h("dl.kv", null, [["Module", preview.module], ["Bus", preview.bus],
        ["Response", preview.response_id]]
        .flatMap(([key, value]) => [h("dt", { text: key }),
                                    h("dd.mono", { text: String(value ?? "—") })])),
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
    mount(holders.writeResult, h("span.sub", { text: "Writing…" }));
    try {
      const result = await api.writeDidConfirm({
        confirm_token: preview.confirm_token, acknowledgement: preview.module,
      });
      mount(holders.writeResult, resultCard("Write DID result", result));
      toast(`Write: ${result.status}`, result.status === "ok" ? "ok" : "warn");
    } catch (error) { fail(holders.writeResult, error); }
  }

  async function runRoutine(preview) {
    mount(holders.routineResult, h("span.sub", { text: "Running…" }));
    try {
      const result = await api.routineConfirm({
        confirm_token: preview.confirm_token, acknowledgement: preview.module,
      });
      mount(holders.routineResult, resultCard("Routine result", result));
      toast(`Routine: ${result.status}`, result.status === "ok" ? "ok" : "warn");
    } catch (error) { fail(holders.routineResult, error); }
  }

  function resultCard(title, result) {
    return h("div", null,
      pill(result.status, result.status === "ok" ? "ok" : "crit"),
      h("dl.kv", null, [["Module", result.module], ["Bus", result.bus],
        ["Request", result.request], ["Raw", result.raw]]
        .flatMap(([key, value]) => [h("dt", { text: key }),
                                    h("dd.mono", { text: String(value ?? "—") })])),
      h("p.sub", { text: "The outcome was written to the local audit log." }));
  }

  function fail(holder, error) {
    mount(holder, errorState(error));
    toast(error instanceof ApiError ? `${error.code}: ${error.message}` : String(error), "err", 0);
  }
}
