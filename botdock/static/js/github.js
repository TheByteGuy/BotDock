// GitHub helpers shared by Account, New bot, Dashboard, bot header and Settings.
import { h, icon, setChildren } from "./dom.js";
import { api, enc } from "./api.js";
import { fmtRelative, spinnerBlock, plural, busy, toast, toastError, confirmDialog } from "./ui.js";
import { upsertBot, getBot } from "./store.js";

let status = null;          // last GET /api/github
let inflight = null;

/** GET /api/github, cached for the session; force=true refetches. */
export async function getGithub(force = false) {
  if (status && !force) return status;
  if (!inflight) inflight = api("/api/github").then((s) => { status = s; return s; }).finally(() => { inflight = null; });
  return inflight;
}
export function setGithubStatus(s) { status = s; }

export const shortSha = (sha) => (sha ? String(sha).slice(0, 7) : "");
export const isGithub = (bot) => bot && bot.source && bot.source.type === "github";
export const sourceLabel = (src) => `${src.repo}@${src.branch}${src.subdir ? ` /${src.subdir.replace(/^\/+|\/+$/g, "")}` : ""}`;

function ghPath(s) { return String(s).split("/").map(enc).join("/"); }
export function repoUrl(src) {
  const base = `https://github.com/${ghPath(src.repo)}`;
  return src.branch ? `${base}/tree/${ghPath(src.branch)}${src.subdir ? "/" + ghPath(src.subdir.replace(/^\/+|\/+$/g, "")) : ""}` : base;
}
export function commitUrl(src, sha) { return `https://github.com/${ghPath(src.repo)}/commit/${enc(sha)}`; }

/** External link that opens in a new tab without leaking the BotDock URL. */
export function extLink(href, ...children) {
  return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, ...children);
}

/** Avatar with initials underneath: if the image can't load (offline NAS) the initials stay. */
export function avatar(url, login, size = 40) {
  const initials = (login || "?").replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "?";
  const box = h("span", { class: "avatar", style: { width: `${size}px`, height: `${size}px`, fontSize: `${Math.round(size * 0.38)}px` }, "aria-hidden": "true" },
    h("span", { class: "avatar-initials" }, initials));
  if (url && /^https:\/\/avatars\.githubusercontent\.com\//.test(url)) {
    const img = h("img", { class: "avatar-img", alt: "", referrerpolicy: "no-referrer", decoding: "async", loading: "lazy",
      width: String(size), height: String(size) });
    img.addEventListener("error", () => img.remove());
    img.addEventListener("load", () => box.classList.add("loaded"));
    img.src = url;
    box.append(img);
  }
  return box;
}

/** Compact "GitHub mark + repo@branch" line for cards and headers. */
export function sourceMark(src, { link = false, chip = true } = {}) {
  const label = h("span", { class: "mono" }, sourceLabel(src));
  return h("span", { class: "source-mark", title: `GitHub: ${sourceLabel(src)}${src.auto_update ? " · auto-update on" : ""}` },
    icon("github"), link ? extLink(repoUrl(src), label) : label,
    chip && src.auto_update ? h("span", { class: "chip chip-ok", title: "Pulls and restarts automatically when the branch changes" }, "auto") : null);
}

/** Card shown wherever GitHub is needed but not connected. */
export function notConnected(text) {
  return h("div", { class: "gh-cta" },
    h("div", { class: "gh-cta-icon" }, icon("github")),
    h("div", { class: "gh-cta-body" },
      h("strong", null, "Connect your GitHub account"),
      h("p", { class: "muted" }, text || "Sign in once and BotDock can list your repositories, including private ones.")),
    h("a", { class: "btn btn-primary", href: "#/account/github" }, h("span", { class: "btn-label" }, "Connect GitHub")));
}

/**
 * Searchable repo list. onPick(repo) is called when a row is chosen.
 * Returns {el, focus()}.
 */
