// Files tab: lazy tree (dirs collapsed) + plain-text editor with line numbers, Tab indent, Ctrl+S.
import { h, icon, setChildren } from "../dom.js";
import { api, post, upload, enc } from "../api.js";
import { busy, confirmDialog, promptDialog, showMenu, toast, toastError, fmtBytes, spinnerBlock, plural, iconBtn } from "../ui.js";
import { fromDrop, fromFileList, toFormData, checkLimits, progressBar } from "../upload.js";

const DIR_RENDER_CAP = 400;   // rows rendered per directory before a "show more" button
const FILTER_CAP = 300;
const INDENT = "    ";

function parentOf(p) { const i = p.lastIndexOf("/"); return i < 0 ? "" : p.slice(0, i); }
function baseName(p) { return p.slice(p.lastIndexOf("/") + 1); }
function pathProblem(p) {
  if (!p) return "Enter a name.";
  if (p.startsWith("/") || /^[A-Za-z]:/.test(p)) return "Use a path relative to the bot folder.";
  if (p.split("/").some((s) => s === ".." || s === "." || s === "")) return "That path isn't valid.";
  return null;
}
function fileIcon(name) {
  if (/\.py$/i.test(name)) return "fileCode";
  if (/\.zip$/i.test(name)) return "zip";
  return "file";
}

