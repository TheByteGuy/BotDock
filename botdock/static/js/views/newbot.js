// New bot: pick/drop code -> review -> upload with progress -> (pick entrypoint) -> bot page.
import { h, icon, setChildren } from "../dom.js";
import { upload, enc, post } from "../api.js";
import { fmtBytes, plural, toast, toastError, busy, fmtRelative, spinnerBlock, envImportToasts } from "../ui.js";
import { dropZone, attachDrop, analyze, envKeys, suggestName, prettyName, toFormData, checkLimits, progressBar } from "../upload.js";
import { getGithub, repoPicker, branchSelect, notConnected, keepPathsEditor } from "../github.js";
import { upsertBot, patchBot, botAction, allBots } from "../store.js";

let pending = null;
/** The dashboard's empty-state drop zone hands its selection over to this screen. */
export function setPendingSelection(p) { pending = p; }

const SOURCE_KEY = "botdock-newbot-source";
function savedSource() { try { return localStorage.getItem(SOURCE_KEY) === "github" ? "github" : "upload"; } catch { return "upload"; } }

export function mount(root, _params, { navigate, setGuard }) {
  let alive = true;
  let xhr = null;
  let picker = null;
  let creating = false;
  let source = pending ? "upload" : savedSource();
  const stage = h("div", { class: "newbot-stage" });
  const srcTab = (id, iconName, label, hint) => h("button", { class: "source-tab", type: "button", role: "tab", dataset: { src: id },
    onclick: () => switchSource(id) }, h("span", { class: "source-tab-icon" }, icon(iconName)),
    h("span", { class: "source-tab-text" }, h("strong", null, label), h("span", null, hint)));
  const tabs = h("div", { class: "source-tabs", role: "tablist", "aria-label": "Where the code comes from" },
    srcTab("upload", "upload", "Upload", "A .py file, folder or .zip"),
    srcTab("github", "github", "From GitHub", "One of your repositories"));
  function markTabs() {
    for (const t of tabs.children) {
      const on = t.dataset.src === source;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", String(on));
    }
  }
  function switchSource(id) {
    if (xhr || creating || id === source) return;
    source = id;
    try { localStorage.setItem(SOURCE_KEY, id); } catch { /* ignore */ }
    markTabs();
    if (picker) { picker.destroy(); picker = null; }
    if (id === "github") githubStage(); else pickStage();
  }
  root.append(h("div", { class: "page page-narrow" },
    h("a", { class: "back-link", href: "#/" }, icon("chevronLeft"), h("span", null, "Bots")),
    h("div", { class: "page-head" }, h("div", null,
      h("h1", { class: "page-title" }, "New bot"),
      h("p", { class: "page-sub" }, "Upload code or pick a GitHub repository. BotDock creates a virtualenv and installs requirements.txt on first start."))),
    tabs, stage));
  markTabs();

  function pickStage() {
    setGuard(null);
    setChildren(stage, dropZone({
      title: "Drop your bot here",
      hint: "A .py file, a folder or a .zip · venv, __pycache__ and .git are skipped automatically",
      onSelect: handle,
    }));
  }

  async function handle(selPromise) {
    setChildren(stage, h("div", { class: "card loading-block" }, h("span", { class: "spinner" }), h("span", null, "Reading files…")));
    let sel;
    try { sel = await selPromise; } catch (e) {
      if (!alive) return;
      toast(e.message || "Couldn't read those files", "error");
      pickStage();
      return;
    }
    if (!alive) return;
    if (!sel.items.length) {
      toast("Nothing to upload: the selection was empty or only contained skipped folders.", "error");
      pickStage();
      return;
    }
    review(sel);
  }

  async function review(sel) {
    const info = analyze(sel);
    const keys = info.envItem ? await envKeys(info.envItem.file).catch(() => []) : [];
    if (!alive) return;
    const limitProblem = checkLimits(sel);

    // ---- summary
    const facts = [];
    const fact = (ok, text, tone) => facts.push(h("li", { class: `fact ${tone || (ok ? "ok" : "muted")}` }, icon(ok ? "check" : tone === "warn" ? "alert" : "info"), h("span", null, text)));
    if (sel.kind === "zip") fact(true, "Zip archive — it is extracted on the server; requirements.txt and .env inside are detected there.", "muted");
    else {
      if (info.hasRequirements) fact(true, "requirements.txt found — installed into the bot's own virtualenv");
      else fact(false, sel.kind === "py" ? "Single file — add packages later under Settings → Extra pip packages" : "No requirements.txt — add packages later in Settings if needed");
      if (info.envItem) fact(true, keys.length ? `.env found — ${plural(keys.length, "key")} will be imported as encrypted secrets: ${keys.join(", ")}` : ".env found — its variables will be imported as secrets");
      if (!info.topPy.length && sel.kind === "folder") fact(false, "No Python files at the top level of this folder", "warn");
    }
    if (sel.skippedFiles || sel.skippedDirs.size) {
      const dirs = [...sel.skippedDirs];
      fact(false, sel.skippedFiles
        ? `Skipped ${plural(sel.skippedFiles, "file")}${dirs.length ? ` in ${dirs.join(", ")}` : ""} (caches, virtualenvs, .git)`
        : `Skipped ${dirs.join(", ")} (caches, virtualenvs, .git)`);
    }
    if (limitProblem) fact(false, limitProblem, "warn");

    const kindIcon = sel.kind === "py" ? "fileCode" : sel.kind === "zip" ? "zip" : "folder";
    const fileList = sel.kind === "folder" ? h("details", { class: "file-preview" },
      h("summary", null, `Show ${plural(info.count, "file")}`),
      h("ul", { class: "mono" }, info.rel.slice(0, 300).map((p) => h("li", null, p)),
        info.rel.length > 300 ? h("li", { class: "faint" }, `…and ${(info.rel.length - 300).toLocaleString()} more`) : null)) : null;

    // ---- form
    const name = h("input", { class: "input", id: "nb-name", type: "text", required: true, maxlength: "60", value: suggestName(sel), autocomplete: "off", spellcheck: false });
    const desc = h("input", { class: "input", id: "nb-desc", type: "text", maxlength: "200", placeholder: "Optional — what does this bot do?", autocomplete: "off" });
    let entry;
    if (sel.kind === "folder" && info.topPy.length > 0) {
      entry = h("select", { class: "input mono", id: "nb-entry" },
        h("option", { value: "" }, info.entry ? `Auto-detect (${info.entry})` : "Auto-detect"),
        info.topPy.map((p) => h("option", { value: p }, p)));
    } else if (sel.kind === "py") {
      entry = h("input", { class: "input mono", id: "nb-entry", type: "text", value: info.entry, disabled: true });
    } else {
      entry = h("input", { class: "input mono", id: "nb-entry", type: "text", placeholder: "Auto-detect (main.py, bot.py, …)", autocomplete: "off", spellcheck: false });
    }
    const startNow = h("input", { type: "checkbox", id: "nb-start", checked: keys.length > 0 });
    const err = h("div", { class: "form-error", role: "alert", hidden: true });
    const bar = progressBar();
    bar.el.hidden = true;
    const submit = h("button", { class: "btn btn-primary", type: "submit", disabled: !!limitProblem }, icon("upload"), h("span", { class: "btn-label" }, "Create bot"));
    const cancel = h("button", { class: "btn", type: "button", onclick: () => { if (xhr) xhr.abort(); else pickStage(); } }, "Cancel");

    const form = h("form", { class: "card form-card", novalidate: true, onsubmit: (e) => { e.preventDefault(); create(); } },
      h("div", { class: "selection-head" },
        h("div", { class: "selection-icon" }, icon(kindIcon)),
        h("div", { class: "selection-main" },
          h("div", { class: "selection-name" }, sel.label),
          h("div", { class: "selection-meta" }, sel.kind === "zip" ? `Zip · ${fmtBytes(info.size)}` : `${plural(info.count, "file")} · ${fmtBytes(info.size)}`)),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { if (!xhr) pickStage(); } }, "Change")),
      h("ul", { class: "facts" }, facts),
      fileList,
      h("div", { class: "form-grid" },
        h("label", { class: "field", for: "nb-name" }, h("span", { class: "field-label" }, "Name"), name),
        h("label", { class: "field", for: "nb-entry" }, h("span", { class: "field-label" }, "Entrypoint"), entry,
          h("span", { class: "field-hint" }, "The file BotDock runs with python -u."))),
      h("label", { class: "field", for: "nb-desc" }, h("span", { class: "field-label" }, "Description"), desc),
      h("label", { class: "check" }, startNow, h("span", null, "Start the bot right after upload"),
        h("span", { class: "check-hint" }, keys.length ? "Secrets come from the .env file." : "Leave off if you still need to add a token under Secrets.")),
      err, bar.el,
      h("div", { class: "form-actions" }, cancel, submit));
    setChildren(stage, form);
    attachDrop(form, handle);
    name.focus();
    name.select();

    async function create() {
      err.hidden = true;
      const n = name.value.trim();
      if (!n) { showErr("Give the bot a name."); name.focus(); return; }
      if (allBots().some((b) => b.name.toLowerCase() === n.toLowerCase())) { showErr(`A bot named “${n}” already exists.`); name.focus(); return; }
      const fields = { name: n, description: desc.value.trim() };
      if (sel.kind !== "py" && entry.value.trim()) fields.entrypoint = entry.value.trim();
      bar.el.hidden = false;
      bar.set(0, 1);
      for (const el of form.querySelectorAll("input, select")) el.disabled = true;
      cancel.textContent = "Cancel upload";
      setGuard(() => "An upload is in progress. Leaving this page cancels it.");
      await busy(submit, async () => {
        xhr = upload("/api/bots", toFormData(sel, fields), (l, t) => bar.set(l, t));
        let res;
        try {
          res = await xhr.promise;
        } catch (e) {
          xhr = null;
          setGuard(null);
          if (!alive) return;
          for (const el of form.querySelectorAll("input, select")) el.disabled = el.id === "nb-entry" && sel.kind === "py";
          cancel.textContent = "Cancel";
          bar.el.hidden = true;
          if (e.status === -1) return;
          showErr(e.status === 413 ? `Too large: ${e.message}` : e.message);
          return;
        }
        xhr = null;
        setGuard(null);
        if (!alive) return;
        upsertBot(res.bot);
        envImportToasts(res);
        if (!res.bot.entrypoint) chooseEntrypoint(res, startNow.checked);
        else finish(res.bot, startNow.checked);
      });
    }
    function showErr(m) { err.textContent = m; err.hidden = false; }
  }

  function chooseEntrypoint(res, startAfter) {
    const bot = res.bot;
    const cands = res.detected_entrypoints || [];
    const err = h("div", { class: "form-error", role: "alert", hidden: true });
    let control;
    if (cands.length) {
      control = h("div", { class: "radio-list", role: "radiogroup", "aria-label": "Entrypoint" },
        cands.map((p, i) => h("label", { class: "radio-row" }, h("input", { type: "radio", name: "ep", value: p, checked: i === 0 }), h("span", { class: "mono" }, p))));
    } else {
      control = h("input", { class: "input mono", type: "text", placeholder: "e.g. src/bot.py", "aria-label": "Entrypoint path", autocomplete: "off", spellcheck: false });
    }
    const save = h("button", { class: "btn btn-primary", type: "submit" }, h("span", { class: "btn-label" }, "Use this file"));
    setChildren(stage, h("form", { class: "card form-card", onsubmit: (e) => {
      e.preventDefault();
      const v = cands.length ? (stage.querySelector('input[name="ep"]:checked') || {}).value : control.value.trim();
      if (!v) { err.textContent = "Choose a file."; err.hidden = false; return; }
      busy(save, async () => {
        try { finish(await patchBot(bot.id, { entrypoint: v }), startAfter); } catch (e2) { err.textContent = e2.message; err.hidden = false; }
      });
    } },
    h("div", { class: "selection-head" }, h("div", { class: "selection-icon ok" }, icon("check")),
      h("div", { class: "selection-main" }, h("div", { class: "selection-name" }, `“${bot.name}” was uploaded`),
        h("div", { class: "selection-meta" }, `${plural(bot.file_count || 0, "file")} · ${fmtBytes(bot.size_bytes)}`))),
    h("h2", { class: "section-title" }, "Which file starts your bot?"),
    h("p", { class: "muted" }, cands.length ? "BotDock couldn't tell for sure. Pick the script you'd normally run with python." : "No Python file was found at the top level. Enter the path of the script to run, relative to the bot folder."),
    control, err,
    h("div", { class: "form-actions" },
      h("a", { class: "btn", href: `#/bots/${enc(bot.id)}/settings` }, "Decide later"), save)));
  }

  async function finish(bot, startAfter) {
    if (startAfter) {
      try { await botAction(bot.id, "start"); } catch (e) { toastError(e, "Couldn't start"); }
    }
    if (!alive) return;
    if (!startAfter && !bot.env_count) toast(`“${bot.name}” is ready. Add its token under Secrets, then press Start.`, "success");
    else toast(`“${bot.name}” created`, "success");
    navigate(`#/bots/${enc(bot.id)}/${!startAfter && !bot.env_count ? "secrets" : "console"}`, { replace: true });
  }

  // ---------------------------------------------------------------- GitHub source
  async function githubStage() {
    setGuard(null);
    setChildren(stage, h("div", { class: "card" }, spinnerBlock("Checking GitHub…")));
    let gh;
    try { gh = await getGithub(true); } catch (e) {
      if (alive && source === "github") setChildren(stage, h("div", { class: "card form-card" }, h("div", { class: "form-error" }, e.message)));
      return;
    }
    if (!alive || source !== "github") return;
    if (!gh.connected) {
      setChildren(stage, h("div", { class: "card form-card" },
        notConnected("Sign in once on the Account page (a personal access token works too) and your repositories show up here.")));
      return;
    }
    pickRepo();
  }

  function pickRepo() {
    if (picker) picker.destroy();
    picker = repoPicker({ onPick: (r) => configureRepo(r) });
    setChildren(stage, h("div", { class: "card form-card" },
      h("h2", { class: "section-title" }, "Choose a repository"), picker.el));
    picker.focus();
  }

  function configureRepo(repo) {
    if (picker) { picker.destroy(); picker = null; }
    const name = h("input", { class: "input", id: "gh-name", type: "text", maxlength: "60", value: prettyName(repo.name), autocomplete: "off", spellcheck: false });
    const desc = h("input", { class: "input", id: "gh-desc", type: "text", maxlength: "200", value: (repo.description || "").slice(0, 200), placeholder: "Optional", autocomplete: "off" });
    const br = branchSelect(repo.full_name, repo.default_branch, "gh-branch");
    const subdir = h("input", { class: "input mono", id: "gh-subdir", type: "text", placeholder: "Repository root", autocomplete: "off", spellcheck: false });
    const entry = h("input", { class: "input mono", id: "gh-entry", type: "text", placeholder: "Auto-detect (main.py, bot.py, …)", autocomplete: "off", spellcheck: false });
    const auto = h("input", { type: "checkbox", id: "gh-auto", role: "switch", checked: true });
    const startNow = h("input", { type: "checkbox", id: "gh-start" });
    const keep = keepPathsEditor({ initial: [], id: "gh-keep" });
    const err = h("div", { class: "form-error", role: "alert", hidden: true });
    const status = h("div", { class: "device-status", hidden: true, role: "status" });
    const submit = h("button", { class: "btn btn-primary", type: "submit" }, icon("cloudDown"), h("span", { class: "btn-label" }, "Create bot"));
    function showErr(m) { err.textContent = m; err.hidden = false; }
    br.ready.catch((e) => showErr(`Couldn't load branches: ${e.message}`));
    const meta = [repo.private ? "Private" : "Public", repo.language, `pushed ${fmtRelative(repo.pushed_at)}`].filter(Boolean).join(" · ");
    const back = () => { if (!creating) pickRepo(); };

    const form = h("form", { class: "card form-card", novalidate: true, onsubmit: (e) => { e.preventDefault(); create(); } },
      h("div", { class: "selection-head" },
        h("div", { class: "selection-icon" }, icon("github")),
        h("div", { class: "selection-main" }, h("div", { class: "selection-name" }, repo.full_name), h("div", { class: "selection-meta" }, meta)),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: back }, "Change")),
      h("div", { class: "form-grid" },
        h("label", { class: "field", for: "gh-name" }, h("span", { class: "field-label" }, "Name"), name),
        h("label", { class: "field", for: "gh-branch" }, h("span", { class: "field-label" }, "Branch"), br.el)),
      h("div", { class: "form-grid" },
        h("label", { class: "field", for: "gh-subdir" }, h("span", { class: "field-label" }, "Subfolder ", h("span", { class: "faint" }, "(optional)")), subdir,
          h("span", { class: "field-hint" }, "For a repo with several bots: the folder that holds this one.")),
        h("label", { class: "field", for: "gh-entry" }, h("span", { class: "field-label" }, "Entrypoint ", h("span", { class: "faint" }, "(optional)")), entry,
          h("span", { class: "field-hint" }, "Relative to the subfolder. Leave empty to detect it."))),
      h("label", { class: "field", for: "gh-desc" }, h("span", { class: "field-label" }, "Description"), desc),
      h("label", { class: "switch-row", for: "gh-auto" },
        h("span", { class: "switch" }, auto, h("span", { class: "switch-track" })),
        h("span", null, h("span", { class: "switch-label" }, "Auto-update"),
          h("span", { class: "field-hint" }, "When the branch gets a new commit, BotDock pulls it and restarts the bot. Files your bot writes itself (like data/*.json) are kept."))),
      keep.el,
      h("label", { class: "check" }, startNow, h("span", null, "Start the bot right after it's downloaded"),
        h("span", { class: "check-hint" }, "A .env file in a repo is never imported — leave this off if the bot still needs its token under Secrets.")),
      err, status,
      h("div", { class: "form-actions" }, h("button", { class: "btn", type: "button", onclick: back }, "Back"), submit));
    setChildren(stage, form);
    name.focus();
    name.select();

    async function create() {
      err.hidden = true;
      const n = name.value.trim();
      if (!n) { showErr("Give the bot a name."); name.focus(); return; }
      if (allBots().some((b) => b.name.toLowerCase() === n.toLowerCase())) { showErr(`A bot named “${n}” already exists.`); name.focus(); return; }
      const sd = subdir.value.trim().replace(/^\/+|\/+$/g, "");
      if (sd.split("/").some((p) => p === "..")) { showErr("The subfolder must be a path inside the repository."); subdir.focus(); return; }
      const body = { name: n, description: desc.value.trim(), repo: repo.full_name, branch: br.el.value || repo.default_branch,
        subdir: sd, auto_update: auto.checked, keep_paths: keep.get() };
      if (entry.value.trim()) body.entrypoint = entry.value.trim();
      creating = true;
      for (const el of form.querySelectorAll("input, select")) el.disabled = true;
      status.hidden = false;
      setChildren(status, h("span", { class: "spinner spinner-sm" }), h("span", null, `Downloading ${repo.full_name}@${body.branch}… large repositories can take a moment.`));
      setGuard(() => "The repository is still being downloaded.");
      await busy(submit, async () => {
        let res;
        try { res = await post("/api/bots/github", body); } catch (e) {
          creating = false;
          setGuard(null);
          if (!alive) return;
          status.hidden = true;
          for (const el of form.querySelectorAll("input, select")) el.disabled = false;
          showErr(e.message);
          return;
        }
        creating = false;
        setGuard(null);
        if (!alive) return;
        upsertBot(res.bot);
        if (!res.bot.entrypoint) chooseEntrypoint(res, startNow.checked);
        else finish(res.bot, startNow.checked);
      });
    }
  }

  if (pending) { const p = pending; pending = null; handle(p); } else if (source === "github") githubStage(); else pickStage();

  return {
    unmount() { alive = false; if (xhr) xhr.abort(); if (picker) picker.destroy(); setGuard(null); },
  };
}
