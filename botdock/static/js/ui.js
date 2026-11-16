// Shared UI pieces: toasts, dialogs, menus, formatting, state pills, live tickers.
import { h, icon, clear } from "./dom.js";

// ---------------------------------------------------------------- formatting
export function fmtBytes(n) {
  if (n == null || isNaN(n)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
}

export function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return "–";
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), hr = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (d) return `${d}d ${hr}h`;
  if (hr) return `${hr}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

export function fmtRelative(iso) {
  if (!iso) return "–";
  const t = Date.parse(iso);
  if (isNaN(t)) return "–";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)} d ago`;
  return new Date(t).toLocaleDateString();
}

export function fmtDate(iso) {
  const t = Date.parse(iso);
  return isNaN(t) ? "–" : new Date(t).toLocaleString();
}

export function fmtCpu(p) { return p == null ? "–" : `${p < 10 ? p.toFixed(1) : Math.round(p)}%`; }

export function plural(n, one, many = one + "s") { return `${n.toLocaleString()} ${n === 1 ? one : many}`; }

// ---------------------------------------------------------------- toasts
export function toast(message, kind = "info", { detail, timeout } = {}) {
  const box = document.getElementById("toasts");
  const close = h("button", { class: "toast-close icon-btn", type: "button", "aria-label": "Dismiss" }, icon("x"));
  const el = h("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" },
    icon(kind === "error" ? "alert" : kind === "success" ? "check" : "info", "toast-icon"),
    h("div", { class: "toast-body" }, h("div", { class: "toast-msg" }, message), detail ? h("div", { class: "toast-detail" }, detail) : null),
    close);
  const remove = () => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 180);
  };
  close.addEventListener("click", remove);
  box.append(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  const t = setTimeout(remove, timeout ?? (kind === "error" ? 8000 : 4000));
  el.addEventListener("mouseenter", () => clearTimeout(t), { once: true });
  return remove;
}

export function toastError(err, prefix) {
  if (err && err.name === "AbortError") return;
  if (err && err.status === 401) return; // handled globally (back to login)
  const msg = (err && err.message) || String(err);
  toast(prefix ? `${prefix}: ${msg}` : msg, "error");
}

// ---------------------------------------------------------------- dialogs
/**
 * Opens a modal <dialog>. Escape and the close button close it (resolve(undefined)).
 * build(close) returns {body, actions} or a node; call close(value) to resolve.
 */
export function openDialog({ title, wide = false, build, onClose }) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: `dialog${wide ? " dialog-wide" : ""}`, "aria-labelledby": "dlg-title" });
    let done = false;
    const close = (value) => {
      if (done) return;
      done = true;
      if (onClose) onClose(value);
      if (dlg.open) dlg.close();
      dlg.remove();
      resolve(value);
    };
    const content = build(close);
    dlg.append(
      h("div", { class: "dialog-head" },
        h("h2", { id: "dlg-title", class: "dialog-title" }, title),
        h("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: () => close(undefined) }, icon("x"))),
      content);
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(undefined); });
    dlg.addEventListener("mousedown", (e) => { if (e.target === dlg) dlg.dataset.downOnBackdrop = "1"; });
    dlg.addEventListener("click", (e) => {
      if (e.target === dlg && dlg.dataset.downOnBackdrop) close(undefined);
      delete dlg.dataset.downOnBackdrop;
    });
    document.body.append(dlg);
    dlg.showModal();
    const auto = dlg.querySelector("[autofocus]") || dlg.querySelector(".dialog-body input, .dialog-body textarea, .dialog-body select");
    if (auto) auto.focus();
  });
}

/** Confirmation dialog -> Promise<boolean>. requireText: user must type it to enable confirm. */
export function confirmDialog({ title, message, confirmText = "Confirm", danger = false, requireText }) {
  return openDialog({
    title,
    build(close) {
      const ok = h("button", { class: `btn ${danger ? "btn-danger" : "btn-primary"}`, type: "submit" }, confirmText);
      let input = null;
      if (requireText) {
        ok.disabled = true;
        input = h("input", { class: "input", type: "text", autocomplete: "off", spellcheck: false, autofocus: true,
          "aria-label": "Type the name to confirm",
          oninput: () => { ok.disabled = input.value.trim() !== requireText; } });
      }
      const form = h("form", { class: "dialog-form", onsubmit: (e) => { e.preventDefault(); if (!ok.disabled) close(true); } },
        h("div", { class: "dialog-body" },
          typeof message === "string" ? h("p", null, message) : message,
          requireText ? h("label", { class: "field" },
            h("span", { class: "field-label" }, "Type ", h("strong", { class: "mono" }, requireText), " to confirm"), input) : null),
        h("div", { class: "dialog-actions" },
          h("button", { class: "btn", type: "button", onclick: () => close(false) }, "Cancel"), ok));
      if (!requireText) setTimeout(() => ok.focus());
      return form;
    },
  }).then((v) => v === true);
}

