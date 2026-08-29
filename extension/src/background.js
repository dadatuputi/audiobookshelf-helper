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
const HOOK_ID = "absh-page-hook";

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

  const ids = [SCRIPT_ID, HOOK_ID];
  const existing = await browser.scripting.getRegisteredContentScripts({ ids })
    .catch(() => []);
  if (existing.length) {
    await browser.scripting.unregisterContentScripts(
      { ids: existing.map((s) => s.id) }).catch(() => {});
  }
  if (!pattern || !(await hasHostPermission(c.absUrl))) return;

  // Registered one at a time, deliberately. A single call is atomic: if the
  // browser rejects the MAIN-world hook - an older engine, a tightened policy -
  // it rejects the whole array and *neither* script registers, so the page gets
  // no button and no badges and nothing says why. The hook is an enhancement;
  // the content script is the feature. Losing the first must not cost the
  // second.
  const problems = [];

  // The content script first: it is the part the user can see.
  try {
    await browser.scripting.registerContentScripts([{
      id: SCRIPT_ID,
      matches: [pattern],
      js: ["browser-polyfill.js", "content.js"],
      css: ["content.css"],
      runAt: "document_idle",
      persistAcrossSessions: true
    }]);
  } catch (e) {
    problems.push(`content script: ${e && e.message ? e.message : e}`);
  }

  // Runs in the page's own world so it can see the API responses
  // Audiobookshelf fetched - the only reliable source of the library item id
  // for a rendered card. document_start, or the app has already made its first
  // request before we are watching.
  try {
    await browser.scripting.registerContentScripts([{
      id: HOOK_ID,
      matches: [pattern],
      js: ["page-hook.js"],
      runAt: "document_start",
      world: "MAIN",
      persistAcrossSessions: true
    }]);
  } catch (e) {
    problems.push(`page hook: ${e && e.message ? e.message : e}`);
  }

  // Recorded rather than logged. console.warn goes to the background console,
  // which nobody opens; the options page reads this and says so out loud.
  await browser.storage.local.set({
    registrationError: problems.length ? problems.join("; ") : "",
    registeredPattern: problems.length ? "" : pattern
  }).catch(() => {});
}

/* ------------------------------------------------------------------- API
 *
 * The helper talks to Audiobookshelf itself now - the same code path `absh` on
 * the command line uses. The extension passes its settings down and gets an
 * answer, rather than reimplementing the client in JavaScript.
 */
async function call(cmd, extra, onProgress) {
  const c = await cfg();
  return native({ cmd, ...c, ...(extra || {}) }, onProgress);
}

async function ping() {
  try {
    return { ok: true, ...(await call("ping")) };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

/* --------------------------------------------------------------- routing */
async function route(msg, onProgress) {
  switch (msg && msg.type) {
    case "libraries": return { ok: true, data: (await call("libraries")).libraries };
    case "folders":   return { ok: true, data: (await call("folders", { libraryId: msg.libraryId })).folders };
    case "devices":   return { ok: true, data: (await call("devices")).devices };
    case "status":    return { ok: true, data: await call("status", { libraryId: msg.libraryId, readTags: msg.readTags !== false }) };
    case "pull":      return { ok: true, data: await call("pull", { ids: msg.ids, libraryId: msg.libraryId }, onProgress) };
    case "push":      return { ok: true, data: await call("push", { names: msg.names, libraryId: msg.libraryId, folderId: msg.folderId }, onProgress) };
    case "remove":    return { ok: true, data: await call("remove", { names: msg.names }) };
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
    const streams = msg && (msg.type === "pull" || msg.type === "push");
    const onProgress = streams ? (ev) => reply({ ok: true, progress: ev }) : null;
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