export function repoPicker({ onPick, selected = null }) {
  let q = "";
  let page = 1;
  let seq = 0;
  let ctrl = null;
  let repos = [];
  let current = selected;
  const search = h("input", { class: "input", type: "search", placeholder: "Search your repositories", "aria-label": "Search repositories",
    autocomplete: "off", spellcheck: false });
  const list = h("div", { class: "repo-list", role: "listbox", "aria-label": "Repositories" }, spinnerBlock("Loading repositories…"));
  const more = h("button", { class: "btn btn-sm repo-more", type: "button", hidden: true, onclick: () => load(page + 1) }, "Load more");

  function row(r) {
    const sel = current === r.full_name;
    return h("button", { class: `repo-row${sel ? " selected" : ""}`, type: "button", role: "option", "aria-selected": String(sel),
      dataset: { repo: r.full_name }, onclick: () => { current = r.full_name; render(); onPick(r); } },
    h("span", { class: "repo-row-main" },
      h("span", { class: "repo-name" }, h("span", { class: "repo-owner" }, `${r.owner}/`), h("strong", null, r.name)),
      r.private ? h("span", { class: "chip", title: "Private repository" }, icon("lock"), "Private") : null),
    r.description ? h("span", { class: "repo-desc" }, r.description) : null,
    h("span", { class: "repo-meta" },
      r.language ? h("span", { class: "repo-lang" }, h("span", { class: "lang-dot", dataset: { lang: r.language } }), r.language) : null,
      h("span", null, `Pushed ${fmtRelative(r.pushed_at)}`),
      h("span", { class: "faint" }, icon("branch"), r.default_branch)));
  }

  function render() {
    if (!repos.length) {
      setChildren(list, h("div", { class: "tree-empty" }, q ? `No repositories match “${q}”.` : "No repositories found on this account."));
      return;
    }
    setChildren(list, repos.map(row));
  }

  async function load(p = 1) {
    const my = ++seq;
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    if (p === 1) setChildren(list, spinnerBlock("Loading repositories…"));
    more.disabled = true;
    try {
      const d = await api(`/api/github/repos?q=${enc(q)}&page=${p}`, { signal: ctrl.signal });
      if (my !== seq) return;
      page = p;
      repos = p === 1 ? d.repos || [] : repos.concat(d.repos || []);
      render();
      more.hidden = !d.has_more;
    } catch (e) {
      if (e.name === "AbortError" || my !== seq) return;
      setChildren(list, h("div", { class: "tree-empty" }, h("p", null, e.message),
        h("button", { class: "btn btn-sm", type: "button", onclick: () => load(p) }, "Try again")));
    } finally { more.disabled = false; }
  }

  let t = 0;
  search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { q = search.value.trim(); load(1); }, 250); });
  load(1);
  return {
    el: h("div", { class: "repo-picker" }, h("div", { class: "search-box grow" }, icon("search"), search), list, more),
    focus: () => search.focus(),
    destroy: () => { clearTimeout(t); if (ctrl) ctrl.abort(); },
  };
}

/** <select> filled with a repo's branches. Returns {el, ready: Promise} . */
export function branchSelect(fullName, preferred, id) {
  const sel = h("select", { class: "input mono", id, disabled: true }, h("option", { value: preferred || "" }, preferred || "Loading…"));
  const [owner, repo] = fullName.split("/");
  const ready = api(`/api/github/repos/${enc(owner)}/${enc(repo)}/branches`).then((d) => {
    const branches = d.branches || [];
    const want = preferred && branches.includes(preferred) ? preferred : d.default_branch;
    setChildren(sel, branches.map((b) => h("option", { value: b, selected: b === want }, b === d.default_branch ? `${b} (default)` : b)));
    if (preferred && !branches.includes(preferred)) sel.prepend(h("option", { value: preferred, selected: true }, `${preferred} (not found)`));
    sel.disabled = false;
    return d;
  }).catch((e) => {
    setChildren(sel, h("option", { value: preferred || "" }, preferred || "Couldn't load branches"));
    sel.disabled = !preferred;
    throw e;
  });
  return { el: sel, ready };
}

export function commitLine(src) {
  if (!src.commit_sha) return h("span", { class: "muted" }, "Not pulled yet");
  return h("span", { class: "commit-line" },
    extLink(commitUrl(src, src.commit_sha), h("span", { class: "chip chip-mono", title: src.commit_sha }, icon("commit"), shortSha(src.commit_sha))),
    src.commit_message ? h("span", { class: "commit-msg", title: src.commit_message }, src.commit_message) : null,
    src.commit_date ? h("span", { class: "faint", dataset: { tick: "relative", t: src.commit_date } }, fmtRelative(src.commit_date)) : null);
}

async function sourceCall(botId, what, button) {
  const run = async () => {
    try {
      const d = await api(`/api/bots/${enc(botId)}/source/${what}`, { method: "POST" });
      if (d && d.bot) upsertBot(d.bot);
      return d && d.bot;
    } catch (e) { toastError(e, what === "sync" ? "Couldn't pull" : "Couldn't check GitHub"); return null; }
  };
  return button ? busy(button, run) : run();
}
/** Plain-words explanation of what the first pull does to a bot that already has files. */
export function firstPullMessage(src) {
  const keep = (src && src.keep_paths) || [];
  return h("div", { class: "confirm-body" },
    h("p", null, "This is the first pull from ", h("strong", { class: "mono" }, src ? src.repo : "the repository"), ". It replaces every file that is different in the repository. The old copies are saved in the bot's ",
      h("code", null, "backups/"), " folder."),
    h("p", null, "Files the repository doesn't have are left alone."),
    keep.length ? h("p", null, "Never overwritten: ", keep.map((k, i) => [i ? ", " : "", h("code", null, k)])) : null);
}