/** Single text-input prompt -> Promise<string|null>. */
export function promptDialog({ title, label, value = "", placeholder = "", confirmText = "Save", validate, mono = true }) {
  return openDialog({
    title,
    build(close) {
      const err = h("div", { class: "field-error", role: "alert" });
      const input = h("input", { class: `input${mono ? " mono" : ""}`, type: "text", value, placeholder, autocomplete: "off",
        spellcheck: false, autofocus: true });
      setTimeout(() => {
        // select the file stem, like OS file managers do
        const dot = value.lastIndexOf(".");
        const slash = value.lastIndexOf("/") + 1;
        input.setSelectionRange(slash, dot > slash ? dot : value.length);
      });
      return h("form", { class: "dialog-form", onsubmit: (e) => {
        e.preventDefault();
        const v = input.value.trim();
        const problem = validate ? validate(v) : (v ? null : "Required");
        if (problem) { err.textContent = problem; input.focus(); return; }
        close(v);
      } },
      h("div", { class: "dialog-body" }, h("label", { class: "field" }, h("span", { class: "field-label" }, label), input), err),
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "btn btn-primary", type: "submit" }, confirmText)));
    },
  }).then((v) => (typeof v === "string" ? v : null));
}

// ---------------------------------------------------------------- popover menu
let openMenu = null;
function closeMenu() {
  if (!openMenu) return;
  openMenu.el.remove();
  openMenu.anchor.setAttribute("aria-expanded", "false");
  document.removeEventListener("mousedown", openMenu.outside, true);
  document.removeEventListener("keydown", openMenu.key, true);
  window.removeEventListener("resize", closeMenu);
  window.removeEventListener("scroll", openMenu.scroll, true);
  const a = openMenu.anchor;
  openMenu = null;
  return a;
}

/** items: [{label, icon, danger, onSelect}] | "sep" */
export function showMenu(anchor, items) {
  if (openMenu && openMenu.anchor === anchor) { closeMenu(); return; }
  closeMenu();
  const el = h("div", { class: "menu", role: "menu" });
  for (const it of items) {
    if (it === "sep") { el.append(h("div", { class: "menu-sep", role: "separator" })); continue; }
    el.append(h("button", { class: `menu-item${it.danger ? " danger" : ""}`, type: "button", role: "menuitem",
      onclick: () => { closeMenu(); it.onSelect(); } }, it.icon ? icon(it.icon) : null, h("span", null, it.label)));
  }
  document.body.append(el);
  const r = anchor.getBoundingClientRect();
  const mw = el.offsetWidth, mh = el.offsetHeight;
  let left = Math.min(r.right - mw, window.innerWidth - mw - 8);
  left = Math.max(8, left);
  let top = r.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  anchor.setAttribute("aria-expanded", "true");
  const btns = [...el.querySelectorAll(".menu-item")];
  btns[0]?.focus();
  openMenu = {
    el, anchor,
    outside: (e) => { if (!el.contains(e.target) && !anchor.contains(e.target)) closeMenu(); },
    key: (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu()?.focus(); }
      else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const i = btns.indexOf(document.activeElement);
        const n = e.key === "ArrowDown" ? (i + 1) % btns.length : (i - 1 + btns.length) % btns.length;
        btns[n].focus();
      } else if (e.key === "Tab") closeMenu();
    },
    scroll: (e) => { if (!el.contains(e.target)) closeMenu(); },
  };
  document.addEventListener("mousedown", openMenu.outside, true);
  document.addEventListener("keydown", openMenu.key, true);
  window.addEventListener("resize", closeMenu);
  window.addEventListener("scroll", openMenu.scroll, true);
}
export { closeMenu };

// ---------------------------------------------------------------- buttons
export function btn(label, { cls = "", iconName, onclick, type = "button", title, attrs = {} } = {}) {
  return h("button", { class: `btn ${cls}`.trim(), type, title, onclick, ...attrs },
    iconName ? icon(iconName) : null, label ? h("span", { class: "btn-label" }, label) : null);
}

export function iconBtn(iconName, label, onclick, cls = "") {
  return h("button", { class: `icon-btn ${cls}`.trim(), type: "button", "aria-label": label, title: label, onclick }, icon(iconName));
}

/** Runs fn while the button shows a spinner and is disabled. */
export async function busy(button, fn) {
  if (button.classList.contains("is-busy")) return;
  const wasDisabled = button.disabled;
  button.classList.add("is-busy");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    return await fn();
  } finally {
    button.classList.remove("is-busy");
    button.removeAttribute("aria-busy");
    button.disabled = wasDisabled;
  }
}

