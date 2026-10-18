// App shell: nav rail, top bar (connection + read-only + Stop), theme, router.
//
// Views are ES modules under ./views/ exporting `render(root, ctx)`. This file
// owns the only global state (the status snapshot) and re-renders the top bar.

import { h, clear, mount, on } from "./dom.js";
import { api, ingestTokenFromFragment, ApiError } from "./api.js";
import { navigate, onRoute, parseHash } from "./router.js";
import { toast, banner } from "./components.js";

const NAV = [
  ["READ", [
    { path: "/", label: "Dashboard", glyph: "▤" },
    { path: "/live", label: "Live data", glyph: "◉" },
    { path: "/modules", label: "Modules & DTCs", glyph: "⌗" },
    { path: "/dids", label: "DIDs / ID block", glyph: "⧉" },
  ]],
  ["DATA", [
    { path: "/capture", label: "Capture", glyph: "⤓" },
    { path: "/db", label: "Database", glyph: "▦" },
    { path: "/reports", label: "Reports", glyph: "⎙" },
  ]],
  ["SYSTEM", [
    { path: "/settings", label: "Settings", glyph: "⚙" },
    { path: "/help", label: "Help", glyph: "?" },
  ]],
];

const STAGE_TWO = [
  { label: "Security 0x27", glyph: "⚿" },
  { label: "Writes", glyph: "✎" },
  { label: "Assistant", glyph: "✦" },
];

const VIEWS = {
  "/": () => import("./views/dashboard.js"),
  "/live": () => import("./views/live.js"),
  "/modules": () => import("./views/modules.js"),
  "/dids": () => import("./views/dids.js"),
  "/capture": () => import("./views/capture.js"),
  "/db": () => import("./views/db.js"),
  "/reports": () => import("./views/reports.js"),
  "/settings": () => import("./views/settings.js"),
  "/help": () => import("./views/help.js"),
};

const state = {
  view: null,          // active view module ({dispose?})
  status: null,
  settings: { theme: localStorage.getItem("pacifica-obd.theme") || "system" },
};

/* ------------------------------------------------------------------- theme */

function applyTheme() {
  const choice = state.settings.theme;
  const system = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const resolved = choice === "system" ? system : choice;
  document.documentElement.dataset.theme = resolved;
  document.dispatchEvent(new CustomEvent("themechange", { detail: { theme: resolved } }));
  const button = document.getElementById("theme-toggle");
  if (button) button.textContent = `Theme: ${resolved}`;
}

function cycleTheme() {
  const order = ["system", "light", "dark"];
  const next = order[(order.indexOf(state.settings.theme) + 1) % order.length];
  state.settings.theme = next;
  localStorage.setItem("pacifica-obd.theme", next);
  applyTheme();
}

/* ---------------------------------------------------------------- nav rail */

function renderNav(activePath) {
  const groups = document.getElementById("rail-groups");
  clear(groups);
  for (const [title, items] of NAV) {
    groups.appendChild(h("nav.rail__group", { "aria-label": title },
      h("div.eyebrow", { text: title }),
      items.map((item) => h("a.rail__link", {
        href: `#${item.path}`,
        "aria-current": item.path === activePath ? "page" : null,
      },
        h("span.glyph", { text: item.glyph, "aria-hidden": "true" }),
        h("span", { text: item.label })))));
  }
  groups.appendChild(h("nav.rail__group", { "aria-label": "Stage 2 (not in this build)" },
    h("div.eyebrow", { text: "STAGE 2" }),
    STAGE_TWO.map((item) => h("span.rail__link", {
      "aria-disabled": "true", title: "not part of this read-only build",
    },
      h("span.glyph", { text: item.glyph, "aria-hidden": "true" }),
      h("span", { text: item.label })))));
}

/* -------------------------------------------------------- status/top bar */

function setBanner(nodes) {
  const region = document.getElementById("banners");
  clear(region);
  for (const node of [].concat(nodes || [])) if (node) region.appendChild(node);
}