/** Pull now. A bot that was linked but never pulled asks first. Progress arrives via bot events + console. */
export async function syncSource(botId, button) {
  const src = getBot(botId)?.source;
  if (src && src.type === "github" && !src.commit_sha) {
    const ok = await confirmDialog({ title: "Pull from GitHub for the first time?", confirmText: "Pull and replace files", message: firstPullMessage(src) });
    if (!ok) return null;
  }
  const bot = await sourceCall(botId, "sync", button);
  if (bot && bot.source && !bot.source.last_error) toast("Pulling the latest commit — follow along in the console", "success");
  return bot;
}
/** Check now: asks GitHub for the branch head without pulling. */
export async function checkSource(botId, button) {
  const bot = await sourceCall(botId, "check", button);
  if (bot && bot.source && !bot.source.last_error) {
    toast(bot.source.update_available ? `New commit available: ${shortSha(bot.source.latest_sha)}` : "Already up to date", "success", { timeout: 2500 });
  }
  return bot;
}

/**
 * "Never overwrite" list: glob patterns a sync must not touch. Returns {el, get(), set(list)}.
 * onChange fires whenever the list changes.
 */
export function keepPathsEditor({ initial = [], onChange = () => {}, id = "keep-paths" } = {}) {
  let items = [...new Set((initial || []).filter(Boolean))];
  const SUGGEST = ["data/**", "config.py"];
  const chips = h("div", { class: "keep-chips", "aria-live": "polite" });
  const err = h("div", { class: "field-error", role: "alert" });
  const input = h("input", { class: "input input-sm mono", id, type: "text", placeholder: "e.g. data/** or settings.json", autocomplete: "off", spellcheck: false });
  const addBtn = h("button", { class: "btn btn-sm", type: "button" }, icon("plus"), h("span", { class: "btn-label" }, "Add"));
  const sugg = h("div", { class: "keep-suggest" });

  const problem = (p) => {
    if (p.startsWith("/") || /^[A-Za-z]:/.test(p)) return "Use a path relative to the bot folder.";
    if (p.split("/").includes("..")) return "Patterns can't contain “..”.";
    return null;
  };
  function add(raw) {
    const parts = String(raw).split(/[\n,]+/).map((s) => s.trim().replace(/\\/g, "/")).filter(Boolean);
    for (const p of parts) {
      const bad = problem(p);
      if (bad) { err.textContent = bad; return false; }
      if (!items.includes(p)) items.push(p);
    }
    err.textContent = "";
    render();
    onChange(items);
    return true;
  }
  function remove(p) { items = items.filter((x) => x !== p); render(); onChange(items); }
  function render() {
    setChildren(chips, items.length
      ? items.map((p) => h("span", { class: "keep-chip" }, h("span", { class: "mono" }, p),
        h("button", { class: "keep-chip-x", type: "button", "aria-label": `Remove ${p}`, title: "Remove", onclick: () => remove(p) }, icon("x"))))
      : h("span", { class: "faint small" }, "Nothing protected yet — every file can be updated from the repository."));
    setChildren(sugg, SUGGEST.filter((s) => !items.includes(s)).map((s) =>
      h("button", { class: "chip chip-btn", type: "button", onclick: () => add(s), title: `Never overwrite ${s}` }, icon("plus"), h("span", { class: "mono" }, s))));
  }
  const commit = () => { if (input.value.trim() && add(input.value)) input.value = ""; };
  addBtn.addEventListener("click", commit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } });
  input.addEventListener("paste", (e) => {
    const t = e.clipboardData && e.clipboardData.getData("text");
    if (t && /[\n,]/.test(t)) { e.preventDefault(); if (add(t)) input.value = ""; }
  });
  render();
  return {
    el: h("div", { class: "field keep-paths" },
      h("label", { class: "field-label", for: id }, "Never overwrite"),
      h("span", { class: "field-hint" }, "Files your bot changes while running (saved data, its own config). A pull never overwrites or deletes a local file that matches. ",
        h("code", null, "**"), " means everything inside a folder."),
      chips,
      h("div", { class: "inline-input keep-add" }, input, addBtn),
      h("div", { class: "keep-suggest-row" }, h("span", { class: "faint small" }, "Suggestions:"), sugg),
      err),
    /** Current list, including a pattern still typed in the box. */
    get() { if (input.value.trim()) commit(); return [...items]; },
    set(list) { items = [...new Set(list || [])]; render(); },
  };
}

export { plural };
