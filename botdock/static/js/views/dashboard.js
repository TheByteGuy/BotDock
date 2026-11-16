// Dashboard: live grid of bots + system footer.
import { h, icon, setChildren } from "../dom.js";
import { api } from "../api.js";
import { allBots, isLoaded, subscribe, loadBots } from "../store.js";
import { statePill, stateTone, fmtCpu, fmtBytes, uptimeEl, spinnerBlock, toastError, plural, fmtDuration } from "../ui.js";
import { botActions } from "../botactions.js";
import { dropZone } from "../upload.js";
import { setPendingSelection } from "./newbot.js";
import { isGithub, sourceMark, shortSha } from "../github.js";

export function mount(root, _params, { navigate }) {
  const cards = new Map(); // id -> element
  const summary = h("p", { class: "page-sub" });
  const grid = h("div", { class: "bot-grid" });
  const body = h("div", null, spinnerBlock("Loading bots…"));
  const footer = h("footer", { class: "sys-footer" });
  const newBtn = h("a", { class: "btn btn-primary", href: "#/new" }, icon("plus"), h("span", { class: "btn-label" }, "New bot"));

  root.append(h("div", { class: "page" },
    h("div", { class: "page-head" }, h("div", null, h("h1", { class: "page-title" }, "Bots"), summary), newBtn),
    body, footer));

  function cardFor(bot) {
    const rt = bot.runtime || {};
    const running = rt.state === "running";
    const tone = stateTone(rt);
    const stat = (label, value) => h("div", { class: "stat" }, h("dt", null, label), h("dd", null, value));
    let note = null;
    if (rt.last_error && (rt.state === "crashed" || rt.state === "stopped")) {
      note = h("div", { class: "card-note note-danger" }, icon("alert"), h("span", null, rt.last_error));
    } else if (tone === "backoff") {
      note = h("div", { class: "card-note note-warn" }, icon("alert"),
        h("span", null, `Exited with code ${rt.exit_code ?? "?"} · ${plural(rt.restart_count || 0, "restart")} so far`));
    } else if (rt.state === "crashed") {
      note = h("div", { class: "card-note note-danger" }, icon("alert"), h("span", null, `Exited with code ${rt.exit_code ?? "?"}`));
    } else if (isGithub(bot) && bot.source.syncing) {
      note = h("div", { class: "card-note note-info" }, h("span", { class: "spinner spinner-xs" }), h("span", null, "Pulling update from GitHub…"));
    } else if (isGithub(bot) && bot.source.last_error) {
      note = h("div", { class: "card-note note-warn", title: bot.source.last_error }, icon("github"), h("span", null, bot.source.last_error));
    } else if (isGithub(bot) && bot.source.update_available) {
      note = h("div", { class: "card-note note-info", title: `Newest commit ${shortSha(bot.source.latest_sha)}` }, icon("cloudDown"),
        h("span", null, !bot.source.commit_sha ? "Linked · not pulled yet" : bot.source.auto_update ? "Update available · pulling soon" : "Update available"));
    } else if (bot.needs_restart) {
      note = h("div", { class: "card-note note-info" }, icon("restart"), h("span", null, "Restart to apply changes"));
    } else if (!bot.entrypoint) {
      note = h("div", { class: "card-note note-warn" }, icon("alert"), h("span", null, "No entrypoint set"));
    }
    return h("article", { class: `bot-card tone-${tone}`, dataset: { id: bot.id, name: bot.name } },
      h("div", { class: "bot-card-top" },
        h("a", { class: "bot-card-name stretched", href: `#/bots/${encodeURIComponent(bot.id)}/console`, dataset: { focusKey: "name" } }, bot.name),
        statePill(bot)),
      h("p", { class: "bot-card-desc" }, bot.description || h("span", { class: "mono faint" }, bot.entrypoint || "no entrypoint")),
      isGithub(bot) ? h("div", { class: "bot-card-source" }, sourceMark(bot.source)) : null,
      h("dl", { class: "bot-card-stats" },
        stat("Uptime", running && rt.started_at ? uptimeEl(rt.started_at) : "–"),
        stat("CPU", running ? fmtCpu(rt.cpu_percent) : "–"),
        stat("Memory", running ? fmtBytes(rt.memory_bytes) : "–")),
      h("div", { class: "bot-card-foot" }, note || h("span", { class: "card-note faint" }, `${plural(bot.file_count || 0, "file")} · ${fmtBytes(bot.size_bytes)}`),
        botActions(bot, { compact: true })));
  }

  function renderSummary(list) {
    const running = list.filter((b) => b.runtime?.state === "running").length;
    const crashed = list.filter((b) => b.runtime?.state === "crashed").length;
    const parts = [`${running} running`, `${list.length} total`];
    if (crashed) parts.splice(1, 0, `${crashed} crashed`);
    summary.textContent = list.length ? parts.join(" · ") : "Nothing hosted yet";
  }

  function renderEmpty() {
    setChildren(body, h("div", { class: "empty-hero" },
      dropZone({
        title: "Add your first bot",
        hint: "Drop a .py file, a bot folder or a .zip here. Requirements and .env files are picked up automatically.",
        onSelect: (p) => { setPendingSelection(p); navigate("#/new"); },
      })));
  }

  function renderAll() {
    if (!isLoaded()) return;
    const list = allBots();
    renderSummary(list);
    newBtn.hidden = !list.length;
    if (!list.length) { cards.clear(); renderEmpty(); return; }
    if (grid.parentNode !== body) setChildren(body, grid);
    cards.clear();
    setChildren(grid, list.map((b) => { const c = cardFor(b); cards.set(b.id, c); return c; }));
  }

  function updateOne(bot) {
    const old = cards.get(bot.id);
    if (!old) { renderAll(); return; }
    const focusKey = old.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
    const fresh = cardFor(bot);
    old.replaceWith(fresh);
    cards.set(bot.id, fresh);
    if (focusKey) fresh.querySelector(`[data-focus-key="${focusKey}"]`)?.focus();
    renderSummary(allBots());
  }

  const unsub = subscribe((kind, payload) => {
    if (kind === "bot" && cards.has(payload.id) && cards.get(payload.id).dataset.name === payload.name) updateOne(payload);
    else renderAll(); // added, removed, renamed (order may change) or reset
  });

  async function loadSystem() {
    try {
      const s = await api("/api/system");
      const used = s.disk ? s.disk.total_bytes - s.disk.free_bytes : 0;
      const pct = s.disk && s.disk.total_bytes ? Math.round((used / s.disk.total_bytes) * 100) : 0;
      setChildren(footer,
        h("span", { class: "sys-item" }, icon("server"), h("span", null, `BotDock ${s.version}`)),
        h("span", { class: "sys-item" }, h("span", null, `Python ${s.python_version}`)),
        s.disk ? h("span", { class: "sys-item sys-disk", title: `${pct}% used` },
          h("span", { class: "disk-bar" }, h("span", { class: "disk-fill", style: { width: `${pct}%` } })),
          h("span", null, `${fmtBytes(s.disk.free_bytes)} free of ${fmtBytes(s.disk.total_bytes)}`)) : null,
        h("span", { class: "sys-item" }, h("span", null, `Up ${fmtDuration(s.uptime_seconds)}`)));
    } catch (e) { if (e.status !== 401) setChildren(footer, h("span", { class: "faint" }, "System info unavailable")); }
  }

  if (isLoaded()) renderAll();
  else loadBots().catch((e) => {
    toastError(e, "Couldn't load bots");
    setChildren(body, h("div", { class: "empty" }, h("h3", null, "Couldn't load bots"), h("p", null, e.message),
      h("button", { class: "btn", type: "button", onclick: () => loadBots().catch((e2) => toastError(e2)) }, "Try again")));
  });
  loadSystem();
  const sysTimer = setInterval(loadSystem, 60000);

  return {
    unmount() { unsub(); clearInterval(sysTimer); },
  };
}
