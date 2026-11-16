// BotDock SPA entry: auth gate, app shell, hash router with leave-guards.
import { h, icon, logoMark, setChildren, clear } from "./dom.js";
import { api, onUnauthorized } from "./api.js";
import { confirmDialog, closeMenu, toast } from "./ui.js";
import { startEvents, stopEvents, loadBots, onConnection } from "./store.js";
import { renderAuth, renderBlocked } from "./views/auth.js";
import * as dashboard from "./views/dashboard.js";
import * as newbot from "./views/newbot.js";
import * as botview from "./views/bot.js";
import * as account from "./views/account.js";

const app = document.getElementById("app");

const ROUTES = [
  { re: /^\/$/, view: dashboard, name: "dashboard", params: () => ({}) },
  { re: /^\/new\/?$/, view: newbot, name: "new", params: () => ({}) },
  { re: /^\/bots\/([^/]+)(?:\/(console|secrets|files|settings))?\/?$/, view: botview, name: "bot",
    params: (m) => ({ id: decodeURIComponent(m[1]), tab: m[2] || "console" }) },
  { re: /^\/account(?:\/(github))?\/?$/, view: account, name: "account", params: (m) => ({ section: m[1] || "" }) },
];

let shell = null;          // {root, main, nav}
let current = null;        // {name, instance}
let currentHash = "";
let guard = null;          // () => string|null : message when leaving would lose work
let authed = false;
let authTimer = 0;

// Safety net: open SSE streams don't notice a revoked session (password changed on another device),
// so re-check the session now and then while the tab is visible.
function startAuthWatch() {
  clearInterval(authTimer);
  authTimer = setInterval(async () => {
    if (!authed || document.visibilityState !== "visible") return;
    try {
      const st = await api("/api/auth/state");
      if (!st.authenticated) goToLogin();
    } catch { /* offline: the streams show their own reconnect state */ }
  }, 30000);
}

// ---------------------------------------------------------------- theme
function getTheme() { return document.documentElement.dataset.theme === "light" ? "light" : "dark"; }
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("botdock-theme", t); } catch { /* ignore */ }
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", t === "light" ? "#f5f6f8" : "#0d1016");
  updateThemeButton();
}
function updateThemeButton() {
  const b = document.getElementById("theme-toggle");
  if (!b) return;
  const light = getTheme() === "light";
  setChildren(b, icon(light ? "moon" : "sun"));
  b.setAttribute("aria-label", light ? "Switch to dark theme" : "Switch to light theme");
  b.title = b.getAttribute("aria-label");
}

// ---------------------------------------------------------------- guards
/** Views register a guard while they hold unsaved work. */
export function setGuard(fn) { guard = fn; }

window.addEventListener("beforeunload", (e) => {
  if (guard && guard()) { e.preventDefault(); e.returnValue = ""; }
});

// ---------------------------------------------------------------- shell
function buildShell() {
  const conn = h("span", { class: "conn", hidden: true, role: "status" });
  const navLink = (href, iconName, label, key) =>
    h("a", { class: "nav-link", href, dataset: { nav: key } }, icon(iconName), h("span", { class: "nav-label" }, label));
  const nav = h("nav", { class: "nav", "aria-label": "Main" },
    navLink("#/", "grid", "Bots", "bots"),
    navLink("#/account", "user", "Account", "account"));
  const main = h("main", { id: "main", class: "main", tabIndex: -1 });
  const root = h("div", { class: "shell" },
    h("a", { class: "skip-link", href: "#main", onclick: (e) => { e.preventDefault(); main.focus(); } }, "Skip to content"),
    h("header", { class: "topbar" },
      h("div", { class: "topbar-inner" },
        h("a", { class: "brand", href: "#/", "aria-label": "BotDock home" }, logoMark(), h("span", { class: "brand-name" }, "BotDock")),
        nav,
        h("div", { class: "topbar-right" }, conn,
          h("button", { id: "theme-toggle", class: "icon-btn", type: "button",
            onclick: () => setTheme(getTheme() === "light" ? "dark" : "light") })))),
    main);
  onConnection((s) => {
    conn.hidden = s === "live" || s === "idle" || s === "connecting";
    setChildren(conn, h("span", { class: "conn-dot" }), h("span", { class: "conn-label" }, "Reconnecting…"));
    conn.title = "Lost the live connection to BotDock; retrying";
  });
  return { root, main, nav };
}

