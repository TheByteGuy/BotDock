// Console tab: live log via SSE with batched rendering, capped DOM, smart autoscroll, filter, wrap.
import { h, icon, setChildren } from "../dom.js";
import { api, liveStream, enc } from "../api.js";
import { confirmDialog, toastError, toast, iconBtn } from "../ui.js";

const MAX_BUFFER = 5000;   // lines kept in memory (matches the server's tail limit)
const MAX_DOM = 1500;      // lines rendered at once
const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)/g;
const PREF_KEY = "botdock-console-wrap";

function prefWrap() { try { return localStorage.getItem(PREF_KEY) !== "0"; } catch { return true; } }

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

export function mount(panel, ctx) {
  const id = ctx.botId;
  let buffer = [];            // LogLine objects (text already ANSI-stripped)
  let pending = [];           // received but not yet rendered
  let lastSeq = 0;
  let filter = "";
  let rafId = 0;
  let stick = true;           // user is at the bottom -> keep following
  let unseen = 0;
  let stream = null;
  let alive = true;
  let wrap = prefWrap();
  let programmaticScroll = false;

  const out = h("div", { class: `log${wrap ? " wrap" : ""}`, role: "log", "aria-live": "off", tabIndex: 0, "aria-label": "Console output" });
  const topNote = h("div", { class: "log-note", hidden: true });
  const empty = h("div", { class: "log-empty", hidden: true });
  const jump = h("button", { class: "btn btn-sm jump-latest", type: "button", hidden: true, onclick: () => { stick = true; rerenderAll(); updateJump(); } },
    icon("arrowDown"), h("span", null, "Jump to latest"));
  const status = h("span", { class: "live-status" });
  const filterInput = h("input", { class: "input input-sm", type: "search", placeholder: "Filter lines", "aria-label": "Filter log lines",
    autocomplete: "off", spellcheck: false });
  const wrapBtn = h("button", { class: `icon-btn icon-btn-bordered${wrap ? " active" : ""}`, type: "button", "aria-pressed": String(wrap),
    title: "Wrap long lines", "aria-label": "Wrap long lines", onclick: toggleWrap }, icon("wrap"));
  const dl = h("a", { class: "icon-btn icon-btn-bordered", href: `/api/bots/${enc(id)}/logs/download`, download: "", title: "Download full log", "aria-label": "Download full log" }, icon("download"));
  const clearBtn = iconBtn("trash", "Clear log", clearLog, "icon-btn-bordered");

  panel.append(h("div", { class: "console" },
    h("div", { class: "toolbar" },
      h("div", { class: "toolbar-left" }, status),
      h("div", { class: "toolbar-right" },
        h("div", { class: "search-box" }, icon("search"), filterInput), wrapBtn, dl, clearBtn)),
    h("div", { class: "log-wrap" }, out, jump)));
  out.append(topNote, empty);

  // ---- rendering
  function lineEl(l) {
    const d = new Date(l.ts);
    const row = h("div", { class: `ln ln-${l.stream === "stderr" || l.stream === "system" ? l.stream : "stdout"}` });
    row.append(h("span", { class: "ln-ts", title: isNaN(d) ? "" : d.toLocaleString() }, isNaN(d) ? "" : timeFmt.format(d)),
      h("span", { class: "ln-text" }, l.text));
    return row;
  }

  function matches(l) { return !filter || l.lower.includes(filter); }

  function setStatus(s) {
    const label = { connecting: "Connecting…", reconnecting: "Reconnecting…", live: "Live", closed: "Disconnected" }[s] || s;
    setChildren(status, h("span", { class: `live-dot ${s}` }), h("span", null, label));
  }

  function rerenderAll() {
    const rows = [];
    for (let i = buffer.length - 1; i >= 0 && rows.length < MAX_DOM; i--) if (matches(buffer[i])) rows.push(buffer[i]);
    rows.reverse();
    const f = document.createDocumentFragment();
    for (const l of rows) f.append(lineEl(l));
    while (out.lastChild && out.lastChild !== empty) out.lastChild.remove();
    out.append(f);
    updateNotes();
    if (stick) scrollToEnd();
  }

  function updateNotes() {
    const shown = out.childElementCount - 2;
    const total = filter ? buffer.filter(matches).length : buffer.length;
    topNote.hidden = total <= shown;
    topNote.textContent = topNote.hidden ? "" : `Showing the latest ${shown.toLocaleString()} of ${total.toLocaleString()} lines · download the log for everything`;
    empty.hidden = shown > 0;
    empty.textContent = buffer.length ? "No lines match the filter." : "No output yet. Lines appear here live once the bot prints something.";
  }

  function flush() {
    rafId = 0;
    if (!alive || !pending.length) return;
    const batch = pending;
    pending = [];
    if (!stick) {
      // the user is reading history: keep the DOM still and just count; catch up on return
      for (const l of batch) if (matches(l)) unseen++;
      updateJump();
      return;
    }
    const f = document.createDocumentFragment();
    let added = 0;
    // only the newest MAX_DOM lines of a burst can survive the cap anyway
    for (let i = Math.max(0, batch.length - MAX_DOM); i < batch.length; i++) {
      if (matches(batch[i])) { f.append(lineEl(batch[i])); added++; }
    }
    if (added) {
      out.append(f);
      let extra = out.childElementCount - 2 - MAX_DOM;
      while (extra-- > 0) out.children[2].remove();
      if (stick) scrollToEnd(); else { unseen += added; updateJump(); }
    }
    updateNotes();
  }

  function schedule() { if (!rafId) rafId = requestAnimationFrame(flush); }

  function ingest(l) {
    if (!l || typeof l.seq !== "number" || l.seq <= lastSeq) return; // de-dupe across reconnects
    lastSeq = l.seq;
    const text = String(l.text ?? "").replace(ANSI, "");
    const line = { seq: l.seq, ts: l.ts, stream: l.stream, text, lower: text.toLowerCase() };
    buffer.push(line);
    if (buffer.length > MAX_BUFFER * 1.2) buffer = buffer.slice(-MAX_BUFFER);
    pending.push(line);
    if (pending.length > MAX_DOM * 2) pending = pending.slice(-MAX_DOM);
    schedule();
  }

  // ---- scrolling
  function atBottom() { return out.scrollHeight - out.scrollTop - out.clientHeight < 24; }
  function scrollToEnd() {
    programmaticScroll = true;
    out.scrollTop = out.scrollHeight;
    unseen = 0;
    requestAnimationFrame(() => { programmaticScroll = false; });
  }
  function updateJump() {
    jump.hidden = stick;
    jump.querySelector("span").textContent = unseen ? `${unseen > 999 ? "999+" : unseen} new line${unseen === 1 ? "" : "s"}` : "Jump to latest";
  }
  out.addEventListener("scroll", () => {
    if (programmaticScroll) return;
    const was = stick;
    stick = atBottom();
    if (stick && !was && unseen) rerenderAll(); // back at the bottom: show what arrived meanwhile
    if (stick) unseen = 0;
    if (was !== stick || stick) updateJump();
  }, { passive: true });

  // ---- controls
  let filterTimer = 0;
  filterInput.addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { filter = filterInput.value.trim().toLowerCase(); stick = true; rerenderAll(); updateJump(); }, 120);
  });

  function toggleWrap() {
    wrap = !wrap;
    out.classList.toggle("wrap", wrap);
    wrapBtn.classList.toggle("active", wrap);
    wrapBtn.setAttribute("aria-pressed", String(wrap));
    try { localStorage.setItem(PREF_KEY, wrap ? "1" : "0"); } catch { /* ignore */ }
    if (stick) scrollToEnd();
  }

  async function clearLog() {
    const ok = await confirmDialog({ title: "Clear the log?", message: "This deletes the buffered output and the log files on disk for this bot.", confirmText: "Clear log", danger: true });
    if (!ok) return;
    try {
      await api(`/api/bots/${enc(id)}/logs`, { method: "DELETE" });
      buffer = []; pending = [];
      rerenderAll();
      toast("Log cleared", "success");
    } catch (e) { toastError(e, "Couldn't clear the log"); }
  }

  // ---- data
  async function load() {
    setStatus("connecting");
    try {
      const data = await api(`/api/bots/${enc(id)}/logs?tail=${MAX_BUFFER}`);
      if (!alive) return;
      for (const l of data.lines || []) ingest(l);
    } catch (e) {
      if (!alive) return;
      toastError(e, "Couldn't load the log");
    }
    if (!alive) return;
    pending = [];
    rerenderAll();
    stream = liveStream(async (isReconnect) => {
      if (isReconnect && lastSeq) {
        // seq restarts from 0 when BotDock itself restarts: detect that and resync from scratch
        const d = await api(`/api/bots/${enc(id)}/logs?tail=1`);
        const last = d.lines && d.lines[d.lines.length - 1];
        if (!last || last.seq < lastSeq) {
          lastSeq = 0; buffer = []; pending = [];
          if (alive) rerenderAll();
        }
      }
      return `/api/bots/${enc(id)}/logs/stream?after=${lastSeq}`;
    }, {
      onMessage: (e) => { try { ingest(JSON.parse(e.data)); } catch { /* ignore malformed */ } },
      onStatus: (s) => { if (alive) setStatus(s); },
    });
  }
  load();

  return {
    unmount() {
      alive = false;
      if (stream) stream.close();
      cancelAnimationFrame(rafId);
      clearTimeout(filterTimer);
      buffer = []; pending = [];
    },
  };
}