// ---------------------------------------------------------------- copy
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through */ }
  // Plain-http LAN access has no async clipboard API; fall back to a throwaway textarea.
  const ta = h("textarea", { class: "sr-only", "aria-hidden": "true", readonly: "" });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.value = "";
  ta.remove();
  return ok;
}

// ---------------------------------------------------------------- live tickers
// Elements with data-tick="uptime|countdown|relative" and data-t=<iso> refresh once a second.
function tickAll() {
  const now = Date.now();
  for (const el of document.querySelectorAll("[data-tick]")) {
    const t = Date.parse(el.dataset.t);
    if (isNaN(t)) continue;
    const kind = el.dataset.tick;
    if (kind === "uptime") el.textContent = fmtDuration((now - t) / 1000);
    else if (kind === "countdown") el.textContent = `Restarting in ${Math.max(0, Math.ceil((t - now) / 1000))}s`;
    else if (kind === "relative") el.textContent = fmtRelative(el.dataset.t);
  }
}
setInterval(tickAll, 1000);

export function uptimeEl(iso) {
  return h("span", { dataset: { tick: "uptime", t: iso } }, fmtDuration((Date.now() - Date.parse(iso)) / 1000));
}

// ---------------------------------------------------------------- bot state
export const STATE_LABEL = {
  stopped: "Stopped", installing: "Installing", starting: "Starting", running: "Running",
  stopping: "Stopping", crashed: "Crashed",
};

/** Visual tone of a bot's runtime, including the backoff-restart case. */
export function stateTone(rt) {
  if (!rt) return "stopped";
  if (rt.state === "crashed" && rt.next_restart_at) return "backoff";
  return rt.state;
}

export function statePill(bot) {
  const rt = bot.runtime || {};
  const tone = stateTone(rt);
  const pill = h("span", { class: `pill pill-${tone}` });
  const transitional = ["installing", "starting", "stopping"].includes(tone);
  pill.append(transitional || tone === "backoff" ? h("span", { class: "spinner spinner-xs" }) : h("span", { class: "pill-dot" }));
  if (tone === "backoff") {
    const until = Date.parse(rt.next_restart_at);
    pill.append(h("span", { dataset: { tick: "countdown", t: rt.next_restart_at } },
      `Restarting in ${Math.max(0, Math.ceil((until - Date.now()) / 1000))}s`));
  } else {
    pill.append(h("span", null, tone === "installing" ? "Installing deps" : STATE_LABEL[rt.state] || rt.state || "Unknown"));
  }
  const tip = [];
  if (rt.last_error) tip.push(rt.last_error);
  if (rt.exit_code != null && rt.state !== "running") tip.push(`Last exit code ${rt.exit_code}`);
  if (rt.restart_count) tip.push(`${rt.restart_count} automatic restart${rt.restart_count === 1 ? "" : "s"}`);
  if (tip.length) pill.title = tip.join(" · ");
  return pill;
}

/** Which lifecycle actions make sense right now. */
export function allowedActions(bot) {
  const s = bot.runtime?.state || "stopped";
  const pending = s === "crashed" && !!bot.runtime?.next_restart_at;
  return {
    start: !!bot.entrypoint && (s === "stopped" || (s === "crashed" && !pending)),
    stop: s === "running" || s === "starting" || s === "installing" || pending,
    restart: !!bot.entrypoint && (s === "running" || s === "starting" || s === "crashed"),
    active: s === "running" || s === "starting" || s === "installing" || s === "stopping" || pending,
  };
}

export function emptyState({ iconName, title, text, action }) {
  return h("div", { class: "empty" },
    iconName ? h("div", { class: "empty-icon" }, icon(iconName)) : null,
    h("h3", null, title), text ? h("p", null, text) : null, action || null);
}

export function spinnerBlock(label = "Loading…") {
  return h("div", { class: "loading-block" }, h("span", { class: "spinner" }), h("span", null, label));
}

/**
 * Toasts for the .env part of an upload response. Existing secrets are never overwritten by an upload;
 * the backend lists those keys in skipped_env_keys (if it sends it).
 */
export function envImportToasts(res, botId) {
  const imported = (res && res.imported_env_keys) || [];
  const skipped = (res && res.skipped_env_keys) || [];
  if (imported.length) toast(`Imported ${plural(imported.length, "secret")} from .env`, "success", { detail: imported.join(", ") });
  if (skipped.length) {
    toast(`${plural(skipped.length, "secret")} from .env kept as they were`, "info", {
      timeout: 12000,
      detail: `${skipped.join(", ")} already exist, so the values in the uploaded .env were not used. Change them under Secrets if needed.`,
    });
  }
}

export { clear };