export function mount(panel, ctx) {
  const id = ctx.botId;
  const base = `/api/bots/${enc(id)}/files`;
  let alive = true;
  let entries = [];
  let children = new Map();          // dir path -> [entries]
  let truncated = false;
  const expanded = new Set();
  const showAll = new Set();          // dirs where the render cap was lifted
  let selected = "";                  // selected path (file or dir)
  let filterText = "";
  let open = null;                    // {path, original, binary, size}
  let uploading = null;

  // ---- layout
  const treeBody = h("div", { class: "tree", role: "tree", "aria-label": "Files" }, spinnerBlock("Loading files…"));
  const treeNote = h("div", { class: "tree-note", hidden: true });
  const filterInput = h("input", { class: "input input-sm", type: "search", placeholder: "Find file", "aria-label": "Find file", autocomplete: "off", spellcheck: false });
  const upBar = progressBar();
  upBar.el.hidden = true;
  const fileInput = h("input", { type: "file", multiple: true, class: "sr-only", tabIndex: -1 });
  const dirInput = h("input", { type: "file", class: "sr-only", tabIndex: -1 });
  dirInput.webkitdirectory = true;
  let uploadTarget = "";

  const uploadBtn = iconBtn("upload", "Upload files here", (e) => openUploadMenu(e.currentTarget, targetDir()));
  const treePane = h("aside", { class: "files-tree card" },
    h("div", { class: "pane-toolbar" },
      h("div", { class: "search-box grow" }, icon("search"), filterInput),
      iconBtn("filePlus", "New file", () => newFile(targetDir())),
      iconBtn("folderPlus", "New folder", () => newFolder(targetDir())),
      uploadBtn,
      iconBtn("refresh", "Refresh", () => reload(true))),
    treeNote, treeBody, upBar.el, fileInput, dirInput);

  const editorPane = h("section", { class: "files-editor card" });
  const root = h("div", { class: "files" }, treePane, editorPane);
  panel.append(root);

  // ---- tree data
  function index() {
    children = new Map([["", []]]);
    for (const e of entries) {
      const p = parentOf(e.path);
      if (!children.has(p)) children.set(p, []);
      children.get(p).push(e);
      if (e.type === "dir" && !children.has(e.path)) children.set(e.path, []);
    }
    for (const list of children.values()) {
      list.sort((a, b) => (a.type === b.type ? 0 : a.type === "dir" ? -1 : 1) || a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true }));
    }
  }

  async function reload(manual = false) {
    try {
      const d = await api(base);
      if (!alive) return;
      entries = d.entries || [];
      truncated = !!d.truncated;
      index();
      renderTree();
      if (manual) toast("File list refreshed", "info", { timeout: 1500 });
    } catch (e) {
      if (!alive) return;
      toastError(e, "Couldn't load files");
      setChildren(treeBody, h("div", { class: "tree-empty" }, h("p", null, e.message), h("button", { class: "btn btn-sm", type: "button", onclick: () => reload() }, "Try again")));
    }
  }

  // ---- tree rendering
  function row(e, depth) {
    const isDir = e.type === "dir";
    const isOpen = isDir && expanded.has(e.path);
    const r = h("div", { class: `tree-row${e.path === selected ? " selected" : ""}${open && open.path === e.path ? " open" : ""}`,
      role: "treeitem", "aria-expanded": isDir ? String(isOpen) : null, "aria-selected": String(e.path === selected),
      dataset: { path: e.path, dir: isDir ? "1" : "" }, style: { "--depth": depth } },
    h("button", { class: "tree-main", type: "button", tabIndex: 0, title: e.path },
      isDir ? icon(isOpen ? "chevronDown" : "chevronRight", "tree-chevron") : h("span", { class: "tree-chevron-pad" }),
      icon(isDir ? "folder" : fileIcon(e.name), isDir ? "tree-icon dir" : "tree-icon"),
      h("span", { class: "tree-name" }, e.name),
      !isDir ? h("span", { class: "tree-size" }, fmtBytes(e.size)) : null),
    h("button", { class: "icon-btn tree-more", type: "button", "aria-label": `Actions for ${e.name}`, "aria-haspopup": "menu", title: "Actions" }, icon("more")));
    return r;
  }

  function renderDir(path, depth, out) {
    const list = children.get(path) || [];
    const cap = showAll.has(path) ? Infinity : DIR_RENDER_CAP;
    for (let i = 0; i < list.length && i < cap; i++) {
      const e = list[i];
      out.append(row(e, depth));
      if (e.type === "dir" && expanded.has(e.path)) renderDir(e.path, depth + 1, out);
    }
    if (list.length > cap) {
      out.append(h("button", { class: "tree-more-rows", type: "button", style: { "--depth": depth }, dataset: { showAll: path } },
        `Show ${(list.length - cap).toLocaleString()} more…`));
    }
  }

  function renderTree() {
    treeNote.hidden = !truncated;
    treeNote.textContent = truncated ? "Only the first 5,000 entries are listed." : "";
    const scroll = treeBody.scrollTop;
    const out = document.createDocumentFragment();
    if (filterText) {
      const q = filterText.toLowerCase();
      const hits = entries.filter((e) => e.type === "file" && e.path.toLowerCase().includes(q));
      for (const e of hits.slice(0, FILTER_CAP)) {
        const r = row(e, 0);
        r.querySelector(".tree-name").textContent = e.path;
        out.append(r);
      }
      if (!hits.length) out.append(h("div", { class: "tree-empty" }, "No files match."));
      else if (hits.length > FILTER_CAP) out.append(h("div", { class: "tree-empty" }, `${(hits.length - FILTER_CAP).toLocaleString()} more matches — refine the search`));
    } else {
      renderDir("", 0, out);
      if (!entries.length) out.append(h("div", { class: "tree-empty" }, h("p", null, "This bot has no files."), h("p", { class: "faint" }, "Drop files here or use the upload button.")));
    }
    setChildren(treeBody, out);
    treeBody.scrollTop = scroll;
  }

  function ensureVisible(path) {
    let p = parentOf(path);
    while (p) { expanded.add(p); p = parentOf(p); }
  }

  function targetDir() {
    if (!selected) return "";
    const e = entries.find((x) => x.path === selected);
    if (!e) return "";
    return e.type === "dir" ? e.path : parentOf(e.path);
  }

  treeBody.addEventListener("click", (ev) => {
    const more = ev.target.closest(".tree-more-rows");
    if (more) { showAll.add(more.dataset.showAll); renderTree(); return; }
    const r = ev.target.closest(".tree-row");
    if (!r) return;
    const path = r.dataset.path;
    const e = entries.find((x) => x.path === path);
    if (!e) return;
    if (ev.target.closest(".tree-more")) { rowMenu(ev.target.closest(".tree-more"), e); return; }
    selected = path;
    if (e.type === "dir") {
      if (expanded.has(path)) expanded.delete(path); else expanded.add(path);
      renderTree();
      treeBody.querySelector(`.tree-row[data-path="${CSS.escape(path)}"] .tree-main`)?.focus();
    } else openFile(path);
  });

  treeBody.addEventListener("keydown", (ev) => {
    const r = ev.target.closest(".tree-row");
    if (!r) return;
    const rows = [...treeBody.querySelectorAll(".tree-row")];
    const i = rows.indexOf(r);
    const path = r.dataset.path;
    const focusRow = (x) => x && x.querySelector(".tree-main").focus();
    if (ev.key === "ArrowDown") { ev.preventDefault(); focusRow(rows[i + 1]); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); focusRow(rows[i - 1]); }
    else if (ev.key === "ArrowRight" && r.dataset.dir && !expanded.has(path)) { ev.preventDefault(); expanded.add(path); renderTree(); focusPath(path); }
    else if (ev.key === "ArrowLeft") {
      ev.preventDefault();
      if (r.dataset.dir && expanded.has(path)) { expanded.delete(path); renderTree(); focusPath(path); }
      else if (parentOf(path)) focusPath(parentOf(path));
    } else if (ev.key === "Delete") {
      const e = entries.find((x) => x.path === path);
      if (e) { ev.preventDefault(); removeEntry(e); }
    } else if (ev.key === "F2") {
      const e = entries.find((x) => x.path === path);
      if (e) { ev.preventDefault(); rename(e); }
    }
  });
  function focusPath(p) { treeBody.querySelector(`.tree-row[data-path="${CSS.escape(p)}"] .tree-main`)?.focus(); }

  let filterTimer = 0;
  filterInput.addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { filterText = filterInput.value.trim(); renderTree(); }, 120);
  });

  // ---- row actions
  function rowMenu(anchor, e) {
    const items = [];
    if (e.type === "dir") {
      items.push({ label: "New file here", icon: "filePlus", onSelect: () => newFile(e.path) },
        { label: "New folder here", icon: "folderPlus", onSelect: () => newFolder(e.path) },
        { label: "Upload here", icon: "upload", onSelect: () => openUploadMenu(anchor, e.path) }, "sep");
    } else {
      items.push({ label: "Open", icon: "edit", onSelect: () => openFile(e.path) },
        { label: "Download", icon: "download", onSelect: () => download(e.path) }, "sep");
    }
    items.push({ label: "Rename or move", icon: "edit", onSelect: () => rename(e) },
      { label: "Delete", icon: "trash", danger: true, onSelect: () => removeEntry(e) });
    showMenu(anchor, items);
  }

  function download(path) {
    const a = h("a", { href: `${base}/download?path=${enc(path)}`, download: baseName(path), class: "sr-only" });
    document.body.append(a);
    a.click();
    a.remove();
  }

  async function newFile(dir) {
    const name = await promptDialog({ title: "New file", label: dir ? `File name (in ${dir}/)` : "File name", placeholder: "cogs/example.py", confirmText: "Create", validate: pathProblem });
    if (!name) return;
    const path = dir ? `${dir}/${name}` : name;
    if (entries.some((e) => e.path === path)) { toast(`${path} already exists`, "error"); return; }
    if (!(await confirmLeaveFile())) return;
    try {
      await api(`${base}/content`, { method: "PUT", json: { path, content: "" } });
      ensureVisible(path);
      selected = path;
      await reload();
      openFile(path, true);
    } catch (e) { toastError(e, "Couldn't create the file"); }
  }

  async function newFolder(dir) {
    const name = await promptDialog({ title: "New folder", label: dir ? `Folder name (in ${dir}/)` : "Folder name", placeholder: "data", confirmText: "Create", validate: pathProblem });
    if (!name) return;
    const path = dir ? `${dir}/${name}` : name;
    try {
      await post(`${base}/mkdir`, { path });
      ensureVisible(path);
      expanded.add(path);
      selected = path;
      await reload();
      focusPath(path);
    } catch (e) { toastError(e, "Couldn't create the folder"); }
  }

  async function rename(e) {
    const to = await promptDialog({ title: e.type === "dir" ? "Rename folder" : "Rename file", label: "New path (relative to the bot folder)", value: e.path, confirmText: "Rename", validate: pathProblem });
    if (!to || to === e.path) return;
    try {
      await post(`${base}/move`, { from: e.path, to });
      const remap = (p) => (p === e.path ? to : p.startsWith(e.path + "/") ? to + p.slice(e.path.length) : p);
      if (open) open.path = remap(open.path);
      selected = remap(selected);
      for (const p of [...expanded]) { const n = remap(p); if (n !== p) { expanded.delete(p); expanded.add(n); } }
      ensureVisible(to);
      await reload();
      if (open) renderEditorHead();
      toast(`Renamed to ${to}`, "success");
    } catch (err) { toastError(err, "Couldn't rename"); }
  }

  async function removeEntry(e) {
    const isDir = e.type === "dir";
    const inside = isDir ? entries.filter((x) => x.path.startsWith(e.path + "/")).length : 0;
    const ok = await confirmDialog({ title: `Delete ${e.name}?`, danger: true, confirmText: "Delete",
      message: isDir ? `This deletes the folder ${e.path} and ${plural(inside, "item")} inside it. This can't be undone.` : `This deletes ${e.path}. This can't be undone.` });
    if (!ok) return;
    try {
      await post(`${base}/delete`, { path: e.path });
      if (open && (open.path === e.path || open.path.startsWith(e.path + "/"))) closeEditor(true);
      if (selected === e.path) selected = "";
      await reload();
      toast(`Deleted ${e.name}`, "success");
    } catch (err) { toastError(err, "Couldn't delete"); }
  }

  // ---- uploads
  function openUploadMenu(anchor, dir) {
    uploadTarget = dir;
    showMenu(anchor, [
      { label: dir ? `Files into ${dir}/` : "Files", icon: "file", onSelect: () => fileInput.click() },
      { label: dir ? `Folder into ${dir}/` : "Folder", icon: "folder", onSelect: () => dirInput.click() },
    ]);
  }
  const onPicked = (input) => {
    if (!input.files || !input.files.length) return;
    let p;
    try { p = Promise.resolve(fromFileList(input.files)); } catch (e) { p = Promise.reject(e); }
    input.value = "";
    doUpload(p, uploadTarget);
  };
  fileInput.addEventListener("change", () => onPicked(fileInput));
  dirInput.addEventListener("change", () => onPicked(dirInput));

  // drop onto a folder row uploads into it; anywhere else in the tree -> root
  let dragDepth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  treePane.addEventListener("dragenter", (e) => { if (hasFiles(e)) { e.preventDefault(); dragDepth++; treePane.classList.add("dragging"); } });
  treePane.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) treePane.classList.remove("dragging"); });
  treePane.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    for (const x of treeBody.querySelectorAll(".drop-target")) x.classList.remove("drop-target");
    e.target.closest?.('.tree-row[data-dir="1"]')?.classList.add("drop-target");
  });
  treePane.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    treePane.classList.remove("dragging");
    for (const x of treeBody.querySelectorAll(".drop-target")) x.classList.remove("drop-target");
    const dirRow = e.target.closest?.('.tree-row[data-dir="1"]');
    doUpload(fromDrop(e.dataTransfer), dirRow ? dirRow.dataset.path : "");
  });

  async function doUpload(selPromise, dir) {
    if (uploading) { toast("An upload is already running.", "info"); return; }
    let sel;
    try { sel = await selPromise; } catch (e) { toast(e.message, "error"); return; }
    const problem = checkLimits(sel);
    if (problem) { toast(problem, "error"); return; }
    const clash = sel.items.filter((i) => entries.some((x) => x.path === (dir ? `${dir}/${i.path}` : i.path)));
    if (clash.length) {
      const ok = await confirmDialog({ title: "Overwrite files?", confirmText: "Overwrite",
        message: `${plural(clash.length, "file")} already exist${clash.length === 1 ? "s" : ""} and will be replaced: ${clash.slice(0, 5).map((c) => c.path).join(", ")}${clash.length > 5 ? "…" : ""}` });
      if (!ok) return;
    }
    if (!alive) return;
    upBar.el.hidden = false;
    upBar.set(0, 1);
    uploading = upload(`${base}/upload`, toFormData(sel, { dir }), (l, t) => upBar.set(l, t));
    try {
      const r = await uploading.promise;
      if (!alive) return;
      for (const e of r.entries || []) ensureVisible(e.path);
      if (dir) expanded.add(dir);
      toast(`Uploaded ${plural((r.entries || sel.items).length, "file")}${dir ? ` to ${dir}/` : ""}`, "success");
      await reload();
    } catch (e) { if (e.status !== -1) toastError(e, "Upload failed"); }
    finally { uploading = null; upBar.el.hidden = true; }
  }

  // ---- editor
  const gutter = h("pre", { class: "gutter", "aria-hidden": "true" });
  const ta = h("textarea", { class: "code", spellcheck: false, autocapitalize: "off", autocomplete: "off", autocorrect: "off",
    wrap: "off", "aria-label": "File contents", "data-gramm": "false" });
  const pos = h("span", { class: "editor-pos" });
  const editorHead = h("div", { class: "editor-head" });
  const editorBody = h("div", { class: "editor-body" }, gutter, ta);
  const eol = h("span", { title: "Line endings are kept as they were in the file" }, "LF");
  const editorFoot = h("div", { class: "editor-foot" }, h("span", { class: "editor-foot-left" }, pos, eol),
    h("span", { class: "faint" }, "Spaces: 4 · Tab indents · Esc then Tab leaves the editor · Ctrl+S saves"));
  let lineCount = 0;
  let escapeTab = false;
  const saveBtn = h("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => save() }, icon("save"), h("span", { class: "btn-label" }, "Save"));

  function isDirty() { return !!open && !open.binary && ta.value !== open.original; }

  function updateGutter() {
    let n = 1;
    for (let i = ta.value.indexOf("\n"); i !== -1; i = ta.value.indexOf("\n", i + 1)) n++;
    if (n !== lineCount) {
      lineCount = n;
      const parts = new Array(n);
      for (let i = 0; i < n; i++) parts[i] = i + 1;
      gutter.textContent = parts.join("\n") + "\n";
      gutter.style.setProperty("--digits", String(Math.max(2, String(n).length)));
    }
    gutter.scrollTop = ta.scrollTop;
  }
  function updatePos() {
    const before = ta.value.slice(0, ta.selectionStart);
    const line = before.split("\n").length;
    const col = ta.selectionStart - before.lastIndexOf("\n");
    pos.textContent = `Ln ${line}, Col ${col}`;
  }
  function updateDirty() {
    const d = isDirty();
    root.classList.toggle("dirty", d);
    saveBtn.disabled = !d;
    editorHead.querySelector(".dirty-dot")?.toggleAttribute("hidden", !d);
    ctx.setGuard(d ? () => `${open.path} has unsaved changes.` : null);
  }

  function renderEditorHead() {
    const parts = open.path.split("/");
    setChildren(editorHead,
      h("button", { class: "icon-btn editor-back", type: "button", "aria-label": "Back to files", onclick: () => closeEditorGuarded() }, icon("chevronLeft")),
      h("div", { class: "editor-path mono", title: open.path },
        parts.slice(0, -1).map((p) => h("span", { class: "crumb" }, p, h("span", { class: "crumb-sep" }, "/"))),
        h("strong", null, parts[parts.length - 1]),
        h("span", { class: "dirty-dot", hidden: !isDirty(), title: "Unsaved changes" }, "●")),
      h("div", { class: "editor-actions" },
        iconBtn("download", "Download", () => download(open.path)),
        open.binary ? null : saveBtn));
  }

  async function confirmLeaveFile() {
    if (!isDirty()) return true;
    return confirmDialog({ title: "Discard unsaved changes?", message: `${open.path} has unsaved changes.`, confirmText: "Discard", danger: true });
  }

  async function openFile(path, fresh = false) {
    if (open && open.path === path && !fresh) { root.classList.add("show-editor"); ta.focus(); return; }
    if (!(await confirmLeaveFile())) return;
    selected = path;
    renderTree();
    setChildren(editorPane, spinnerBlock(`Opening ${baseName(path)}…`));
    root.classList.add("show-editor");
    try {
      const d = await api(`${base}/content?path=${enc(path)}`);
      if (!alive) return;
      // <textarea> turns CRLF into LF, so compare/edit in LF and restore the file's own line endings on save
      const raw = d.binary ? null : d.content ?? "";
      open = { path, original: raw == null ? null : raw.replace(/\r\n?/g, "\n"), crlf: raw != null && raw.includes("\r\n"),
        binary: !!d.binary, size: d.size };
      eol.textContent = open.crlf ? "CRLF" : "LF";
    } catch (e) {
      if (!alive) return;
      toastError(e, `Couldn't open ${path}`);
      open = null;
      renderEditorEmpty();
      return;
    }
    renderTree();
    renderEditorHead();
    if (open.binary) {
      setChildren(editorPane, editorHead, h("div", { class: "binary-note" },
        h("div", { class: "empty-icon" }, icon("file")),
        h("h3", null, "This file can't be edited here"),
        h("p", { class: "muted" }, `It's a binary file or larger than 2 MB (${fmtBytes(open.size)}).`),
        h("button", { class: "btn btn-primary", type: "button", onclick: () => download(open.path) }, icon("download"), h("span", { class: "btn-label" }, "Download"))));
      ctx.setGuard(null);
      return;
    }
    ta.value = open.original;
    lineCount = 0;
    setChildren(editorPane, editorHead, editorBody, editorFoot);
    updateGutter();
    ta.scrollTop = 0;
    ta.setSelectionRange(0, 0);
    updatePos();
    updateDirty();
    ta.focus({ preventScroll: true });
  }

  function closeEditor(force = false) {
    if (!force && isDirty()) return;
    open = null;
    ta.value = "";
    root.classList.remove("show-editor", "dirty");
    ctx.setGuard(null);
    renderEditorEmpty();
    renderTree();
  }
  async function closeEditorGuarded() { if (await confirmLeaveFile()) closeEditor(true); }

  function renderEditorEmpty() {
    setChildren(editorPane, h("div", { class: "editor-empty" },
      h("div", { class: "empty-icon" }, icon("fileCode")),
      h("h3", null, "Select a file"),
      h("p", { class: "muted" }, "Pick a file on the left to view or edit it. Changes apply after the bot restarts.")));
  }

  async function save() {
    if (!open || open.binary || !isDirty()) return;
    const content = ta.value;
    const body = open.crlf ? content.replace(/\n/g, "\r\n") : content;
    const path = open.path;
    await busy(saveBtn, async () => {
      try {
        const r = await api(`${base}/content`, { method: "PUT", json: { path, content: body } });
        if (!alive || !open || open.path !== path) return;
        open.original = content;
        updateDirty();
        const ent = entries.find((x) => x.path === path);
        if (ent && r && r.entry) { Object.assign(ent, r.entry); renderTree(); }
        toast(`Saved ${baseName(path)}`, "success", { timeout: 2000 });
      } catch (e) { toastError(e, "Couldn't save"); }
    });
    updateDirty();
  }

  // text editing helpers (execCommand keeps the native undo stack working)
  function insert(text) {
    if (!document.execCommand("insertText", false, text)) ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
  }
  function lineStart(i) { return ta.value.lastIndexOf("\n", i - 1) + 1; }

  function indentSelection(outdent) {
    const { selectionStart: s, selectionEnd: e, value } = ta;
    if (!outdent && !value.slice(s, e).includes("\n")) {
      const col = s - lineStart(s);
      insert(" ".repeat(INDENT.length - (col % INDENT.length)));
      return;
    }
    const from = lineStart(s);
    const endAdj = e > s && value[e - 1] === "\n" ? e - 1 : e;
    let to = value.indexOf("\n", endAdj);
    if (to === -1) to = value.length;
    const lines = value.slice(from, to).split("\n");
    const changed = lines.map((l) => (outdent ? l.replace(/^( {1,4}|\t)/, "") : INDENT + l)).join("\n");
    ta.setSelectionRange(from, to);
    insert(changed);
    ta.setSelectionRange(from, from + changed.length);
  }

  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { escapeTab = true; return; }
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (escapeTab) { escapeTab = false; return; } // let focus leave the editor
      e.preventDefault();
      indentSelection(e.shiftKey);
      return;
    }
    escapeTab = false;
    if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing) {
      const s = ta.selectionStart;
      const line = ta.value.slice(lineStart(s), s);
      let indent = (line.match(/^[ \t]*/) || [""])[0];
      if (/:\s*(#.*)?$/.test(line)) indent += INDENT;
      if (indent) { e.preventDefault(); insert("\n" + indent); }
    }
  });
  ta.addEventListener("input", () => { updateGutter(); updateDirty(); updatePos(); });
  ta.addEventListener("scroll", () => { gutter.scrollTop = ta.scrollTop; }, { passive: true });
  ta.addEventListener("keyup", updatePos);
  ta.addEventListener("click", updatePos);

  const onKey = (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (open && !open.binary) save();
    }
  };
  document.addEventListener("keydown", onKey);

  renderEditorEmpty();
  reload();

  return {
    unmount() {
      alive = false;
      document.removeEventListener("keydown", onKey);
      clearTimeout(filterTimer);
      if (uploading) uploading.abort();
      ctx.setGuard(null);
    },
  };
}
