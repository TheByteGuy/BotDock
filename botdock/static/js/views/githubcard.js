// Account → GitHub card: connected state, device-flow sign-in, personal access token, settings.
import { h, icon, setChildren } from "../dom.js";
import { api, post } from "../api.js";
import { busy, confirmDialog, toast, toastError, copyText, spinnerBlock } from "../ui.js";
import { getGithub, setGithubStatus, avatar, extLink } from "../github.js";

const NO_SAVE = { autocomplete: "off", spellcheck: false, autocapitalize: "off", autocorrect: "off",
  "data-lpignore": "true", "data-1p-ignore": "true", "data-bwignore": "true", "data-form-type": "other" };
const INTERVALS = [[30, "30 seconds"], [60, "1 minute"], [120, "2 minutes"], [300, "5 minutes"], [600, "10 minutes"],
  [900, "15 minutes"], [1800, "30 minutes"], [3600, "1 hour"]];
const NEW_APP_URL = "https://github.com/settings/applications/new";
const CLASSIC_TOKEN_URL = "https://github.com/settings/tokens/new?scopes=repo&description=BotDock";
const FINE_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new";

export function githubCard() {
  let alive = true;
  let gh = null;
  let pollTimer = 0;
  let tickTimer = 0;
  let editingClientId = false;
  let clientIdError = "";
  const body = h("div", { class: "gh-body" }, spinnerBlock("Checking GitHub…"));
  const el = h("section", { class: "card form-card gh-card", id: "github" },
    h("h2", { class: "section-title gh-title" }, icon("github"), h("span", null, "GitHub")),
    h("p", { class: "muted gh-intro" }, "Connect GitHub to create bots straight from your repositories (private ones too) and let BotDock pull new commits and restart them automatically."),
    body);

  function stopPolling() { clearTimeout(pollTimer); clearInterval(tickTimer); pollTimer = tickTimer = 0; }

  async function refresh() {
    try {
      gh = await getGithub(true);
      if (alive) render();
    } catch (e) {
      if (alive) setChildren(body, h("div", { class: "form-error" }, `GitHub status unavailable: ${e.message}`));
    }
  }

  function render() {
    stopPolling();
    setChildren(body, gh.connected ? connectedView() : signInView(), settingsView());
  }

  // ---------------------------------------------------------------- connected
  function connectedView() {
    const disc = h("button", { class: "btn", type: "button" }, icon("unlink"), h("span", { class: "btn-label" }, "Disconnect"));
    disc.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: "Disconnect GitHub?", danger: true, confirmText: "Disconnect",
        message: "BotDock forgets the token. Bots linked to repositories stay linked, but they can't check for or pull updates until you connect again." });
      if (!ok) return;
      await busy(disc, async () => {
        try { await api("/api/github", { method: "DELETE" }); toast("GitHub disconnected", "success"); await refresh(); }
        catch (e) { toastError(e, "Couldn't disconnect"); }
      });
    });
    return h("div", { class: "gh-account" },
      avatar(gh.avatar_url, gh.login, 48),
      h("div", { class: "gh-account-main" },
        h("div", { class: "gh-account-name" }, gh.name || gh.login, gh.name ? h("span", { class: "muted" }, ` @${gh.login}`) : null),
        h("div", { class: "muted small" },
          gh.method === "token" ? "Connected with a personal access token" : "Connected with GitHub sign-in",
          gh.rate_limit_remaining != null ? ` · ${gh.rate_limit_remaining.toLocaleString()} API requests left this hour` : "")),
      h("div", { class: "gh-account-actions" },
        Object.assign(extLink(`https://github.com/${encodeURIComponent(gh.login || "")}`, h("span", { class: "btn-label" }, "Profile"), icon("external")), { className: "btn btn-ghost" }),
        disc));
  }

  // ---------------------------------------------------------------- sign in
  function signInView() {
    return h("div", { class: "gh-methods" }, tokenMethod(), deviceMethod());
  }

  function deviceMethod() {
    const box = h("div", { class: "gh-method" },
      h("div", { class: "gh-method-head" }, h("h3", null, "Sign in with GitHub")),
      h("p", { class: "scope-note" }, icon("alert"), h("span", null, "Grants full ", h("code", null, "repo"),
        " scope: read and write access to every repository on your account, and any bot running in BotDock can read it.")));
    if (!gh.client_id || editingClientId) {
      box.append(clientIdHowTo());
      return box;
    }
    const start = h("button", { class: "btn btn-block", type: "button" }, icon("github"), h("span", { class: "btn-label" }, "Sign in with GitHub"));
    start.addEventListener("click", () => busy(start, startDeviceFlow));
    box.append(
      h("p", { class: "muted small" }, "You'll get a short code to enter on github.com — works from any device, nothing to type here."),
      start,
      h("p", { class: "faint small gh-clientid" }, "OAuth App ", h("span", { class: "mono" }, gh.client_id), " · ",
        h("button", { class: "linklike", type: "button", onclick: () => { editingClientId = true; render(); } }, "change")));
    return box;
  }

  function clientIdHowTo() {
    const input = h("input", { class: "input mono", type: "text", placeholder: "Ov23li… or Iv1.…", "aria-label": "OAuth App client ID", ...NO_SAVE,
      value: gh.client_id || "" });
    const err = h("div", { class: "field-error", role: "alert" }, clientIdError);
    clientIdError = "";
    const save = h("button", { class: "btn btn-primary", type: "submit" }, h("span", { class: "btn-label" }, "Save client ID"));
    const form = h("form", { class: "gh-clientid-form", novalidate: true, onsubmit: (e) => {
      e.preventDefault();
      const v = input.value.trim();
      if (!v) { err.textContent = "Paste the Client ID from your OAuth App."; input.focus(); return; }
      busy(save, async () => {
        try {
          gh = await api("/api/github/settings", { method: "PUT", json: { client_id: v } });
          setGithubStatus(gh);
          editingClientId = false;
          toast("Client ID saved — you can sign in now", "success");
          render();
        } catch (e2) { err.textContent = e2.message; }
      });
    } },
    h("div", { class: "inline-input" }, input, save),
    editingClientId ? h("button", { class: "linklike small", type: "button", onclick: () => { editingClientId = false; render(); } }, "Cancel") : null,
    err);
    return h("div", { class: "gh-howto" },
      h("p", { class: "small" }, "This needs a one-time GitHub OAuth App (free, takes a minute). BotDock never sees your GitHub password."),
      h("ol", { class: "steps" },
        h("li", null, "On GitHub open ", h("strong", null, "Settings → Developer settings → OAuth Apps → New OAuth App"), " (", extLink(NEW_APP_URL, "direct link", icon("external")), ")."),
        h("li", null, "Name it ", h("strong", null, "BotDock"), ". For ", h("em", null, "Homepage URL"), " and ", h("em", null, "Authorization callback URL"),
          " enter any URL, e.g. ", h("code", null, "http://localhost"), " — they aren't used."),
        h("li", null, "Tick ", h("strong", null, "Enable Device Flow"), " and click ", h("strong", null, "Register application"), "."),
        h("li", null, "Copy the ", h("strong", null, "Client ID"), " (no client secret needed) and paste it here:")),
      form,
      h("p", { class: "faint small" }, "The fine-grained token option needs no app and can be limited to your bot repos."));
  }

  async function startDeviceFlow() {
    let d;
    try { d = await post("/api/github/device/start"); } catch (e) {
      // 409 = no client ID; GitHub rejecting the ID comes back as 4xx/502 mentioning it
      if (e.status === 409 || /client.?id/i.test(e.message)) {
        editingClientId = true;
        clientIdError = e.message;
        render();
        body.querySelector(".gh-clientid-form input")?.focus();
      } else toastError(e, "Couldn't start sign-in");
      return;
    }
    if (!alive) return;
    deviceView(d);
  }

  function deviceView(d) {
    stopPolling();
    const expiresAt = Date.now() + (d.expires_in || 900) * 1000;
    const remaining = h("span", { class: "mono" });
    const statusLine = h("div", { class: "device-status", role: "status" });
    const copyBtn = h("button", { class: "btn", type: "button" }, icon("copy"), h("span", { class: "btn-label" }, "Copy code"));
    copyBtn.addEventListener("click", async () => {
      if (await copyText(d.user_code)) {
        toast("Code copied", "success", { timeout: 1500 });
        setChildren(copyBtn, icon("check"), h("span", { class: "btn-label" }, "Copied"));
      } else toast("Couldn't copy — type the code instead", "error");
    });
    const verify = d.verification_uri || "https://github.com/login/device";
    const open = extLink(verify, h("span", { class: "btn-label" }, `Open ${verify.replace(/^https?:\/\//, "")}`), icon("external"));
    open.className = "btn btn-primary";

    const waiting = () => setChildren(statusLine, h("span", { class: "spinner spinner-sm" }),
      h("span", null, "Waiting for you to approve on GitHub… "), h("span", { class: "faint" }, "code expires in ", remaining));
    waiting();

    const tick = () => {
      const s = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
      remaining.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      if (s <= 0) finish("expired");
    };
    tick();
    tickTimer = setInterval(tick, 1000);

    let failures = 0;
    const poll = async () => {
      if (!alive) return;
      let r;
      try { r = await post("/api/github/device/poll"); failures = 0; } catch (e) {
        if (e.status === 401) return;
        failures++;
        if (failures === 2) setChildren(statusLine, h("span", { class: "spinner spinner-sm" }), h("span", null, "Can't reach BotDock — still trying…"));
        pollTimer = setTimeout(poll, 4000);
        return;
      }
      if (!alive) return;
      if (failures === 0 && statusLine.textContent.startsWith("Can't")) waiting();
      if (r.status === "pending") { pollTimer = setTimeout(poll, 2000); return; }
      finish(r.status);
    };
    pollTimer = setTimeout(poll, 2000);

    async function finish(st) {
      stopPolling();
      if (!alive) return;
      if (st === "connected") {
        toast("GitHub connected", "success");
        await refresh();
        return;
      }
      const denied = st === "denied";
      setChildren(body, h("div", { class: "device-box device-ended" },
        h("div", { class: `device-ended-icon ${denied ? "danger" : "warn"}` }, icon("alert")),
        h("h3", null, denied ? "Access was denied on GitHub" : "The code expired"),
        h("p", { class: "muted" }, denied
          ? "Someone pressed Cancel on the GitHub authorization page. Nothing was connected."
          : "Codes are valid for 15 minutes. Get a fresh one and enter it on GitHub."),
        h("div", { class: "form-actions center" },
          h("button", { class: "btn", type: "button", onclick: () => render() }, "Back"),
          (() => { const b = h("button", { class: "btn btn-primary", type: "button" }, icon("refresh"), h("span", { class: "btn-label" }, denied ? "Try again" : "Get a new code"));
            b.addEventListener("click", () => busy(b, startDeviceFlow)); return b; })())), settingsView());
    }

    setChildren(body, h("div", { class: "device-box" },
      h("div", { class: "device-steps" },
        h("div", { class: "step" }, h("span", { class: "step-num" }, "1"), h("span", null, "Copy this code")),
        h("div", { class: "device-code", "aria-label": `Code ${d.user_code.split("").join(" ")}` }, d.user_code),
        h("div", { class: "device-actions" }, copyBtn),
        h("div", { class: "step" }, h("span", { class: "step-num" }, "2"), h("span", null, "Open GitHub, sign in if asked, and enter the code")),
        h("div", { class: "device-actions" }, open),
        h("div", { class: "step" }, h("span", { class: "step-num" }, "3"), h("span", null, "Approve BotDock — this page updates by itself"))),
      statusLine,
      h("div", { class: "form-actions center" },
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => render() }, "Cancel"))));
  }

  function tokenMethod() {
    const input = h("input", { class: "input mono secret-input-line masked", type: "text", placeholder: "ghp_… or github_pat_…", "aria-label": "Personal access token", ...NO_SAVE });
    const toggle = h("button", { class: "icon-btn secret-toggle", type: "button", "aria-label": "Show token", title: "Show token" }, icon("eye"));
    toggle.addEventListener("click", () => {
      const masked = input.classList.toggle("masked");
      setChildren(toggle, icon(masked ? "eye" : "eyeOff"));
      toggle.setAttribute("aria-label", masked ? "Show token" : "Hide token");
    });
    const err = h("div", { class: "field-error", role: "alert" });
    const go = h("button", { class: "btn btn-primary", type: "submit" }, h("span", { class: "btn-label" }, "Connect with token"));
    return h("form", { class: "gh-method", novalidate: true, autocomplete: "off", onsubmit: (e) => {
      e.preventDefault();
      const token = input.value.trim();
      input.value = "";                       // never keep the token around
      if (!token) { err.textContent = "Paste a token first."; input.focus(); return; }
      err.textContent = "";
      busy(go, async () => {
        try {
          gh = await post("/api/github/token", { token });
          setGithubStatus(gh);
          toast(`GitHub connected as ${gh.login}`, "success");
          render();
        } catch (e2) { err.textContent = e2.message; input.focus(); }
      });
    } },
    h("div", { class: "gh-method-head" }, h("span", { class: "gh-step-badge" }, "Recommended"), h("h3", null, "Fine-grained access token")),
    h("p", { class: "muted small" }, "Read-only access to just your bot repositories. Every bot running in BotDock can read the stored token, so give it as little as possible."),
    h("ol", { class: "steps" },
      h("li", null, "On GitHub open ", h("strong", null, "Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token"),
        " (", extLink(FINE_TOKEN_URL, "direct link", icon("external")), "). Name it ", h("strong", null, "BotDock"), "."),
      h("li", null, "Repository access: ", h("strong", null, "Only select repositories"), " — pick the repos your bots live in."),
      h("li", null, "Permissions → Repository permissions → ", h("strong", null, "Contents: Read-only"), " (Metadata read-only is added by itself)."),
      h("li", null, "Generate the token, copy it and paste it here:")),
    h("div", { class: "inline-input" }, h("div", { class: "secret-field grow" }, input, toggle), go),
    err,
    h("p", { class: "faint small" }, "Stored encrypted on this server; revoke it on GitHub any time. A ", extLink(CLASSIC_TOKEN_URL, "classic token", icon("external")),
      " with the ", h("code", null, "repo"), " scope also works, but it can write to all your repositories."));
  }

  // ---------------------------------------------------------------- settings
  function settingsView() {
    const cur = gh.poll_interval_seconds || 120;
    const opts = INTERVALS.some(([v]) => v === cur) ? INTERVALS : [...INTERVALS, [cur, `${cur} seconds`]].sort((a, b) => a[0] - b[0]);
    const sel = h("select", { class: "input", id: "gh-interval" }, opts.map(([v, l]) => h("option", { value: String(v), selected: v === cur }, l)));
    sel.addEventListener("change", async () => {
      sel.disabled = true;
      try {
        gh = await api("/api/github/settings", { method: "PUT", json: { poll_interval_seconds: Number(sel.value) } });
        setGithubStatus(gh);
        toast("Update check interval saved", "success", { timeout: 2000 });
      } catch (e) { toastError(e, "Couldn't save"); sel.value = String(cur); }
      sel.disabled = false;
    });
    return h("div", { class: "gh-settings" },
      h("label", { class: "field", for: "gh-interval" }, h("span", { class: "field-label" }, "Check linked repos for new commits every"), sel,
        h("span", { class: "field-hint" }, "Checks are cheap conditional requests; bots with auto-update on pull and restart when their branch changes.")));
  }

  refresh();
  return {
    el,
    destroy() { alive = false; stopPolling(); },
  };
}
