// Settings tab: bot config form, code update, dependencies, export, danger zone.
import { h, icon, setChildren } from "../dom.js";
import { api, post, upload, enc } from "../api.js";
import { busy, confirmDialog, toast, toastError, fmtBytes, plural, envImportToasts } from "../ui.js";
import { dropZone, analyze, toFormData, checkLimits, progressBar } from "../upload.js";
import { patchBot, upsertBot, removeBot } from "../store.js";
import { sourceSection } from "./sourcecard.js";

const POLICIES = [
  { value: "on-failure", label: "On failure", hint: "Restart after a crash (non-zero exit)" },
  { value: "always", label: "Always", hint: "Restart whenever the process exits" },
  { value: "never", label: "Never", hint: "Stay stopped once it exits" },
];
const FIELDS = ["name", "description", "entrypoint", "args", "extra_packages", "restart_policy", "autostart"];

export function mount(panel, ctx) {
  const id = ctx.botId;
  let alive = true;
  let xhr = null;
  const bot0 = ctx.getBot();

  // ---- config form
  const name = h("input", { class: "input", id: "s-name", type: "text", maxlength: "60", required: true, autocomplete: "off", spellcheck: false });
  const desc = h("input", { class: "input", id: "s-desc", type: "text", maxlength: "200", autocomplete: "off", placeholder: "Optional" });
  const pyList = h("datalist", { id: "s-py-files" });
  const entry = h("input", { class: "input mono", id: "s-entry", type: "text", list: "s-py-files", autocomplete: "off", spellcheck: false, placeholder: "main.py" });
  const args = h("input", { class: "input mono", id: "s-args", type: "text", autocomplete: "off", spellcheck: false, placeholder: "--verbose" });
  const pkgs = h("textarea", { class: "input mono", id: "s-pkgs", rows: "3", spellcheck: false, autocomplete: "off", placeholder: "discord.py>=2.3\npython-dotenv" });
  const autostart = h("input", { type: "checkbox", id: "s-autostart", role: "switch" });
  const policyInputs = POLICIES.map((p) => h("input", { type: "radio", name: "s-policy", value: p.value }));
  const err = h("div", { class: "form-error", role: "alert", hidden: true });
  const saveBtn = h("button", { class: "btn btn-primary", type: "submit", disabled: true }, h("span", { class: "btn-label" }, "Save changes"));
  const resetBtn = h("button", { class: "btn", type: "button", disabled: true, onclick: () => fill(ctx.getBot(), true) }, "Reset");
  let base = null;

  function values() {
    return {
      name: name.value.trim(), description: desc.value.trim(), entrypoint: entry.value.trim() || null,
      args: args.value.trim(), extra_packages: pkgs.value.trim(),
      restart_policy: (policyInputs.find((i) => i.checked) || {}).value || "on-failure",
      autostart: autostart.checked,
    };
  }
  function changes() {
    const v = values();
    const out = {};
    for (const k of FIELDS) if ((v[k] ?? "") !== (base[k] ?? "")) out[k] = v[k];
    return out;
  }
  function refreshDirty() {
    const dirty = base && Object.keys(changes()).length > 0;
    saveBtn.disabled = !dirty;
    resetBtn.disabled = !dirty;
    ctx.setGuard(dirty ? () => "Your settings changes haven't been saved." : null);
  }
  function fill(bot, force = false) {
    if (!bot) return;
    if (!force && base && Object.keys(changes()).length) return; // don't clobber edits in progress
    base = {};
    for (const k of FIELDS) base[k] = k === "entrypoint" ? bot[k] || null : bot[k] ?? "";
    const set = (el, v) => { if (el.value !== v) el.value = v; }; // avoid moving the caret needlessly
    set(name, bot.name || "");
    set(desc, bot.description || "");
    set(entry, bot.entrypoint || "");
    set(args, bot.args || "");
    set(pkgs, bot.extra_packages || "");
    autostart.checked = !!bot.autostart;
    for (const i of policyInputs) i.checked = i.value === (bot.restart_policy || "on-failure");
    err.hidden = true;
    refreshDirty();
  }

  const form = h("form", { class: "card form-card settings-form", novalidate: true, onsubmit: (e) => {
    e.preventDefault();
    const ch = changes();
    if (!Object.keys(ch).length) return;
    if ("name" in ch && !ch.name) { showErr("The name can't be empty."); name.focus(); return; }
    busy(saveBtn, async () => {
      try {
        const bot = await patchBot(id, ch);
        fill(bot, true);
        toast("Settings saved", "success");
      } catch (e2) { showErr(e2.message); }
    }).then(refreshDirty);
  } },
  h("h2", { class: "section-title" }, "General"),
  h("div", { class: "form-grid" },
    h("label", { class: "field", for: "s-name" }, h("span", { class: "field-label" }, "Name"), name),
    h("label", { class: "field", for: "s-desc" }, h("span", { class: "field-label" }, "Description"), desc)),
  h("h2", { class: "section-title" }, "Run"),
  h("div", { class: "form-grid" },
    h("label", { class: "field", for: "s-entry" }, h("span", { class: "field-label" }, "Entrypoint"), entry, pyList,
      h("span", { class: "field-hint" }, "Path of the script to run, relative to the bot folder.")),
    h("label", { class: "field", for: "s-args" }, h("span", { class: "field-label" }, "Arguments"), args,
      h("span", { class: "field-hint" }, "Extra command-line arguments, split like a shell would."))),
  h("fieldset", { class: "field" },
    h("legend", { class: "field-label" }, "Restart policy"),
    h("div", { class: "segmented-cards" }, POLICIES.map((p, i) => h("label", { class: "seg-card" }, policyInputs[i],
      h("span", { class: "seg-title" }, p.label), h("span", { class: "seg-hint" }, p.hint))))),
  h("label", { class: "switch-row", for: "s-autostart" },
    h("span", { class: "switch" }, autostart, h("span", { class: "switch-track" })),
    h("span", null, h("span", { class: "switch-label" }, "Start automatically"),
      h("span", { class: "field-hint" }, "When BotDock boots, start this bot again if it was running before."))),
  h("h2", { class: "section-title" }, "Dependencies"),
  h("label", { class: "field", for: "s-pkgs" }, h("span", { class: "field-label" }, "Extra pip packages"), pkgs,
    h("span", { class: "field-hint dep-hint" })),
  err,
  h("div", { class: "form-actions sticky-actions" }, resetBtn, saveBtn));
  form.addEventListener("input", refreshDirty);
  form.addEventListener("change", refreshDirty);
  function showErr(m) { err.textContent = m; err.hidden = false; }

  // ---- code + deps actions
  const codeArea = h("div", { class: "code-update" });
  const reinstallBtn = h("button", { class: "btn", type: "button", onclick: reinstall }, icon("refresh"), h("span", { class: "btn-label" }, "Reinstall dependencies"));
  const exportLink = h("a", { class: "btn", href: `/api/bots/${enc(id)}/export`, download: "" }, icon("download"), h("span", { class: "btn-label" }, "Export as zip"));
  const deleteBtn = h("button", { class: "btn btn-danger", type: "button", onclick: deleteBot }, icon("trash"), h("span", { class: "btn-label" }, "Delete bot"));

  const source = sourceSection(ctx);
  panel.append(h("div", { class: "settings" },
    source.el,
    form,
    h("section", { class: "card form-card" },
      h("h2", { class: "section-title" }, "Code"),
      h("p", { class: "muted" }, "Upload a new version of the bot. Drop a folder, a .zip or a single .py file."),
      codeArea,
      h("div", { class: "button-row" }, exportLink, reinstallBtn),
      h("p", { class: "field-hint" }, "Reinstall wipes the bot's virtualenv and installs requirements.txt and the extra packages again; a running bot is restarted.")),
    h("section", { class: "card form-card danger-zone" },
      h("h2", { class: "section-title" }, "Danger zone"),
      h("div", { class: "danger-row" },
        h("div", null, h("strong", null, "Delete this bot"),
          h("p", { class: "muted" }, "Stops it and permanently removes its code, data files, logs and secrets.")),
        deleteBtn))));

  function codePicker() {
    setChildren(codeArea, dropZone({ title: "Drop updated code here", compact: true, onSelect: reviewCode }));
  }

  async function reviewCode(p) {
    let sel;
    try { sel = await p; } catch (e) { toast(e.message, "error"); return; }
    if (!alive) return;
    const info = analyze(sel);
    const problem = checkLimits(sel);
    const merge = h("input", { type: "radio", name: "code-mode", value: "merge", checked: true });
    const replace = h("input", { type: "radio", name: "code-mode", value: "replace" });
    const bar = progressBar();
    bar.el.hidden = true;
    const go = h("button", { class: "btn btn-primary", type: "button", disabled: !!problem }, icon("upload"), h("span", { class: "btn-label" }, "Upload"));
    const cancel = h("button", { class: "btn", type: "button", onclick: () => { if (xhr) xhr.abort(); else codePicker(); } }, "Cancel");
    go.addEventListener("click", async () => {
      const mode = replace.checked ? "replace" : "merge";
      if (mode === "replace") {
        const ok = await confirmDialog({ title: "Replace all files?", danger: true, confirmText: "Replace",
          message: "Every current file is deleted first, including data files the bot created (JSON databases, caches). Use merge to keep them." });
        if (!ok) return;
      }
      bar.el.hidden = false;
      merge.disabled = replace.disabled = true;
      await busy(go, async () => {
        xhr = upload(`/api/bots/${enc(id)}/code`, toFormData(sel, { mode }), (l, t) => bar.set(l, t));
        try {
          const r = await xhr.promise;
          xhr = null;
          if (!alive) return;
          upsertBot(r.bot);
          toast("Code updated", "success", { detail: r.bot.runtime?.state === "running" ? "Restart the bot to run the new code." : null });
          envImportToasts(r);
          if (!r.bot.entrypoint) toast("No entrypoint could be detected. Set one above.", "error");
          fill(r.bot);
          loadPyFiles();
          codePicker();
        } catch (e) {
          xhr = null;
          if (!alive) return;
          bar.el.hidden = true;
          merge.disabled = replace.disabled = false;
          if (e.status !== -1) toastError(e, "Upload failed");
        }
      });
    });
    setChildren(codeArea, h("div", { class: "code-review" },
      h("div", { class: "selection-head" },
        h("div", { class: "selection-icon" }, icon(sel.kind === "py" ? "fileCode" : sel.kind === "zip" ? "zip" : "folder")),
        h("div", { class: "selection-main" }, h("div", { class: "selection-name" }, sel.label),
          h("div", { class: "selection-meta" }, `${sel.kind === "zip" ? "Zip" : plural(info.count, "file")} · ${fmtBytes(info.size)}${sel.skippedDirs.size ? ` · skipped ${[...sel.skippedDirs].join(", ")}` : ""}`))),
      problem ? h("div", { class: "form-error" }, problem) : null,
      h("div", { class: "radio-cards" },
        h("label", { class: "seg-card" }, merge, h("span", { class: "seg-title" }, "Merge"), h("span", { class: "seg-hint" }, "Overwrite uploaded files, keep everything else (recommended)")),
        h("label", { class: "seg-card" }, replace, h("span", { class: "seg-title" }, "Replace"), h("span", { class: "seg-hint" }, "Delete all current files first"))),
      bar.el,
      h("div", { class: "form-actions" }, cancel, go)));
  }

  async function reinstall() {
    const ok = await confirmDialog({ title: "Reinstall dependencies?", confirmText: "Reinstall",
      message: "The virtualenv is deleted and rebuilt from requirements.txt and the extra packages. If the bot is running it restarts afterwards." });
    if (!ok) return;
    await busy(reinstallBtn, async () => {
      try {
        const r = await post(`/api/bots/${enc(id)}/reinstall`);
        if (r && r.bot) upsertBot(r.bot);
        toast("Reinstalling dependencies — follow along in the console", "success");
      } catch (e) { toastError(e, "Couldn't reinstall"); }
    });
  }

  async function deleteBot() {
    const bot = ctx.getBot();
    const ok = await confirmDialog({ title: `Delete ${bot.name}?`, danger: true, confirmText: "Delete forever", requireText: bot.name,
      message: h("p", null, "This stops the bot and permanently deletes its code, data files, logs and secrets. Export a zip first if you might want it back.") });
    if (!ok) return;
    await busy(deleteBtn, async () => {
      try {
        ctx.markDeleted();
        ctx.setGuard(null);
        await api(`/api/bots/${enc(id)}`, { method: "DELETE" });
        toast(`Deleted ${bot.name}`, "success");
        removeBot(id); // navigates home via the bot page's subscription
      } catch (e) { toastError(e, "Couldn't delete"); }
    });
  }

  async function loadPyFiles() {
    try {
      const d = await api(`/api/bots/${enc(id)}/files`);
      if (!alive) return;
      const py = (d.entries || []).filter((e) => e.type === "file" && e.path.endsWith(".py"))
        .sort((a, b) => (a.path.split("/").length - b.path.split("/").length) || a.path.localeCompare(b.path));
      setChildren(pyList, py.slice(0, 500).map((e) => h("option", { value: e.path })));
    } catch { /* datalist is optional */ }
  }

  function updateDepHint(bot) {
    const el = form.querySelector(".dep-hint");
    el.textContent = bot.has_requirements
      ? "Installed in addition to requirements.txt. One requirement per line or separated by spaces."
      : "This bot has no requirements.txt, so list what it needs here (e.g. discord.py).";
  }

  fill(bot0, true);
  updateDepHint(bot0);
  codePicker();
  loadPyFiles();

  return {
    onBot(bot) { fill(bot); updateDepHint(bot); source.update(bot); },
    unmount() { alive = false; if (xhr) xhr.abort(); source.destroy(); ctx.setGuard(null); },
  };
}