function setActiveNav(name) {
  for (const a of shell.nav.querySelectorAll(".nav-link")) {
    const active = (a.dataset.nav === "bots" && (name === "dashboard" || name === "bot" || name === "new")) ||
      (a.dataset.nav === "account" && name === "account");
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

// ---------------------------------------------------------------- routing
function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const path = raw.startsWith("/") ? raw : "/" + raw;
  for (const r of ROUTES) {
    const m = path.match(r.re);
    if (m) return { route: r, params: r.params(m), path };
  }
  return null;
}

function unmountCurrent() {
  closeMenu();
  if (current && current.instance && current.instance.unmount) {
    try { current.instance.unmount(); } catch (e) { console.warn(e); }
  }
  current = null;
  guard = null;
}

function render() {
  if (!authed) return;
  const parsed = parseHash();
  if (!parsed) { location.replace("#/"); return; }
  currentHash = location.hash || "#/";
  const { route, params } = parsed;
  setActiveNav(route.name);
  if (current && current.name === route.name && current.instance.update && current.instance.update(params) !== false) return;
  unmountCurrent();
  clear(shell.main);
  shell.main.scrollTop = 0;
  window.scrollTo(0, 0);
  current = { name: route.name, instance: route.view.mount(shell.main, params, { setGuard, navigate }) || {} };
}

export function navigate(hash, { replace = false } = {}) {
  if (replace) { location.replace(hash); return; }
  if (location.hash === hash) render(); else location.hash = hash;
}

let restoring = false;
window.addEventListener("hashchange", async () => {
  if (restoring) { restoring = false; return; }
  if (!authed) return;
  const msg = guard && guard(location.hash);
  if (msg) {
    const target = location.hash;
    history.replaceState(null, "", currentHash);
    const ok = await confirmDialog({ title: "Discard unsaved changes?", message: msg, confirmText: "Discard", danger: true });
    if (!ok) return;
    guard = null;
    location.hash = target;
    return;
  }
  render();
});

// ---------------------------------------------------------------- auth flow
let kicking = false;
function goToLogin() {
  if (kicking || !authed) return;
  kicking = true;
  authed = false;
  clearInterval(authTimer);
  unmountCurrent();
  stopEvents();
  for (const d of document.querySelectorAll("dialog")) d.remove();
  toast("Your session has ended. Please sign in again.", "info");
  showAuth("login");
  kicking = false;
}
onUnauthorized(goToLogin);

let authState = {};
function showAuth(mode) {
  shell = null;
  app.className = "auth-root";
  renderAuth(app, mode, enterApp, { getTheme, setTheme }, { codeRequired: !!authState.setup_code_required });
}

async function enterApp() {
  authed = true;
  app.className = "";
  shell = buildShell();
  setChildren(app, shell.root);
  updateThemeButton();
  startEvents();
  startAuthWatch();
  loadBots().catch(() => { /* views show their own errors */ });
  render();
}

async function boot() {
  let state;
  for (let attempt = 0; ; attempt++) {
    try {
      state = await api("/api/auth/state");
      break;
    } catch (e) {
      if (e.status === 403) { // e.g. Host header not allowed: retrying won't help, explain instead
        app.className = "auth-root";
        renderBlocked(app, e.message, { getTheme, setTheme });
        return;
      }
      setChildren(app, h("div", { class: "boot-splash" }, h("span", { class: "spinner" }),
        h("span", null, `Can't reach BotDock — retrying… (${e.message})`)));
      await new Promise((r) => setTimeout(r, Math.min(10000, 1000 * (attempt + 1))));
    }
  }
  authState = state;
  if (state.setup_required) showAuth("setup");
  else if (!state.authenticated) showAuth("login");
  else enterApp();
}

boot();
