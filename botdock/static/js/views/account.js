// Account: change password, sign out, system information.
import { h, icon, setChildren } from "../dom.js";
import { api, post } from "../api.js";
import { busy, toast, toastError, fmtBytes, fmtDuration, spinnerBlock } from "../ui.js";
import { githubCard } from "./githubcard.js";

export function mount(root, params = {}) {
  let alive = true;
  const gh = githubCard();
  const sys = h("div", null, spinnerBlock("Loading…"));

  const cur = h("input", { class: "input", type: "password", id: "a-cur", autocomplete: "current-password", required: true });
  const pw = h("input", { class: "input", type: "password", id: "a-new", autocomplete: "new-password", required: true, minlength: "8" });
  const pw2 = h("input", { class: "input", type: "password", id: "a-new2", autocomplete: "new-password", required: true });
  const user = h("input", { type: "text", name: "username", value: "admin", autocomplete: "username", class: "sr-only", tabIndex: -1, "aria-hidden": "true", readOnly: true });
  const err = h("div", { class: "form-error", role: "alert", hidden: true });
  const saveBtn = h("button", { class: "btn btn-primary", type: "submit" }, h("span", { class: "btn-label" }, "Change password"));
  const showErr = (m) => { err.textContent = m; err.hidden = !m; };

  const pwForm = h("form", { class: "card form-card", novalidate: true, onsubmit: (e) => {
    e.preventDefault();
    showErr("");
    if (!cur.value) { showErr("Enter your current password."); cur.focus(); return; }
    if (pw.value.length < 8) { showErr("The new password needs at least 8 characters."); pw.focus(); return; }
    if (pw.value !== pw2.value) { showErr("The new passwords don't match."); pw2.focus(); return; }
    busy(saveBtn, async () => {
      try {
        await post("/api/auth/password", { current_password: cur.value, new_password: pw.value });
        cur.value = pw.value = pw2.value = "";
        toast("Password changed. Other devices have been signed out.", "success");
      } catch (e2) {
        showErr(e2.status === 401 || e2.status === 403 ? "The current password is wrong." : e2.message);
      }
    });
  } },
  h("h2", { class: "section-title" }, "Change password"),
  user,
  h("label", { class: "field", for: "a-cur" }, h("span", { class: "field-label" }, "Current password"), cur),
  h("div", { class: "form-grid" },
    h("label", { class: "field", for: "a-new" }, h("span", { class: "field-label" }, "New password"), pw, h("span", { class: "field-hint" }, "At least 8 characters.")),
    h("label", { class: "field", for: "a-new2" }, h("span", { class: "field-label" }, "Confirm new password"), pw2)),
  err,
  h("p", { class: "field-hint" }, "Changing the password signs out every other session."),
  h("div", { class: "form-actions" }, saveBtn));

  const logoutBtn = h("button", { class: "btn", type: "button", onclick: () => busy(logoutBtn, async () => {
    try { await post("/api/auth/logout"); } catch (e) { if (e.status !== 401) { toastError(e, "Couldn't sign out"); return; } }
    location.assign("/"); // full page load, so nothing renders with the dead session
  }) }, icon("logout"), h("span", { class: "btn-label" }, "Sign out"));

  root.append(h("div", { class: "page page-narrow" },
    h("div", { class: "page-head" }, h("div", null, h("h1", { class: "page-title" }, "Account"),
      h("p", { class: "page-sub" }, "One admin account protects this BotDock instance.")), logoutBtn),
    gh.el,
    pwForm,
    h("section", { class: "card form-card" }, h("h2", { class: "section-title" }, "System"), sys)));

  (async () => {
    try {
      const s = await api("/api/system");
      if (!alive) return;
      const total = s.disk?.total_bytes || 0, free = s.disk?.free_bytes || 0;
      const pct = total ? Math.round(((total - free) / total) * 100) : 0;
      const row = (k, v) => [h("dt", null, k), h("dd", null, v)];
      setChildren(sys, h("dl", { class: "kv" },
        row("BotDock", s.version),
        row("Python", s.python_version),
        row("Platform", h("span", { class: "mono" }, s.platform)),
        row("Data directory", h("span", { class: "mono" }, s.data_dir)),
        row("Bots", `${s.running_count} running of ${s.bot_count}`),
        row("Uptime", fmtDuration(s.uptime_seconds)),
        row("Disk", h("span", { class: "disk-line" },
          h("span", { class: "disk-bar wide" }, h("span", { class: `disk-fill${pct > 90 ? " high" : ""}`, style: { width: `${pct}%` } })),
          h("span", null, `${fmtBytes(free)} free of ${fmtBytes(total)} (${pct}% used)`)))));
    } catch (e) {
      if (!alive) return;
      setChildren(sys, h("p", { class: "muted" }, `System information is unavailable: ${e.message}`));
    }
  })();

  if (params.section === "github") requestAnimationFrame(() => gh.el.scrollIntoView({ block: "start" }));
  return {
    update(p) { if (p.section === "github") gh.el.scrollIntoView({ block: "start", behavior: "smooth" }); return true; },
    unmount() { alive = false; gh.destroy(); cur.value = pw.value = pw2.value = ""; },
  };
}
