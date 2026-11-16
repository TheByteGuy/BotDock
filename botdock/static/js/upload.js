// Collecting files for upload from <input> pickers and drag & drop (including whole folders),
// applying the server's skip-list client-side so a venv never gets sent.
import { h, icon } from "./dom.js";
import { fmtBytes } from "./ui.js";

export const MAX_FILES = 20000;
export const MAX_BYTES = 300 * 1024 * 1024;
const SKIP_DIRS = new Set(["__pycache__", ".git", ".venv", "venv", "env", "node_modules", ".idea", ".vscode"]);
const SKIP_FILES = new Set([".DS_Store", "Thumbs.db"]);
const ENTRY_PREF = ["main.py", "bot.py", "app.py", "run.py", "start.py", "__main__.py"];

export function isSkipped(path) {
  const parts = path.split("/");
  for (let i = 0; i < parts.length - 1; i++) if (SKIP_DIRS.has(parts[i])) return true;
  const name = parts[parts.length - 1];
  return SKIP_FILES.has(name) || /\.py[co]$/i.test(name);
}

class TooMany extends Error {}

/**
 * Selection = {kind: "py"|"zip"|"folder", items: [{file, path}], skippedFiles, skippedDirs:Set, label}
 */
function makeSelection(items, skippedFiles, skippedDirs) {
  let kind = "folder";
  if (items.length === 1 && !items[0].path.includes("/")) {
    const n = items[0].path.toLowerCase();
    if (n.endsWith(".py")) kind = "py";
    else if (n.endsWith(".zip")) kind = "zip";
  }
  items.sort((a, b) => a.path.localeCompare(b.path));
  const tops = new Set(items.map((i) => i.path.split("/")[0]));
  const label = kind !== "folder" ? items[0].path : (tops.size === 1 && items.every((i) => i.path.includes("/")) ? [...tops][0] : `${items.length} files`);
  return { kind, items, skippedFiles, skippedDirs, label };
}

/** From an <input type=file> (optionally webkitdirectory). */
export function fromFileList(fileList) {
  const items = [];
  let skippedFiles = 0;
  const skippedDirs = new Set();
  for (const file of fileList) {
    const path = (file.webkitRelativePath || file.name).replace(/\\/g, "/").replace(/^\/+/, "");
    if (isSkipped(path)) {
      skippedFiles++;
      const seg = path.split("/").find((p) => SKIP_DIRS.has(p));
      if (seg) skippedDirs.add(seg);
      continue;
    }
    items.push({ file, path });
  }
  if (items.length > MAX_FILES) throw new Error(`Too many files (${items.length.toLocaleString()}); the limit is 20 000.`);
  return makeSelection(items, skippedFiles, skippedDirs);
}

/**
 * From a drop event. MUST be called synchronously inside the drop handler (items are only
 * readable during the event); the traversal itself is async.
 */
export function fromDrop(dataTransfer) {
  const entries = [];
  const loose = [];
  for (const item of dataTransfer.items || []) {
    if (item.kind !== "file") continue;
    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    if (entry) entries.push(entry);
    else { const f = item.getAsFile(); if (f) loose.push(f); }
  }
  if (!entries.length && !loose.length) for (const f of dataTransfer.files || []) loose.push(f);
  return (async () => {
    const items = loose.map((file) => ({ file, path: file.name }));
    const st = { skippedFiles: 0, skippedDirs: new Set(), items };
    try {
      for (const e of entries) await walk(e, "", st);
    } catch (e) {
      if (e instanceof TooMany) throw new Error("That folder has more than 20 000 files (after skipping venvs and caches).");
      throw e;
    }
    return makeSelection(items, st.skippedFiles, st.skippedDirs);
  })();
}

function readAllEntries(reader) {
  // readEntries returns at most ~100 entries per call; keep calling until it returns [].
  return new Promise((resolve, reject) => {
    const all = [];
    const next = () => reader.readEntries((batch) => {
      if (!batch.length) resolve(all);
      else { all.push(...batch); next(); }
    }, reject);
    next();
  });
}

async function walk(entry, prefix, st) {
  if (entry.isFile) {
    const path = prefix + entry.name;
    if (isSkipped(path)) { st.skippedFiles++; return; }
    const file = await new Promise((res, rej) => entry.file(res, rej));
    st.items.push({ file, path });
    if (st.items.length > MAX_FILES) throw new TooMany();
  } else if (entry.isDirectory) {
    if (SKIP_DIRS.has(entry.name)) { st.skippedDirs.add(entry.name); return; }
    const children = await readAllEntries(entry.createReader());
    const base = prefix + entry.name + "/";
    // files of a directory in parallel, sub-directories one after another (bounded concurrency)
    await Promise.all(children.filter((c) => c.isFile).map((c) => walk(c, base, st)));
    for (const c of children) if (c.isDirectory) await walk(c, base, st);
  }
}

/** What the server will see after its "strip a single shared top-level dir" rule. */
export function analyze(sel) {
  let size = 0;
  for (const i of sel.items) size += i.file.size;
  if (sel.kind === "py") {
    return { size, count: 1, rel: [sel.items[0].path], topPy: [sel.items[0].path], hasRequirements: false,
      envItem: null, entry: sel.items[0].path, root: null };
  }
  if (sel.kind === "zip") return { size, count: 1, rel: [], topPy: [], hasRequirements: null, envItem: null, entry: null, root: null };
  const paths = sel.items.map((i) => i.path);
  const tops = new Set(paths.map((p) => p.split("/")[0]));
  const strip = tops.size === 1 && paths.every((p) => p.includes("/"));
  const root = strip ? [...tops][0] : null;
  const rel = strip ? paths.map((p) => p.slice(root.length + 1)) : paths;
  const relSet = new Set(rel);
  const envIdx = rel.indexOf(".env");
  const topPy = rel.filter((p) => !p.includes("/") && p.endsWith(".py"));
  topPy.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  const allPy = rel.filter((p) => p.endsWith(".py"));
  let entry = null;
  if (allPy.length === 1) entry = allPy[0];
  else entry = ENTRY_PREF.find((c) => relSet.has(c)) || (topPy.length === 1 ? topPy[0] : null);
  return { size, count: sel.items.length, rel, topPy, hasRequirements: relSet.has("requirements.txt"),
    envItem: envIdx >= 0 ? sel.items[envIdx] : null, entry, root };
}