function renderStatus(status, health) {
  const dot = document.getElementById("conn-dot");
  const port = document.getElementById("conn-port");
  const proto = document.getElementById("conn-proto");
  const bus = document.getElementById("conn-bus");
  const lock = document.getElementById("lock-pill");
  const stop = document.getElementById("stop-btn");
  const connect = document.getElementById("connect-btn");
  if (!status || !health) return;
  const adapter = health.adapter || {};
  const attached = adapter.attached || status.attached;
  const busy = status.job?.busy;
  dot.dataset.state = busy ? "busy" : attached ? (adapter.status && /no vehicle/i.test(adapter.status) ? "warn" : "ok") : "idle";
  port.textContent = adapter.port || "no adapter";
  proto.textContent = adapter.protocol ? `protocol ${adapter.protocol}` : "not connected";
  bus.textContent = status.bus || "can-c";
  lock.hidden = !busy;
  if (busy) lock.textContent = `busy: ${status.job.holder || "job"}`;
  stop.disabled = !busy;
  connect.textContent = attached ? "Disconnect" : "Connect";
  connect.dataset.action = attached ? "disconnect" : "connect";
}

async function refreshStatus() {
  try {
    const [health, status] = await Promise.all([api.health(), api.status()]);
    state.status = { health, status };
    renderStatus(status, health);
  } catch (error) {
    const dot = document.getElementById("conn-dot");
    if (dot) dot.dataset.state = "crit";
  }
}

/* ------------------------------------------------------------------ router */

async function renderView({ path, params }) {
  const loader = VIEWS[path];
  const root = document.getElementById("view");
  if (state.view?.dispose) { try { state.view.dispose(); } catch { /* ignore */ } }
  state.view = null;
  renderNav(path);
  if (!loader) {
    mount(root, h("div.card", null, h("div.card__body", null,
      h("h1", { text: "Not found" }),
      h("p.sub", { text: `No screen for ${path}.` }),
      h("a.btn", { href: "#/", text: "Back to the dashboard" }))));
    return;
  }
  mount(root, h("div", { "aria-busy": "true" }, h("span.sub", { text: "Loading…" })));
  try {
    const module = await loader();
    const result = await module.render(root, {
      navigate, params, refreshStatus,
      setBanner,
      get status() { return state.status; },
    });
    state.view = result || {};
  } catch (error) {
    mount(root, h("div.card", null, h("div.card__body", null,
      h("h1", { text: "Could not load this screen" }),
      h("p.sub", { text: String(error?.message || error) }))));
  }
}

function renderViewTitle(path) {
  document.title = `pacifica-obd · ${path}`;
}

/* --------------------------------------------------------------- top bar */

function wireTopBar() {
  on(document.getElementById("theme-toggle"), "click", cycleTheme);
  on(document.getElementById("menu-toggle"), "click", () => {
    document.getElementById("rail").classList.toggle("rail--open");
  });
  on(document.getElementById("stop-btn"), "click", async () => {
    try {
      const status = state.status?.status;
      const id = status?.job?.holder_id;
      if (id) { await api.cancelJob(id); toast("Stop requested", "warn"); }
    } catch (error) { toast(error.message, "err", 0); }
    refreshStatus();
  });
  on(document.getElementById("connect-btn"), "click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      if (button.dataset.action === "disconnect") await api.disconnect();
      else await api.connect({});
    } catch (error) {
      toast(error instanceof ApiError ? error.message : String(error), "err", 0);
    } finally {
      button.disabled = false;
      refreshStatus();
    }
  });
}

/* -------------------------------------------------------------------- boot */

async function boot() {
  ingestTokenFromFragment();
  applyTheme();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (state.settings.theme === "system") applyTheme();
  });
  wireTopBar();
  onRoute((route) => { renderView(route); renderViewTitle(route.path); });
  refreshStatus();
  setInterval(refreshStatus, 4000);
  window.addEventListener("resize", () => state.view?.resize?.());
}

boot();
