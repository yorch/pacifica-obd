// App shell: nav rail, top bar (connection + safety posture + Stop), theme,
// router. Views are ES modules under ./views/ exporting `render(root, ctx)`.
// This file owns the only global state (the status snapshot) and re-renders the
// chrome around the view.

import { h, clear, mount, on } from "./dom.js";
import { api, ingestTokenFromFragment, ApiError } from "./api.js";
import { navigate, onRoute, parseHash } from "./router.js";
import { toast, banner, icon, loadingRows } from "./components.js";

// Icons are names into components.js's SVG set: 16px, 1.5px stroke, currentColor.
const NAV = [
  ["READ", [
    { path: "/", label: "Dashboard", glyph: "table" },
    { path: "/live", label: "Live data", glyph: "activity" },
    { path: "/modules", label: "Modules & DTCs", glyph: "layers" },
    { path: "/dids", label: "DIDs / ID block", glyph: "tag" },
  ]],
  ["GUARDED", [
    { path: "/security", label: "Security 0x27", glyph: "lock", guarded: true },
    { path: "/writes", label: "Writes", glyph: "pencil", writes: true, guarded: true },
  ]],
  ["AI", [
    { path: "/assistant", label: "Assistant", glyph: "sparkles" },
  ]],
  ["DATA", [
    { path: "/battery", label: "HV battery", glyph: "gauge" },
    { path: "/capture", label: "Capture", glyph: "download" },
    { path: "/db", label: "Database", glyph: "database" },
    { path: "/reports", label: "Reports", glyph: "file" },
  ]],
  ["SYSTEM", [
    { path: "/raw", label: "Raw console", glyph: "terminal" },
    { path: "/settings", label: "Settings", glyph: "sliders" },
    { path: "/help", label: "Help", glyph: "help" },
  ]],
];

const VIEWS = {
  "/": () => import("./views/dashboard.js"),
  "/live": () => import("./views/live.js"),
  "/modules": () => import("./views/modules.js"),
  "/dids": () => import("./views/dids.js"),
  "/security": () => import("./views/security.js"),
  "/writes": () => import("./views/writes.js"),
  "/assistant": () => import("./views/assistant.js"),
  "/battery": () => import("./views/battery.js"),
  "/capture": () => import("./views/capture.js"),
  "/db": () => import("./views/db.js"),
  "/reports": () => import("./views/reports.js"),
  "/raw": () => import("./views/raw.js"),
  "/settings": () => import("./views/settings.js"),
  "/help": () => import("./views/help.js"),
};

// Screens that are read like prose, not like a data grid.
const PROSE = new Set(["/help", "/assistant", "/settings"]);

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
  const group = document.getElementById("theme-toggle");
  if (group) {
    for (const button of group.querySelectorAll("[data-theme-choice]")) {
      button.setAttribute("aria-pressed", button.dataset.themeChoice === choice ? "true" : "false");
    }
    group.title = `Theme: ${choice === "system" ? `system (${resolved})` : choice}`;
  }
}

function setTheme(choice, persist = true) {
  state.settings.theme = choice;
  if (persist) localStorage.setItem("pacifica-obd.theme", choice);
  applyTheme();
}

const THEMES = ["system", "light", "dark"];

/* ---------------------------------------------------------------- nav rail */

function renderNav(activePath) {
  const groups = document.getElementById("rail-groups");
  if (!groups) return;
  clear(groups);
  const writeEnabled = state.status?.health ? !state.status.health.read_only : false;
  for (const [title, items] of NAV) {
    const links = items
      .filter((item) => !item.writes || writeEnabled)
      .map((item) => h("a.rail__link", {
        href: `#${item.path}`,
        title: item.label,
        "aria-current": item.path === activePath ? "page" : null,
      },
        h("span.glyph", { "aria-hidden": "true" }, icon(item.glyph)),
        h("span.rail__label", { text: item.label }),
        item.guarded ? h("span.lockchip", null, icon("lock", { size: 12 })) : null));
    if (!links.length) continue;
    groups.appendChild(h("nav.rail__group", { "aria-label": title },
      h("div.eyebrow", { text: title }), links));
  }
}

/* -------------------------------------------------------- status/top bar */

function setBanner(nodes) {
  const region = document.getElementById("banners");
  if (!region) return;
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
    renderNav(parseHash().path);
    updateSafetyChrome(health);
  } catch {
    const dot = document.getElementById("conn-dot");
    if (dot) dot.dataset.state = "crit";
  }
}

