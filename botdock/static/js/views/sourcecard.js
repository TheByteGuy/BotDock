// Bot → Settings → Source: GitHub link status and controls, or "link to a repo" for uploaded bots.
import { h, icon, setChildren } from "../dom.js";
import { api, enc } from "../api.js";
import { busy, confirmDialog, toast, toastError, fmtRelative, spinnerBlock } from "../ui.js";
import { upsertBot } from "../store.js";
import { getGithub, isGithub, repoUrl, extLink, commitLine, shortSha, commitUrl, repoPicker, branchSelect,
  notConnected, syncSource, checkSource, keepPathsEditor } from "../github.js";

export function sourceSection(ctx) {
  const id = ctx.botId;
  let mode = "view";          // view | edit | link
  let picker = null;
  const body = h("div", { class: "source-body" });
  const el = h("section", { class: "card form-card source-card", id: "source" },
    h("h2", { class: "section-title gh-title" }, icon("github"), h("span", null, "Source")), body);

  let lastJson = "";
  let keepDirty = false;      // unsaved edits in the "Never overwrite" list: don't re-render under the user
  const sameList = (a, b) => JSON.stringify(a || []) === JSON.stringify(b || []);
  async function putSource(payload, button) {
    const run = async () => {
      try {
        const d = await api(`/api/bots/${enc(id)}/source`, { method: "PUT", json: payload });
        if (d && d.bot) upsertBot(d.bot);
        return d.bot;
      } catch (e) { toastError(e, "Couldn't update the source"); return null; }
    };
    return button ? busy(button, run) : run();
  }

  function render() {
    if (picker) { picker.destroy(); picker = null; }
    const bot = ctx.getBot();
    if (!bot) return;
    keepDirty = false;
    lastJson = JSON.stringify(bot.source || {});
    if (mode === "link") return renderLink();
    if (isGithub(bot)) return mode === "edit" ? renderEdit(bot) : renderGithub(bot);
    mode = "view";
    const link = h("button", { class: "btn", type: "button", onclick: () => { mode = "link"; render(); } }, icon("link"), h("span", { class: "btn-label" }, "Link to a GitHub repo"));
    setChildren(body,
      h("p", { class: "muted" }, "This bot's code was uploaded. Link it to a GitHub repository and BotDock can pull new commits for you — automatically if you like."),
      h("div", { class: "button-row" }, link));
  }

  function renderGithub(bot) {
    const src = bot.source;
    const row = (k, v) => [h("dt", null, k), h("dd", null, v)];
    const upToDate = src.commit_sha && src.latest_sha && src.latest_sha === src.commit_sha;
    const newest = !src.latest_sha ? h("span", { class: "muted" }, "not checked yet")
      : !src.commit_sha ? h("span", { class: "commit-line" },
        extLink(commitUrl(src, src.latest_sha), h("span", { class: "chip chip-mono" }, icon("commit"), shortSha(src.latest_sha))),
        h("span", { class: "chip chip-info" }, "not pulled yet"))
      : upToDate ? h("span", { class: "ok-text" }, icon("check"), " Up to date")
        : h("span", { class: "commit-line" },
          extLink(commitUrl(src, src.latest_sha), h("span", { class: "chip chip-mono" }, icon("commit"), shortSha(src.latest_sha))),
          h("span", { class: "chip chip-info" }, "update available"));

    const auto = h("input", { type: "checkbox", id: "src-auto", role: "switch", checked: !!src.auto_update });
    auto.addEventListener("change", async () => {
      auto.disabled = true;
      const b = await putSource({ type: "github", repo: src.repo, branch: src.branch, subdir: src.subdir || "", auto_update: auto.checked, keep_paths: src.keep_paths || [] });
      if (b) toast(auto.checked ? "Auto-update on" : "Auto-update off", "success", { timeout: 2000 });
      else auto.checked = !auto.checked;
      auto.disabled = false;
    });

    const checkBtn = h("button", { class: "btn", type: "button", disabled: !!src.syncing }, icon("refresh"), h("span", { class: "btn-label" }, "Check now"));
    checkBtn.addEventListener("click", () => checkSource(id, checkBtn));
    const pullBtn = h("button", { class: `btn ${src.update_available || !src.commit_sha ? "btn-primary" : ""}`, type: "button", disabled: !!src.syncing },
      src.syncing ? h("span", { class: "spinner spinner-sm" }) : icon("cloudDown"), h("span", { class: "btn-label" }, src.syncing ? "Pulling…" : "Pull now"));
    pullBtn.addEventListener("click", () => syncSource(id, pullBtn)); // asks first on a never-pulled bot

    // "Never overwrite" list with its own Save
    const keepSave = h("button", { class: "btn btn-sm btn-primary", type: "button", disabled: true }, h("span", { class: "btn-label" }, "Save list"));
    const keep = keepPathsEditor({ initial: src.keep_paths || [], id: "src-keep",
      onChange: (list) => { keepDirty = !sameList(list, src.keep_paths); keepSave.disabled = !keepDirty; } });
    keepSave.addEventListener("click", async () => {
      const list = keep.get();
      const b = await putSource({ type: "github", repo: src.repo, branch: src.branch, subdir: src.subdir || "", auto_update: !!src.auto_update, keep_paths: list }, keepSave);
      if (b) { keepDirty = false; toast("Saved — matching files won't be overwritten by a pull", "success"); render(); }
    });
    const editBtn = h("button", { class: "btn btn-ghost", type: "button", onclick: () => { mode = "edit"; render(); } }, icon("branch"), h("span", { class: "btn-label" }, "Change branch or folder"));
    const unlinkBtn = h("button", { class: "btn btn-ghost", type: "button" }, icon("unlink"), h("span", { class: "btn-label" }, "Unlink"));
    unlinkBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: "Unlink from GitHub?", confirmText: "Unlink", danger: true,
        message: "The bot keeps its current files and keeps running; BotDock just stops checking the repository for updates." });
      if (!ok) return;
      const b = await putSource({ type: "upload" }, unlinkBtn);
      if (b) toast("Unlinked from GitHub", "success");
    });

    setChildren(body,
      h("div", { class: "source-repo" },
        extLink(repoUrl(src), h("strong", { class: "mono" }, src.repo), icon("external")),
        h("span", { class: "chip chip-mono" }, icon("branch"), src.branch),
        src.subdir ? h("span", { class: "chip chip-mono", title: "Subfolder used as the bot root" }, icon("folder"), src.subdir) : null),
      src.last_error ? h("div", { class: "banner banner-warn source-error", role: "alert" }, icon("alert"),
        h("div", { class: "banner-text" }, /^github\b/i.test(src.last_error) ? null : h("strong", null, "GitHub: "), src.last_error,
          /not connected/i.test(src.last_error) ? [" — ", h("a", { href: "#/account/github" }, "connect GitHub")] : null)) : null,
      h("dl", { class: "kv source-kv" },
        row("Running commit", commitLine(src)),
        row("Newest on branch", newest),
        row("Last checked", src.last_checked_at ? h("span", { dataset: { tick: "relative", t: src.last_checked_at } }, fmtRelative(src.last_checked_at)) : h("span", { class: "muted" }, "never")),
        row("Last pulled", src.last_synced_at ? h("span", { dataset: { tick: "relative", t: src.last_synced_at } }, fmtRelative(src.last_synced_at)) : h("span", { class: "muted" }, "never"))),
      h("label", { class: "switch-row", for: "src-auto" },
        h("span", { class: "switch" }, auto, h("span", { class: "switch-track" })),
        h("span", null, h("span", { class: "switch-label" }, "Auto-update"),
          h("span", { class: "field-hint" }, "Pull new commits on this branch and restart the bot automatically. Files in the “Never overwrite” list are never touched; any other local file a pull replaces is backed up first."))),
      h("div", { class: "keep-box" }, keep.el, h("div", { class: "keep-save" }, keepSave)),
      h("div", { class: "button-row" }, pullBtn, checkBtn, editBtn, unlinkBtn),
      h("p", { class: "field-hint" }, "Pull now restarts the bot if it's running. A stopped bot is updated and stays stopped."));
  }

  function renderEdit(bot) {
    const src = bot.source;
    const br = branchSelect(src.repo, src.branch, "src-branch");
    const sub = h("input", { class: "input mono", id: "src-subdir", type: "text", value: src.subdir || "", placeholder: "Repository root", autocomplete: "off", spellcheck: false });
    const err = h("div", { class: "field-error", role: "alert" });
    br.ready.catch((e) => { err.textContent = `Couldn't load branches: ${e.message}`; });
    const save = h("button", { class: "btn btn-primary", type: "submit" }, h("span", { class: "btn-label" }, "Save"));
    setChildren(body, h("form", { novalidate: true, onsubmit: async (e) => {
      e.preventDefault();
      const b = await putSource({ type: "github", repo: src.repo, branch: br.el.value || src.branch,
        subdir: sub.value.trim().replace(/^\/+|\/+$/g, ""), auto_update: !!src.auto_update, keep_paths: src.keep_paths || [] }, save);
      if (b) { mode = "view"; render(); toast("Source updated — pull to get the code from the new branch or folder", "success"); }
    } },
    h("div", { class: "source-repo" }, icon("github"), h("strong", { class: "mono" }, src.repo)),
    h("div", { class: "form-grid" },
      h("label", { class: "field", for: "src-branch" }, h("span", { class: "field-label" }, "Branch"), br.el),
      h("label", { class: "field", for: "src-subdir" }, h("span", { class: "field-label" }, "Subfolder"), sub)),
    err,
    h("div", { class: "form-actions" }, h("button", { class: "btn", type: "button", onclick: () => { mode = "view"; render(); } }, "Cancel"), save)));
  }

  async function renderLink() {
    setChildren(body, spinnerBlock("Checking GitHub…"));
    let gh;
    try { gh = await getGithub(true); } catch (e) { setChildren(body, h("div", { class: "form-error" }, e.message)); return; }
    if (mode !== "link") return;
    const cancel = h("button", { class: "btn", type: "button", onclick: () => { mode = "view"; render(); } }, "Cancel");
    if (!gh.connected) {
      setChildren(body, notConnected("Connect GitHub on the Account page, then come back here to link this bot."), h("div", { class: "form-actions" }, cancel));
      return;
    }
    picker = repoPicker({ onPick: (repo) => {
      picker.destroy(); picker = null;
      const br = branchSelect(repo.full_name, repo.default_branch, "link-branch");
      const sub = h("input", { class: "input mono", id: "link-subdir", type: "text", placeholder: "Repository root", autocomplete: "off", spellcheck: false });
      // linking a bot that already has files: auto-update starts OFF so nothing changes before an explicit first pull
      const auto = h("input", { type: "checkbox", id: "link-auto", role: "switch", checked: false });
      const keep = keepPathsEditor({ initial: [], id: "link-keep" });
      api(`/api/bots/${enc(id)}/files`).then((d) => {
        if ((d.entries || []).some((e) => e.type === "dir" && e.path === "data")) keep.set(["data/**"]);
      }).catch(() => {});
      const err = h("div", { class: "field-error", role: "alert" });
      br.ready.catch((e) => { err.textContent = `Couldn't load branches: ${e.message}`; });
      const save = h("button", { class: "btn btn-primary", type: "submit" }, icon("link"), h("span", { class: "btn-label" }, "Link repository"));
      setChildren(body, h("form", { novalidate: true, onsubmit: async (e) => {
        e.preventDefault();
        const b = await putSource({ type: "github", repo: repo.full_name, branch: br.el.value || repo.default_branch,
          subdir: sub.value.trim().replace(/^\/+|\/+$/g, ""), auto_update: auto.checked, keep_paths: keep.get() }, save);
        if (b) { mode = "view"; render(); toast("Linked. Nothing has changed yet — press Pull now when you're ready.", "success"); }
      } },
      h("div", { class: "selection-head" }, h("div", { class: "selection-icon" }, icon("github")),
        h("div", { class: "selection-main" }, h("div", { class: "selection-name" }, repo.full_name), h("div", { class: "selection-meta" }, repo.private ? "Private" : "Public")),
        h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => renderLink() }, "Change")),
      h("div", { class: "form-grid" },
        h("label", { class: "field", for: "link-branch" }, h("span", { class: "field-label" }, "Branch"), br.el),
        h("label", { class: "field", for: "link-subdir" }, h("span", { class: "field-label" }, "Subfolder ", h("span", { class: "faint" }, "(optional)")), sub)),
      h("label", { class: "switch-row", for: "link-auto" },
        h("span", { class: "switch" }, auto, h("span", { class: "switch-track" })),
        h("span", null, h("span", { class: "switch-label" }, "Auto-update"),
          h("span", { class: "field-hint" }, "Off for now is safest: turn it on after the first pull, once you've checked the result."))),
      keep.el,
      h("div", { class: "link-explain" }, icon("info"),
        h("div", null,
          h("p", null, h("strong", null, "Linking changes no files. "), "Your first Pull replaces every file that is different in the repository — the old copies are kept in the bot's ",
            h("code", null, "backups/"), " folder."),
          h("p", null, "Files the repository doesn't have stay as they are, and files in the “Never overwrite” list are never touched."))),
      err,
      h("div", { class: "form-actions" }, h("button", { class: "btn", type: "button", onclick: () => { mode = "view"; render(); } }, "Cancel"), save)));
    } });
    setChildren(body, h("p", { class: "muted" }, "Choose the repository this bot's code lives in."), picker.el, h("div", { class: "form-actions" }, cancel));
    picker.focus();
  }

  render();
  return {
    el,
    // only re-render when the source itself changed (stats events arrive every 5 s)
    update(bot) { if (mode === "view" && !keepDirty && JSON.stringify(bot.source || {}) !== lastJson) render(); },
    destroy() { if (picker) picker.destroy(); },
  };
}

export { shortSha };
