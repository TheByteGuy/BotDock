// Bot page: live header (state, stats, actions, banners) + Console / Secrets / Files / Settings tabs.
import { h, icon, setChildren } from "../dom.js";
import { api, enc } from "../api.js";
import { getBot, subscribe, upsertBot } from "../store.js";
import { statePill, stateTone, fmtCpu, fmtBytes, uptimeEl, spinnerBlock, toast, emptyState, plural } from "../ui.js";
import { botActions, runAction } from "../botactions.js";
import { isGithub, sourceMark, shortSha, commitUrl, extLink, syncSource } from "../github.js";
import * as consoleTab from "./console.js";
import * as secretsTab from "./secrets.js";
import * as filesTab from "./files.js";
import * as settingsTab from "./settings.js";

const TABS = [
  { id: "console", label: "Console", icon: "terminal", mod: consoleTab },
  { id: "secrets", label: "Secrets", icon: "key", mod: secretsTab },
  { id: "files", label: "Files", icon: "folder", mod: filesTab },
  { id: "settings", label: "Settings", icon: "sliders", mod: settingsTab },
];

export function mount(root, params, ctx) {
  let botId = params.id;
  let tab = null;
  let tabInstance = null;
  let alive = true;
  let deletedByUs = false;

  const head = h("div", { class: "bot-head" });
  const banners = h("div", { class: "banners" });
  const tabbar = h("nav", { class: "tabs", role: "tablist", "aria-label": "Bot sections" });
  const panel = h("section", { class: "tab-panel", role: "tabpanel" });
  const page = h("div", { class: "page page-bot" },
    h("a", { class: "back-link", href: "#/" }, icon("chevronLeft"), h("span", null, "Bots")),
    head, banners, tabbar, panel);
  root.append(page);

  const tabCtx = {
    botId,
    getBot: () => getBot(botId),
    setGuard: ctx.setGuard,
    navigate: ctx.navigate,
    markDeleted: () => { deletedByUs = true; },
  };

  function renderHead(bot) {
    const rt = bot.runtime || {};
    const running = rt.state === "running";
    const meta = [];
    const m = (label, value, cls) => meta.push(h("span", { class: `meta-item ${cls || ""}` }, h("span", { class: "meta-label" }, label), value));
    const src = bot.source || {};
    if (isGithub(bot)) {
      m("Source", sourceMark(src, { link: true, chip: false }), "meta-source");
      m("Commit", src.commit_sha
        ? extLink(commitUrl(src, src.commit_sha), h("span", { class: "chip chip-mono", title: src.commit_message || src.commit_sha }, icon("commit"), shortSha(src.commit_sha)))
        : h("span", { class: "muted" }, "not pulled yet"));
      m("Auto-update", src.auto_update ? h("span", { class: "ok-text" }, "on") : h("span", { class: "muted" }, "off"));
    }
    m("Entrypoint", bot.entrypoint ? h("span", { class: "mono" }, bot.entrypoint) : h("span", { class: "warn-text" }, "not set"));
    if (rt.pid != null && rt.state !== "stopped") m("PID", h("span", { class: "mono" }, String(rt.pid)));
    if (running && rt.started_at) m("Uptime", uptimeEl(rt.started_at));
    if (running) { m("CPU", fmtCpu(rt.cpu_percent)); m("Memory", fmtBytes(rt.memory_bytes)); }
    if (!running && rt.exit_code != null) m("Last exit", h("span", { class: "mono" }, String(rt.exit_code)));
    if (rt.restart_count) m("Auto-restarts", String(rt.restart_count));
    const focusKey = head.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
    setChildren(head,
      h("div", { class: "bot-head-main" },
        h("div", { class: "bot-title-row" }, h("h1", { class: "page-title bot-title" }, bot.name), statePill(bot)),
        bot.description ? h("p", { class: "bot-desc" }, bot.description) : null,
        h("div", { class: "bot-meta" }, meta)),
      h("div", { class: "bot-head-actions" }, botActions(bot)));
    if (focusKey) head.querySelector(`[data-focus-key="${focusKey}"]`)?.focus();
    document.title = `${bot.name} · BotDock`;

    // banners
    const list = [];
    const tone = stateTone(rt);
    if (tone === "backoff") {
      list.push(banner("warn", "alert",
        [h("strong", null, "Crashed. "), `Exited with code ${rt.exit_code ?? "?"}; BotDock will restart it automatically (${plural(rt.restart_count || 0, "restart")} so far).`],
        h("button", { class: "btn btn-sm", type: "button", onclick: () => runAction(botId, "stop") }, "Cancel restart")));
    } else if (rt.state === "crashed") {
      list.push(banner("danger", "alert",
        [h("strong", null, rt.last_error ? "Failed: " : "Crashed. "), rt.last_error || `The process exited with code ${rt.exit_code ?? "?"} and won't be restarted automatically.`],
        tab !== "console" ? h("a", { class: "btn btn-sm", href: `#/bots/${enc(botId)}/console` }, "View console") : null));
    } else if (rt.last_error && rt.state === "stopped") {
      list.push(banner("danger", "alert", [h("strong", null, "Last error: "), rt.last_error]));
    }
    if (isGithub(bot)) {
      const settingsLink = tab !== "settings" ? h("a", { class: "btn btn-sm", href: `#/bots/${enc(botId)}/settings` }, "Source settings") : null;
      if (src.syncing) {
        list.push(banner("info", "github", [h("strong", null, "Pulling from GitHub… "), `${src.repo}@${src.branch}. The bot restarts by itself if it's running.`],
          h("span", { class: "spinner spinner-sm" })));
      } else if (src.last_error) {
        list.push(banner("warn", "github", [/^github\b/i.test(src.last_error) ? null : h("strong", null, "GitHub: "), src.last_error], settingsLink));
      } else if (src.update_available) {
        const pull = h("button", { class: "btn btn-sm btn-primary", type: "button" }, icon("cloudDown"), h("span", { class: "btn-label" }, "Pull now"));
        pull.addEventListener("click", () => syncSource(botId, pull));
        list.push(banner("info", "cloudDown", src.commit_sha
          ? [h("strong", null, "New commit on GitHub: "), h("span", { class: "mono" }, shortSha(src.latest_sha)),
            src.auto_update ? " — auto-update will pull it at the next check." : ` is the newest commit on ${src.branch}.`]
          : [h("strong", null, "Linked, not pulled yet. "), `Nothing has changed in this bot's files. Pull when you're ready to switch to ${src.repo}@${src.branch}.`], pull));
      }
    }
    if (bot.needs_restart) {
      list.push(banner("info", "restart", "Changes are waiting: restart to apply new code, settings or secrets.",
        h("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => runAction(botId, "restart") }, "Restart now")));
    }
    if (!bot.entrypoint) {
      list.push(banner("warn", "alert", "No entrypoint set. Choose which file BotDock should run.",
        tab !== "settings" ? h("a", { class: "btn btn-sm", href: `#/bots/${enc(botId)}/settings` }, "Open settings") : null));
    }
    setChildren(banners, list);
    renderTabs(bot);
  }

  function banner(kind, iconName, content, action) {
    return h("div", { class: `banner banner-${kind}`, role: kind === "danger" ? "alert" : "status" },
      icon(iconName), h("div", { class: "banner-text" }, content), action ? h("div", { class: "banner-action" }, action) : null);
  }

  function renderTabs(bot) {
    setChildren(tabbar, TABS.map((t) => {
      let count = null;
      if (t.id === "secrets" && bot && bot.env_count) count = bot.env_count;
      if (t.id === "files" && bot && bot.file_count) count = bot.file_count;
      return h("a", { class: `tab${t.id === tab ? " active" : ""}`, role: "tab", "aria-selected": t.id === tab ? "true" : "false",
        href: `#/bots/${enc(botId)}/${t.id}` }, icon(t.icon), h("span", null, t.label),
      count != null ? h("span", { class: "tab-count" }, count > 9999 ? "9999+" : String(count)) : null);
    }));
  }

  function showTab(id) {
    if (tab === id && tabInstance) return;
    if (tabInstance && tabInstance.unmount) tabInstance.unmount();
    tabInstance = null;
    ctx.setGuard(null);
    tab = id;
    setChildren(panel);
    panel.dataset.tab = id;
    const bot = getBot(botId);
    if (bot) renderHead(bot); else renderTabs(null);
    const def = TABS.find((t) => t.id === id);
    tabInstance = def.mod.mount(panel, tabCtx) || {};
  }

  const unsub = subscribe((kind, payload) => {
    if (!alive) return;
    if (kind === "deleted" && payload.id === botId) {
      if (!deletedByUs) toast("This bot was deleted.", "info");
      ctx.navigate("#/", { replace: true });
      return;
    }
    const bot = getBot(botId);
    if (bot && (kind === "reset" || payload?.id === botId)) {
      renderHead(bot);
      if (tabInstance && tabInstance.onBot) tabInstance.onBot(bot);
    }
  });

  async function start() {
    let bot = getBot(botId);
    if (!bot) {
      setChildren(head, spinnerBlock("Loading bot…"));
      try {
        const data = await api(`/api/bots/${enc(botId)}`);
        if (!alive) return;
        bot = data.bot;
        upsertBot(bot);
      } catch (e) {
        if (!alive) return;
        setChildren(page, emptyState({ iconName: e.status === 404 ? "search" : "alert",
          title: e.status === 404 ? "Bot not found" : "Couldn't load this bot",
          text: e.status === 404 ? "It may have been deleted." : e.message,
          action: h("a", { class: "btn", href: "#/" }, "Back to all bots") }));
        return;
      }
    }
    renderHead(bot);
    showTab(params.tab);
  }
  start();

  return {
    update(p) {
      if (p.id !== botId) return false; // different bot -> full remount
      if (getBot(botId)) showTab(p.tab);
      return true;
    },
    unmount() {
      alive = false;
      unsub();
      if (tabInstance && tabInstance.unmount) tabInstance.unmount();
      tabInstance = null;
      document.title = "BotDock";
    },
  };
}