function rank(p) { const i = ENTRY_PREF.indexOf(p); return i < 0 ? 99 : i; }

/** Reads a .env File and returns just its key names (values are never shown). */
export async function envKeys(file) {
  if (!file || file.size > 256 * 1024) return [];
  const text = await file.text();
  const keys = [];
  for (let line of text.split(/\r?\n/)) {
    line = line.trim();
    if (!line || line.startsWith("#")) continue;
    if (line.startsWith("export ")) line = line.slice(7);
    const k = line.split("=")[0].trim();
    if (line.includes("=") && /^[A-Za-z_][A-Za-z0-9_]*$/.test(k)) keys.push(k);
  }
  return keys;
}

/** "my_ticket-bot" -> "My Ticket Bot"; mixed-case names are kept as they are. */
export function prettyName(base) {
  base = String(base || "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  if (base === base.toLowerCase()) base = base.replace(/\b\w/g, (c) => c.toUpperCase());
  return base.slice(0, 60);
}

export function suggestName(sel) {
  let base = sel.label;
  if (sel.kind === "py") base = base.replace(/\.py$/i, "");
  else if (sel.kind === "zip") base = base.replace(/\.zip$/i, "").replace(/[-_]?(main|master)$/i, "");
  else if (/^\d+ files$/.test(base)) return "";
  base = base.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  if (base === base.toLowerCase()) base = base.replace(/\b\w/g, (c) => c.toUpperCase());
  return base.slice(0, 60);
}

export function toFormData(sel, fields = {}) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) if (v != null) fd.append(k, v);
  for (const { file, path } of sel.items) fd.append("files", file, path);
  return fd;
}

export function checkLimits(sel) {
  let size = 0;
  for (const i of sel.items) size += i.file.size;
  if (!sel.items.length) return "Nothing to upload: the selection is empty (or only contained skipped folders).";
  if (sel.items.length > MAX_FILES) return `Too many files (${sel.items.length.toLocaleString()}). The limit is 20 000.`;
  if (size > MAX_BYTES) return `The upload is ${fmtBytes(size)}; the limit is 300 MB.`;
  return null;
}

/**
 * A drop zone with "file" and "folder" pickers. onSelect(selectionPromise).
 * accept: what the file picker offers. allowAny: allow arbitrary files (files tab upload).
 */
export function dropZone({ title, hint, onSelect, accept = ".py,.zip", multiple = false, compact = false, allowAny = false }) {
  const fileInput = h("input", { type: "file", class: "sr-only", tabIndex: -1, accept: allowAny ? null : accept, multiple: multiple || allowAny });
  const dirInput = h("input", { type: "file", class: "sr-only", tabIndex: -1 });
  dirInput.webkitdirectory = true;
  const take = (input) => {
    if (!input.files || !input.files.length) return;
    let p;
    try { p = Promise.resolve(fromFileList(input.files)); } catch (e) { p = Promise.reject(e); }
    input.value = "";
    onSelect(p);
  };
  fileInput.addEventListener("change", () => take(fileInput));
  dirInput.addEventListener("change", () => take(dirInput));

  const zone = h("div", { class: `dropzone${compact ? " compact" : ""}` },
    h("div", { class: "dropzone-icon" }, icon("upload")),
    h("div", { class: "dropzone-title" }, title),
    hint ? h("div", { class: "dropzone-hint" }, hint) : null,
    h("div", { class: "dropzone-actions" },
      h("button", { class: "btn", type: "button", onclick: () => fileInput.click() }, icon("file"),
        h("span", null, allowAny ? "Choose files" : "Choose .py or .zip")),
      h("button", { class: "btn", type: "button", onclick: () => dirInput.click() }, icon("folder"), h("span", null, "Choose folder"))),
    fileInput, dirInput);
  attachDrop(zone, onSelect);
  return zone;
}

/** Makes any element a drop target. Adds .dragging while files hover over it. */
export function attachDrop(el, onSelect) {
  let depth = 0;
  const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
  el.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; el.classList.add("dragging"); });
  el.addEventListener("dragover", (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
  el.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) el.classList.remove("dragging"); });
  el.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    depth = 0;
    el.classList.remove("dragging");
    onSelect(fromDrop(e.dataTransfer));
  });
}

// Dropping a file anywhere else must not navigate the browser away from the app.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

/** Progress bar component. */
export function progressBar() {
  const fill = h("div", { class: "progress-fill" });
  const text = h("span", { class: "progress-text" }, "");
  const el = h("div", { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100" },
    h("div", { class: "progress-track" }, fill), text);
  return {
    el,
    set(loaded, total) {
      const pct = total ? Math.min(100, (loaded / total) * 100) : 0;
      fill.style.width = `${pct}%`;
      el.setAttribute("aria-valuenow", String(Math.round(pct)));
      text.textContent = pct >= 100 ? "Processing on server…" : `${fmtBytes(loaded)} of ${fmtBytes(total)} · ${Math.round(pct)}%`;
    },
    reset() { fill.style.width = "0%"; text.textContent = ""; },
  };
}
