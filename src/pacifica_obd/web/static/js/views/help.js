// Help (#/help) — the CLI↔screen map, the protocol glossary and what "no
// response" means. Fully offline.

import { h } from "../dom.js";
import { card, dataTable } from "../components.js";

const CLI_MAP = [
  ["ports", "#/settings", "serial port list (passive enumeration)"],
  ["info", "#/settings", "adapter identity + voltage"],
  ["vin", "#/", "VIN (mode 09 02, then UDS F190)"],
  ["dtc", "#/live", "generic stored/pending/permanent codes"],
  ["live / watch", "#/live", "live PIDs, streaming with a chart"],
  ["readiness", "#/", "readiness monitors in the pre-trip check"],
  ["freeze", "#/live", "freeze-frame snapshot"],
  ["scan", "#/", "VIN + DTC + PID summary"],
  ["modules", "#/modules", "UDS module discovery"],
  ["dtc-all", "#/modules", "DTCs from every responding module"],
  ["read-did", "#/dids", "arbitrary DID reads"],
  ["module-info", "#/dids", "the F186–F191 identification block"],
  ["capture", "#/capture", "full snapshot, stored"],
  ["monitor", "#/live", "UDS DID monitor stream"],
  ["history / show / vehicles", "#/db", "the database browser"],
  ["dump", "#/reports", "multi-format report"],
  ["module-map", "#/modules", "known module addresses (offline)"],
  ["security / write-did / routine", "#/security · #/writes", "guarded 0x27 unlock, 0x2E write and 0x31 routine"],
  ["assistant", "#/assistant", "read-only AI over the stored data (bring your own key)"],
];

const GLOSSARY = [
  ["ATCRA", "The adapter's receive-address filter. Reads point it at the responder so multi-frame replies complete. Manual flow control is never used on a read path."],
  ["ISO-TP", "ISO 15765-2 transport. A first frame is a full 8-byte classic-CAN frame; consecutive frames are sequence-checked."],
  ["SGW", "Security Gateway. On 2018+ vehicles it blocks writes; reads pass."],
  ["NRC", "Negative Response Code, e.g. 0x11 service-not-supported or 0x78 response-pending."],
  ["MIL", "Malfunction Indicator Lamp (the check-engine light)."],
  ["DID", "Data Identifier read with UDS service 0x22."],
  ["FTB", "Failure Type Byte — the sub-code that qualifies a DTC."],
];

const STATES = [
  ["ok", "The module answered with the expected positive response."],
  ["no response", "Nothing came back: wrong bus, ignition off, or the module does not serve the request."],
  ["negative", "The module refused (a negative response with an NRC)."],
  ["malformed", "Something came back that is not a valid response of the expected shape."],
  ["wrong responder", "A different module answered than the one addressed; the value is discarded."],
  ["partial", "A capture stopped mid-run; whatever was read is kept and labelled."],
];

export function render(root) {
  root.appendChild(h("div.view__head", null,
    h("div", null, h("h1", { text: "Help" }),
      h("div.sub", { text: "Every screen maps to a CLI command; the vocabulary is the CLI's, verbatim." }))));

  root.appendChild(card({ eyebrow: "Parity", title: "CLI ↔ screen map", flush: true,
    body: dataTable({
      columns: [
        { key: "0", label: "CLI", mono: true }, { key: "1", label: "Screen", mono: true },
        { key: "2", label: "What it does" },
      ],
      rows: CLI_MAP.map(([cli, screen, what]) => ({ 0: cli, 1: screen, 2: what })), empty: "none",
    }) }));

  root.appendChild(card({ eyebrow: "Glossary", title: "Protocol terms", flush: true,
    body: dataTable({
      columns: [{ key: "0", label: "Term", mono: true }, { key: "1", label: "Meaning" }],
      rows: GLOSSARY.map(([term, meaning]) => ({ 0: term, 1: meaning })), empty: "none",
    }) }));

  root.appendChild(card({ eyebrow: "Reading the results", title: "What the statuses mean", flush: true,
    body: dataTable({
      columns: [{ key: "0", label: "Status", mono: true }, { key: "1", label: "Meaning" }],
      rows: STATES.map(([state, meaning]) => ({ 0: state, 1: meaning })), empty: "none",
    }) }));

  root.appendChild(card({ eyebrow: "Streams", title: "Cancel and Stop",
    body: h("div", null,
      h("p.sub", { text: "One owner talks to the adapter at a time. Starting a second bus operation while a stream is running is refused (409 bus_busy) rather than interleaved." }),
      h("p.sub", { text: "Stop cancels the active job cooperatively — the same points the TUI checks its cancel flag. A partial scan keeps everything it read; a cancelled capture is reported as partial, never as a failure." }),
      h("p.sub", { text: "A live recording continues until you press Stop, even if you close the tab, so a log is never silently ended. Reconnect to the stream with the same id to resume." })) }));

  return { dispose() {}, resize() {} };
}
