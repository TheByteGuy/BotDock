// HTTP layer: JSON fetch wrapper, XHR uploads with progress, resilient EventSource.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

let unauthorizedHandler = () => {};
/** Called (once per burst) whenever an authenticated call comes back 401. */
export function onUnauthorized(fn) { unauthorizedHandler = fn; }

// Auth endpoints answer 401 for "wrong password" -- that must not bounce the user to login.
const NO_REDIRECT = ["/api/auth/login", "/api/auth/setup", "/api/auth/password", "/api/auth/state"];

function detailOf(data, fallback) {
  const d = data && data.detail;
  if (typeof d === "string" && d) return d;
  if (Array.isArray(d) && d.length) {
    // FastAPI validation errors: [{loc, msg, type}]
    return d.map((e) => (e && e.msg) || String(e)).join("; ");
  }
  return fallback;
}

function statusText(status) {
  return ({ 400: "Invalid request", 403: "Request refused", 404: "Not found",
    409: "Conflict", 413: "Upload too large", 429: "Too many attempts, try again later",
    500: "Server error", 502: "BotDock is not reachable", 503: "BotDock is unavailable" })[status] || `HTTP ${status}`;
}

/**
 * api("/api/bots/x", {method: "PATCH", json: {...}}) -> parsed JSON (or null)
 */
export async function api(path, { method = "GET", json, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET" && method !== "HEAD") headers["X-BotDock"] = "1";
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body, signal, credentials: "same-origin", cache: "no-store" });
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new ApiError(0, "Can't reach BotDock. Check that the server is running.");
  }
  let data = null;
  if ((res.headers.get("content-type") || "").includes("json")) data = await res.json().catch(() => null);
  if (res.status === 401 && !NO_REDIRECT.includes(path)) unauthorizedHandler();
  if (!res.ok) throw new ApiError(res.status, detailOf(data, statusText(res.status)));
  return data;
}

/** Shorthand for mutating calls. */
export const post = (path, json) => api(path, { method: "POST", json });

/**
 * Multipart upload with progress. Returns {promise, abort}.
 * onProgress(loaded, total) fires during the request body upload.
 */
export function upload(path, formData, onProgress) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    xhr.open("POST", path);
    xhr.setRequestHeader("X-BotDock", "1");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.addEventListener("progress", (e) => {
      if (onProgress && e.lengthComputable) onProgress(e.loaded, e.total);
    });
    xhr.addEventListener("load", () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not json */ }
      if (xhr.status === 401) unauthorizedHandler();
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError(xhr.status, detailOf(data, statusText(xhr.status))));
    });
    xhr.addEventListener("error", () => reject(new ApiError(0, "Upload failed: connection lost")));
    xhr.addEventListener("abort", () => reject(new ApiError(-1, "Upload cancelled")));
    xhr.send(formData);
  });
  return { promise, abort: () => xhr.abort() };
}

/**
 * EventSource that survives drops: on error it closes, asks /api/auth/state whether we're still
 * logged in (401 -> unauthorized handler, no reconnect loop), then reconnects with backoff.
 * getUrl(isReconnect) is called (and awaited) on each (re)connect so callers can add ?after=<seq>.
 */
export function liveStream(getUrl, { onMessage, onEvent = {}, onStatus = () => {}, onReconnect } = {}) {
  let es = null;
  let closed = false;
  let timer = null;
  let attempt = 0;
  let everOpened = false;

  async function connect() {
    if (closed) return;
    onStatus(attempt ? "reconnecting" : "connecting");
    let url;
    try { url = await getUrl(attempt > 0); } catch { url = null; }
    if (closed) return;
    if (!url) { attempt++; timer = setTimeout(connect, 3000); return; }
    es = new EventSource(url);
    es.addEventListener("open", () => {
      if (everOpened && onReconnect) onReconnect();
      everOpened = true;
      attempt = 0;
      onStatus("live");
    });
    if (onMessage) es.addEventListener("message", (e) => onMessage(e));
    for (const [name, fn] of Object.entries(onEvent)) es.addEventListener(name, fn);
    es.addEventListener("error", async () => {
      if (closed) return;
      es.close();
      es = null;
      attempt++;
      onStatus("reconnecting");
      try {
        const st = await api("/api/auth/state");
        if (!st.authenticated) {
          closed = true;
          onStatus("closed");
          unauthorizedHandler();
          return;
        }
      } catch { /* server down: keep retrying with backoff */ }
      if (closed) return;
      const delay = Math.min(15000, 1000 * 2 ** Math.min(attempt - 1, 4)) + Math.random() * 500;
      timer = setTimeout(connect, delay);
    });
  }
  connect();
  return {
    close() {
      closed = true;
      clearTimeout(timer);
      if (es) es.close();
      es = null;
    },
  };
}

export const enc = encodeURIComponent;
