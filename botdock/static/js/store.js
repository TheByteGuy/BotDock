// Client-side bot state, kept live by GET /api/bots + the /api/events SSE stream.
import { api, post, liveStream, enc } from "./api.js";

const bots = new Map();
const listeners = new Set();
let events = null;
let loaded = false;
let connection = "idle";
const connListeners = new Set();

export function getBot(id) { return bots.get(id) || null; }
export function isLoaded() { return loaded; }
export function allBots() {
  return [...bots.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}

/** fn(kind, payload): kind = "reset" | "bot" | "deleted" */
export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function emit(kind, payload) { for (const fn of [...listeners]) fn(kind, payload); }

export function onConnection(fn) { connListeners.add(fn); fn(connection); return () => connListeners.delete(fn); }
function setConnection(s) { connection = s; for (const fn of connListeners) fn(s); }

export async function loadBots() {
  const data = await api("/api/bots");
  bots.clear();
  for (const b of data.bots || []) bots.set(b.id, b);
  loaded = true;
  emit("reset");
}

export function upsertBot(bot) {
  if (!bot || !bot.id) return;
  bots.set(bot.id, bot);
  emit("bot", bot);
}

export function removeBot(id) {
  if (bots.delete(id)) emit("deleted", { id });
}

export function startEvents() {
  if (events) return;
  events = liveStream(() => "/api/events", {
    onEvent: {
      bot: (e) => { try { upsertBot(JSON.parse(e.data)); } catch { /* ignore malformed */ } },
      bot_deleted: (e) => { try { removeBot(JSON.parse(e.data).id); } catch { /* ignore */ } },
    },
    onStatus: setConnection,
    // events may have been missed while disconnected: resync the full list
    onReconnect: () => loadBots().catch(() => {}),
  });
}

export function stopEvents() {
  if (events) events.close();
  events = null;
  bots.clear();
  loaded = false;
  setConnection("idle");
}

/** POST /api/bots/{id}/{action}; the response bot is applied immediately. */
export async function botAction(id, action) {
  const data = await post(`/api/bots/${enc(id)}/${action}`);
  if (data && data.bot) upsertBot(data.bot);
  return data;
}

export async function patchBot(id, changes) {
  const data = await api(`/api/bots/${enc(id)}`, { method: "PATCH", json: changes });
  if (data && data.bot) upsertBot(data.bot);
  return data.bot;
}

export async function refreshBot(id) {
  const data = await api(`/api/bots/${enc(id)}`);
  upsertBot(data.bot);
  return data.bot;
}
