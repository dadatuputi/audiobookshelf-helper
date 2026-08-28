/* Audiobookshelf Helper - background
 *
 * The extension itself CANNOT write to a USB device. Two hard limits:
 *   - downloads.download({filename}) rejects absolute paths and "../", so
 *     everything would land under the Downloads folder;
 *   - Firefox does not implement showDirectoryPicker().
 * So the filesystem work is delegated to the native messaging host, and this
 * script is only an API client plus a bridge.
 *
 * Pure logic lives in lib.js (globalThis.ABSH) so it can be unit tested
 * without a browser.
 */

const HOST = (globalThis.ABSH_CONFIG || {}).hostName;
const SCRIPT_ID = "absh-content";

/* ------------------------------------------------------------------ native
 *
 * A long-lived port rather than sendNativeMessage, for two reasons: syncing a
 * few hundred books needs progress reporting, and sendNativeMessage reads
 * exactly one reply so it cannot carry progress at all. Requests are tagged
 * with an id because one port carries every in-flight command.
 */
let PORT = null;
let RID = 0;
const PENDING = new Map();

function port() {
  if (PORT) return PORT;
  PORT = browser.runtime.connectNative(HOST);
  PORT.onMessage.addListener((msg) => {
    const p = PENDING.get(msg && msg.rid);
    if (!p) return;
    if (msg.event === "done") {
      PENDING.delete(msg.rid);
      // The host reports a refused command (device not mounted, bad name) as
      // ok:false rather than by failing. Turning that into a rejection here is
      // what stops the popup treating "device not mounted" as an empty shelf.
      if (msg.ok === false) p.reject(new Error(msg.error || "native helper refused the request"));
      else p.resolve(msg);
    } else if (p.onProgress) {
      p.onProgress(msg);
    }
  });
  PORT.onDisconnect.addListener(() => {
    const err = (browser.runtime.lastError && browser.runtime.lastError.message) ||
                "native helper disconnected";
    PORT = null;
    for (const [, p] of PENDING) p.reject(new Error(err));
    PENDING.clear();
  });
  return PORT;
}

function native(payload, onProgress) {
  return new Promise((resolve, reject) => {
    const rid = ++RID;
    PENDING.set(rid, { resolve, reject, onProgress });
    try {
      port().postMessage({ ...payload, rid, progress: !!onProgress });
    } catch (e) {
      PENDING.delete(rid);
      reject(e);
    }
  });
}

/* --------------------------------------------------------------- settings */
async function cfg() {
  return browser.storage.local.get(ABSH.DEFAULTS);
}

async function hasHostPermission(absUrl) {
  try {
    return await browser.permissions.contains({ origins: [ABSH.originPattern(absUrl)] });
  } catch {
    return false;
  }
}

/* The toolbar button is registered at runtime for the one server the user
 * configured, instead of shipping a content script matching every site. */
async function syncContentScript() {
  if (!browser.scripting || !browser.scripting.registerContentScripts) return;
  const c = await cfg();
  let pattern = null;
  try {
    pattern = c.absUrl ? ABSH.libraryPattern(c.absUrl) : null;
  } catch {
    pattern = null;
  }

  const existing = await browser.scripting.getRegisteredContentScripts({ ids: [SCRIPT_ID] })
    .catch(() => []);
  if (existing.length) {
    await browser.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] }).catch(() => {});
  }
  if (!pattern || !(await hasHostPermission(c.absUrl))) return;

  await browser.scripting.registerContentScripts([{
    id: SCRIPT_ID,
    matches: [pattern],
    js: ["browser-polyfill.js", "content.js"],
    css: ["content.css"],
    runAt: "document_idle",
    persistAcrossSessions: true
  }]).catch((e) => console.warn("content script registration failed", e));
}

/* ------------------------------------------------------------------- API */
async function api(path) {
  const c = await cfg();
  if (!c.absUrl || !c.apiKey) {
    throw new Error("Set the server URL and API key in the extension options.");
  }
  if (!(await hasHostPermission(c.absUrl))) {
    throw new Error("Access to your Audiobookshelf server has not been granted yet. " +
                    "Open the options page and press Grant access.");
  }
  const r = await fetch(ABSH.baseUrl(c.absUrl) + path, {
    headers: { Authorization: "Bearer " + c.apiKey }
  });
  if (!r.ok) throw new Error(`Audiobookshelf ${path} responded ${r.status}`);
  return r.json();
}

async function listLibraries() {
  const d = await api("/api/libraries");
  const libs = Array.isArray(d) ? d : (d.libraries || []);
  return libs.filter(l => l.mediaType === "book").map(l => ({ id: l.id, name: l.name }));
}

async function listBooks(libraryId) {
  const c = await cfg();
  const id = libraryId || c.libraryId;
  if (!id) throw new Error("No library selected.");
  const d = await api(`/api/libraries/${encodeURIComponent(id)}/items?limit=0`);
  return ABSH.sortBooks((d.results || []).map(ABSH.normalizeBook));
}

async function sync(items, onProgress) {
  const c = await cfg();
  return native(ABSH.buildSyncPayload(c, items), onProgress);
}

async function listDevice(items) {
  const c = await cfg();
  return native(ABSH.buildListPayload(c, items));
}

async function removeFromDevice(names) {
  const c = await cfg();
  return native(ABSH.buildRemovePayload(c, names));
}

async function ping() {
  try {
    const r = await native({ cmd: "ping" });
    return { ok: true, ...r };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

/* --------------------------------------------------------------- routing */
async function route(msg, onProgress) {
  switch (msg && msg.type) {
    case "libraries": return { ok: true, data: await listLibraries() };
    case "books":     return { ok: true, data: await listBooks(msg.libraryId) };
    case "sync":      return { ok: true, data: await sync(msg.items, onProgress) };
    case "listDevice": return { ok: true, data: await listDevice(msg.items) };
    case "remove":    return { ok: true, data: await removeFromDevice(msg.names) };
    case "ping":      return { ok: true, data: await ping() };
    case "permissionChanged":
      await syncContentScript();
      return { ok: true };
    case "openPicker":
      if (browser.action && browser.action.openPopup) {
        await Promise.resolve(browser.action.openPopup()).catch(() => {});
      }
      return { ok: true };
    default:
      return { ok: false, error: `unknown message type ${msg && msg.type}` };
  }
}

/* Chrome does NOT support returning a promise from an onMessage listener - the
 * sender just receives undefined. Firefox does. Answering through
 * sendResponse and returning true works in both. */
browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  route(msg).then(
    (r) => sendResponse(r),
    (e) => sendResponse({ ok: false, error: String((e && e.message) || e) })
  );
  return true;
});

/* The popup uses a port so that sync progress can stream back to it. */
browser.runtime.onConnect.addListener((p) => {
  if (p.name !== "absh") return;
  p.onMessage.addListener(async (msg) => {
    const reply = (body) => {
      try { p.postMessage({ ...body, rid: msg && msg.rid }); } catch { /* popup closed */ }
    };
    const onProgress = msg && msg.type === "sync"
      ? (ev) => reply({ ok: true, progress: ev })
      : null;
    try {
      reply(await route(msg, onProgress));
    } catch (e) {
      reply({ ok: false, error: String((e && e.message) || e) });
    }
  });
});

browser.runtime.onInstalled.addListener(() => { syncContentScript(); });
if (browser.runtime.onStartup) {
  browser.runtime.onStartup.addListener(() => { syncContentScript(); });
}
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.absUrl) syncContentScript();
});
