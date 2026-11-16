// Start / stop / restart buttons shared by dashboard cards and the bot page header.
import { h, icon } from "./dom.js";
import { allowedActions, toastError, toast } from "./ui.js";
import { botAction, getBot, upsertBot } from "./store.js";

const inflight = new Map(); // botId -> action name

const LABEL = { start: "Start", stop: "Stop", restart: "Restart" };
const ICON = { start: "play", stop: "stop", restart: "restart" };

export async function runAction(botId, action) {
  if (inflight.has(botId)) return;
  inflight.set(botId, action);
  const b = getBot(botId);
  if (b) upsertBot(b); // re-render with the spinner
  try {
    await botAction(botId, action);
  } catch (e) {
    if (e.status === 409 && action === "start" && /entrypoint/i.test(e.message)) {
      toast("This bot has no entrypoint yet. Pick the file to run in Settings.", "error");
    } else toastError(e, `Couldn't ${action}`);
  } finally {
    inflight.delete(botId);
    const cur = getBot(botId);
    if (cur) upsertBot(cur);
  }
}

/**
 * compact: icon-only buttons (dashboard). Otherwise labelled buttons; only the relevant ones shown.
 */
export function botActions(bot, { compact = false } = {}) {
  const can = allowedActions(bot);
  const busyAction = inflight.get(bot.id);
  const anyBusy = !!busyAction;
  const make = (action, cls) => {
    const isBusy = busyAction === action;
    const b = h("button", {
      class: `${compact ? "icon-btn icon-btn-bordered" : "btn"} ${cls || ""} ${isBusy ? "is-busy" : ""}`.trim(),
      type: "button",
      disabled: anyBusy || !can[action],
      "aria-label": `${LABEL[action]} ${bot.name}`,
      title: !bot.entrypoint && action !== "stop" ? "Set an entrypoint in Settings first" : LABEL[action],
      dataset: { focusKey: `act-${action}` },
      "aria-busy": isBusy ? "true" : null,
      onclick: (e) => { e.preventDefault(); e.stopPropagation(); runAction(bot.id, action); },
    }, icon(ICON[action]), compact ? null : h("span", { class: "btn-label" }, LABEL[action]));
    return b;
  };
  if (compact) {
    return h("div", { class: "actions-compact" }, make("start", "act-start"), make("restart"), make("stop", "act-stop"));
  }
  const list = [];
  if (can.active) list.push(make("restart"), make("stop", "btn-danger-soft"));
  else list.push(make("start", "btn-primary"));
  return h("div", { class: "actions" }, list);
}