// The rail subtitle, the top-bar posture badge and the banner region all state
// the actual safety posture, so a screenshot can never imply a write surface
// that is not enabled.
function updateSafetyChrome(health) {
  const writes = health ? !health.read_only : false;
  const posture = writes ? "writes" : "read-only";
  const subtitle = document.getElementById("rail-posture") || document.querySelector(".rail__title small");
  if (subtitle) {
    subtitle.textContent = writes ? "writes enabled" : "read-only";
    subtitle.dataset.posture = posture;
  }
  const badge = document.getElementById("posture-pill");
  if (badge) {
    badge.dataset.posture = posture;
    badge.title = writes
      ? "This server has the guarded write surface enabled: 0x27 unlock and 2E/31 writes need a typed confirmation."
      : "This server can read the vehicle, unlock nothing and write nothing.";
    const text = document.getElementById("posture-text");
    if (text) text.textContent = writes ? "writes enabled" : "read-only";
    const mark = document.getElementById("posture-mark");
    if (mark) mark.setAttribute("d", writes ? "M12 8.5v4.5M12 16h.01" : "M9 12l2 2 4-4");
  }
  const region = document.getElementById("safety-banner");
  if (!region) return;
  clear(region);
  region.appendChild(banner(
    writes
      ? "Write mode: 0x27 unlock and 2E/31 writes require a typed confirmation. The Security Gateway may still refuse; this tool cannot undo a change."
      : "Read-only: this server can read the vehicle, unlock nothing and write nothing. The assistant can read; it cannot write.",
    writes ? "warn" : "info"));
}

/* ------------------------------------------------------------------ router */

function findLabel(path) {
  for (const [, items] of NAV) {
    for (const item of items) if (item.path === path) return item.label;
  }
  return path === "/" ? "Dashboard" : path.replace(/^\//, "") || "Dashboard";
}

function mountPlaceholder(root, path) {
  root.dataset.entering = "";
  mount(root, h("div.card", null,
    h("div.card__head", null, h("h2", { text: "Loading" })),
    h("div.card__body", null, loadingRows(4))));
  const title = document.getElementById("page-title");
  if (title) title.textContent = findLabel(path);
  window.scrollTo({ top: 0 });
}

async function renderView({ path, params }) {
  const loader = VIEWS[path];
  const root = document.getElementById("view");
  // A deep link may pin the theme for this session without persisting it
  // (`#/live?theme=light`), which is what the screenshot harness uses.
  const deepTheme = params.get("theme");
  if (deepTheme && THEMES.includes(deepTheme)) setTheme(deepTheme, false);
  if (state.view?.dispose) { try { state.view.dispose(); } catch { /* ignore */ } }
  state.view = null;
  renderNav(path);
  mountPlaceholder(root, path);
  if (!loader) {
    mount(root, h("div.card", null, h("div.card__body", null,
      h("h1", { text: "Not found" }),
      h("p.sub", { text: `No screen for ${path}.` }),
      h("a.btn.btn--primary", { href: "#/", text: "Back to the dashboard" }))));
    return;
  }
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
    return;
  }
  root.classList.toggle("view--prose", PROSE.has(path));
  root.dataset.entering = "";
  root.addEventListener("animationend", () => { delete root.dataset.entering; }, { once: true });
}

function renderViewTitle(path) {
  const label = findLabel(path);
  document.title = `pacifica-obd · ${label}`;
  const title = document.getElementById("page-title");
  if (title) title.textContent = label;
}

/* --------------------------------------------------------------- top bar */

function wireTopBar() {
  const group = document.getElementById("theme-toggle");
  if (group) {
    for (const button of group.querySelectorAll("[data-theme-choice]")) {
      on(button, "click", () => setTheme(button.dataset.themeChoice));
    }
  }
  const menu = document.getElementById("menu-toggle");
  const rail = document.getElementById("rail");
  on(menu, "click", () => {
    const open = rail.classList.toggle("rail--open");
    menu.setAttribute("aria-expanded", open ? "true" : "false");
  });
  on(rail, "click", (event) => {
    if (event.target.closest(".rail__link") && rail.classList.contains("rail--open")) {
      rail.classList.remove("rail--open");
      menu.setAttribute("aria-expanded", "false");
    }
  });
  const skip = document.querySelector(".skip-link");
  on(skip, "click", (event) => {
    // The hash router owns the fragment, so the skip link must not navigate.
    event.preventDefault();
    document.getElementById("view")?.focus();
  });
  on(document.getElementById("stop-btn"), "click", async () => {
    try {
      const id = state.status?.status?.job?.holder_id;
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
  window.addEventListener("error", (event) => {
    setBanner(banner(`Script error: ${event.message || "unknown"}`, "err"));
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    setBanner(banner(`Script error: ${reason?.message || String(reason)}`, "err"));
  });
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
