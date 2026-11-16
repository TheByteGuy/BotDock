// First-run setup and login screens.
import { h, icon, logoMark, setChildren } from "../dom.js";
import { post } from "../api.js";
import { busy } from "../ui.js";

export function renderAuth(root, mode, onDone, theme, { codeRequired = false } = {}) {
  const setup = mode === "setup";
  const needCode = setup && codeRequired;
  // one-time setup code printed in BotDock's log (and in setup-code.txt) when no password was preset
  const code = needCode ? h("input", { class: "input mono setup-code", type: "text", id: "setup-code", name: "setup_code", required: true, autofocus: true,
    autocomplete: "one-time-code", spellcheck: false, autocapitalize: "off", "data-lpignore": "true", "data-1p-ignore": "true", "data-bwignore": "true" }) : null;
  const err = h("div", { class: "form-error", role: "alert", hidden: true });
  const showErr = (m) => { err.hidden = !m; err.textContent = m || ""; };

  const pw = h("input", { class: "input", type: "password", id: "pw", name: "password", required: true, autofocus: !needCode,
    autocomplete: setup ? "new-password" : "current-password", minlength: setup ? "8" : null });
  const pw2 = setup ? h("input", { class: "input", type: "password", id: "pw2", name: "confirm", required: true, autocomplete: "new-password" }) : null;
  // a hidden username lets password managers file the credential under something sensible
  const user = h("input", { type: "text", name: "username", value: "admin", autocomplete: "username", class: "sr-only", tabIndex: -1, "aria-hidden": "true", readOnly: true });
  const submit = h("button", { class: "btn btn-primary btn-block btn-lg", type: "submit" }, h("span", { class: "btn-label" }, setup ? "Create password & continue" : "Sign in"));

  const form = h("form", { class: "auth-form", novalidate: true, onsubmit: (e) => {
    e.preventDefault();
    showErr("");
    if (needCode && !code.value.trim()) { showErr("Enter the setup code from BotDock's log."); code.focus(); return; }
    if (setup) {
      if (pw.value.length < 8) { showErr("Use at least 8 characters."); pw.focus(); return; }
      if (pw.value !== pw2.value) { showErr("The passwords don't match."); pw2.focus(); return; }
    } else if (!pw.value) { showErr("Enter your password."); pw.focus(); return; }
    busy(submit, async () => {
      try {
        const body = { password: pw.value };
        if (needCode) body.setup_code = code.value.trim();
        await post(setup ? "/api/auth/setup" : "/api/auth/login", body);
        pw.value = "";
        if (pw2) pw2.value = "";
        onDone();
      } catch (e2) {
        if (e2.status === 409) { location.reload(); return; } // setup state changed meanwhile
        if (needCode && e2.status === 403) {
          showErr("That setup code isn't right. Copy it again from the log or from setup-code.txt.");
          code.select(); code.focus(); return;
        }
        if (e2.status === 429) { showErr(e2.message || "Too many attempts. Wait a few minutes and try again."); return; }
        showErr(e2.status === 401 ? "That password isn't right." : e2.message);
        pw.select();
        pw.focus();
      }
    });
  } },
  user,
  needCode ? h("label", { class: "field", for: "setup-code" }, h("span", { class: "field-label" }, "Setup code"), code,
    h("span", { class: "field-hint" }, "Proves you're the owner of this server. BotDock prints it in its log when it starts — on HexOS / TrueNAS open ",
      h("strong", null, "Apps → BotDock → Logs"), ". It's also in ", h("code", null, "setup-code.txt"), " in BotDock's data folder.")) : null,
  h("label", { class: "field", for: "pw" }, h("span", { class: "field-label" }, setup ? "Admin password" : "Password"), pw,
    setup ? h("span", { class: "field-hint" }, "At least 8 characters. This is the only account.") : null),
  pw2 ? h("label", { class: "field", for: "pw2" }, h("span", { class: "field-label" }, "Confirm password"), pw2) : null,
  err, submit);

  const themeBtn = h("button", { class: "icon-btn auth-theme", type: "button", "aria-label": "Toggle theme", title: "Toggle theme",
    onclick: () => { theme.setTheme(theme.getTheme() === "light" ? "dark" : "light"); setChildren(themeBtn, icon(theme.getTheme() === "light" ? "moon" : "sun")); } },
  icon(theme.getTheme() === "light" ? "moon" : "sun"));

  setChildren(root, h("div", { class: "auth" }, themeBtn,
    h("div", { class: "auth-card card" },
      h("div", { class: "auth-brand" }, logoMark(), h("span", { class: "brand-name" }, "BotDock")),
      h("h1", { class: "auth-title" }, setup ? "Welcome to BotDock" : "Sign in"),
      h("p", { class: "auth-sub" }, setup
        ? "Set an admin password to protect this dashboard. You'll use it to manage your bots from any device."
        : "Enter the admin password to manage your bots."),
      form),
    h("p", { class: "auth-foot" }, icon("lock"), h("span", null, setup ? "Stored as a salted scrypt hash on this server." : "Self-hosted · your bots, your server."))));
  (code || pw).focus();
}

/** Full-page notice when BotDock refuses the request (e.g. Host header not in BOTDOCK_ALLOWED_HOSTS). */
export function renderBlocked(root, message, theme) {
  setChildren(root, h("div", { class: "auth" },
    h("div", { class: "auth-card card blocked-card" },
      h("div", { class: "auth-brand" }, logoMark(), h("span", { class: "brand-name" }, "BotDock")),
      h("div", { class: "blocked-icon" }, icon("lock")),
      h("h1", { class: "auth-title" }, "BotDock refused this address"),
      h("p", { class: "auth-sub" }, message),
      h("p", { class: "muted small" }, "Open BotDock with the address it allows, or add this address (",
        h("code", null, location.host), ") to ", h("code", null, "BOTDOCK_ALLOWED_HOSTS"), " in the app's settings and restart it."),
      h("button", { class: "btn btn-block", type: "button", onclick: () => location.reload() }, icon("refresh"), h("span", { class: "btn-label" }, "Try again")))));
}
