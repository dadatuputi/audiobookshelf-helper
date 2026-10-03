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
/* The answer to this connection's watch request. Its `polls` says whether the
 * helper is told about devices by the OS or has to keep looking, which the
 * options page says out loud - so it is kept rather than dropped. */
let WATCH = null;

function port() {
  if (PORT) return PORT;
  const self = browser.runtime.connectNative(HOST);
  PORT = self;
  PORT.onMessage.addListener((msg) => {
    // The helper speaks unprompted too: it watches for volumes appearing and
    // disappearing, and says so. Those carry no rid, because they answer no
    // request - they used to be dropped here as unmatched replies.
    if (msg && msg.rid === undefined && msg.event) {
      onHostEvent(msg);
      return;
    }
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
  // Ask it to watch. Cheap on the platforms with a real event, and the reply
  // says whether this one has to fall back to looking - see absh/mounts.py.
  WATCH = native({ cmd: "watch" });
  WATCH.catch(() => { /* an older helper has no watch */ });

  PORT.onDisconnect.addListener(() => {
    // A port this script already let go of (see dropPort) has nothing left
    // to say about the one that replaced it.
    if (PORT !== self) return;
    dropPort((browser.runtime.lastError && browser.runtime.lastError.message) ||
             "native helper disconnected");
  });
  return PORT;
}

/* Forget the connection, and fail whatever was still waiting on it.
 *
 * Called on a disconnect, and deliberately after the helper replaces itself:
 * that process has exited and will not answer anything queued behind the
 * update, and the next message has to reach a fresh one started from the new
 * files. Waiting for the browser to report the exit would leave a window in
 * which a request is posted to a port that is already closing. */
function dropPort(reason) {
  const old = PORT;
  PORT = null;
  WATCH = null;
  if (old) {
    try { old.disconnect(); } catch { /* already gone */ }
  }
  for (const [, p] of PENDING) p.reject(new Error(reason));
  PENDING.clear();
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
  const wanted = pattern && (await hasHostPermission(c.absUrl)) ? pattern : null;

  // If the registration is already exactly what we want, leave it alone.
  //
  // This used to unregister and re-register every time, and it runs on
  // onStartup - so every browser launch opened a window with no content script
  // registered at all. A library page loaded inside that window got nothing:
  // no toolbar button, no badges, no error, indistinguishable from the
  // extension not being installed. It reproduced on roughly one page load in
  // eight against a real server.
  if (wanted && existing.length === ids.length &&
      existing.every((s) => Array.isArray(s.matches) &&
                            s.matches.length === 1 && s.matches[0] === wanted)) {
    await browser.storage.local.set({
      registrationError: "", registeredPattern: wanted }).catch(() => {});
    return;
  }

  if (existing.length) {
    await browser.scripting.unregisterContentScripts(
      { ids: existing.map((s) => s.id) }).catch(() => {});
  }

  // Say why there is nothing to register, rather than returning in silence.
  // Silence here is indistinguishable, from the page and from the options
  // page alike, from the extension not being installed at all - which is
  // exactly how it looked on a real machine, twice.
  if (!wanted) {
    let why;
    if (!c.absUrl) why = "no server URL is set";
    else if (!pattern) why = `${c.absUrl} is not a usable server URL`;
    else {
      let origin = c.absUrl;
      try { origin = ABSH.originPattern(c.absUrl); } catch { /* keep the raw URL */ }
      why = `access has not been granted for ${origin}`;
    }
    await browser.storage.local.set(
      { registrationError: why, registeredPattern: "" }).catch(() => {});
    return;
  }

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
    const reply = await call("ping");
    // The watch was sent first on this connection, so its answer is already
    // in or about to be. null when the helper is too old to watch at all.
    const watch = WATCH ? await WATCH.catch(() => null) : null;
    return { ok: true, ...reply, polls: watch ? !!watch.polls : null };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

/* ---------------------------------------------------------------- updates
 *
 * Every check goes through the helper, which asks the release feed itself.
 * The extension holds no host permission for GitHub and should not need one:
 * the shipped manifest asks for no host at all, and what the helper is
 * updating is the helper.
 *
 * The answer is cached in storage with the time it was asked, and asked again
 * only when ABSH.updateCheckDue says - see lib.js for the cadence. */
let CHECKING = null;

async function askReleaseFeed(previous, installed) {
  let check;
  try {
    const r = await native({ cmd: "update-check" });
    check = r.checkError
      // Keep what was known before: a failed check is no evidence that an
      // update found yesterday has gone away.
      ? { ...(previous || {}), error: r.checkError }
      : { latest: r.latest || "", prerelease: !!r.prerelease,
          releaseRefused: r.releaseRefused || "", error: "" };
  } catch (e) {
    const msg = String((e && e.message) || e);
    // A helper from before in-browser updates rejects the command outright.
    // That is not a failed check, and retrying it hourly would not help.
    check = /unknown cmd/.test(msg)
      ? { unsupported: true, error: "" }
      : { ...(previous || {}), error: msg };
  }
  check.installed = installed;
  check.checkedAt = Date.now();
  await browser.storage.local.set({ updateCheck: check }).catch(() => {});
  return check;
}

/** The helper as it is now, and what the release feed last said - asked
 *  again first if that is due. `peek` answers from the cache regardless and
 *  says whether a check is due, so a page can show the version at once and
 *  the feed's answer when it arrives. */
async function updateStatus({ force = false, peek = false } = {}) {
  const helper = await ping();
  const { updateCheck } = await browser.storage.local.get({ updateCheck: null });
  if (!helper.ok) return { helper, check: updateCheck, due: false };
  const due = ABSH.updateCheckDue(updateCheck, helper.release, Date.now(), force);
  if (!due || peek) return { helper, check: updateCheck, due };
  // The popup and the options page can both ask at once; one request is
  // enough for both.
  if (!CHECKING) {
    CHECKING = askReleaseFeed(updateCheck, helper.release)
      .finally(() => { CHECKING = null; });
  }
  return { helper, check: await CHECKING, due: false };
}

async function installUpdate(tag, onProgress) {
  const r = await native({ cmd: "update", ...(tag ? { tag } : {}) }, onProgress);
  if (r.restarting) dropPort("the helper restarted after updating itself");
  return r;
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
    case "updateStatus": return { ok: true, data: await updateStatus({ force: !!msg.force, peek: !!msg.peek }) };
    case "update":    return { ok: true, data: await installUpdate(msg.tag, onProgress) };
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

/* Pass a helper event on to the pages that can act on it.
 *
 * The page is what shows device state, so it is the page that has to hear
 * this. Only tabs matching the one registered pattern are told: no other tab
 * has a content script listening, and telling every tab would mean asking for
 * every tab. */
async function onHostEvent(msg) {
  const { registeredPattern } = await browser.storage.local.get(
    { registeredPattern: "" });
  if (!registeredPattern) return;
  const tabs = await browser.tabs.query({ url: registeredPattern }).catch(() => []);
  // Chrome ignores the url filter outright when the host permission is
  // missing, handing back every tab instead of none - so check the answer
  // rather than trusting the question.
  const prefix = registeredPattern.replace(/\*$/, "");
  for (const tab of tabs) {
    if (!tab.url || !tab.url.startsWith(prefix)) continue;
    browser.tabs.sendMessage(tab.id, { type: "host-event", event: msg.event })
      .catch(() => { /* no content script in that tab yet */ });
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
    const streams = msg && ["pull", "push", "update"].includes(msg.type);
    const onProgress = streams ? (ev) => reply({ ok: true, progress: ev }) : null;
    try {
      reply(await route(msg, onProgress));
    } catch (e) {
      reply({ ok: false, error: String((e && e.message) || e) });
    }
  });
});

/* A registered content script is not a guarantee.
 *
 * Verified against a real Audiobookshelf: on roughly one library page load in
 * eight, no content script ran even though getRegisteredContentScripts showed
 * both entries with the right pattern and no error stored. The page then had
 * no toolbar button and no badges - identical to the extension not being
 * installed, and the exact symptom that took an evening to pin down.
 *
 * So inject it again once the page has settled. content.js sets a flag on
 * window and returns early if it is already there, making the duplicate a
 * no-op on the loads where registration did work. */
let LAST_INJECT_ERROR = "";

async function injectFallback(tabId, url) {
  if (!browser.scripting || !browser.scripting.executeScript) return;
  const c = await cfg();
  let prefix = null;
  try {
    prefix = c.absUrl ? ABSH.libraryPrefix(c.absUrl) : null;
  } catch {
    return;
  }
  if (!prefix || !String(url || "").startsWith(prefix)) return;
  if (!(await hasHostPermission(c.absUrl))) return;

  await browser.scripting.insertCSS({ target: { tabId }, files: ["content.css"] })
    .catch(() => {});
  try {
    await browser.scripting.executeScript({
      target: { tabId },
      files: ["browser-polyfill.js", "content.js"],
    });
    if (LAST_INJECT_ERROR) {
      LAST_INJECT_ERROR = "";
      await browser.storage.local.set({ injectError: "" }).catch(() => {});
    }
  } catch (e) {
    // Swallowed until now, which left the same hole the registration had: a
    // page with no UI on it and nothing anywhere saying why. Recorded once
    // per distinct message, so a page that fails on every load does not
    // rewrite storage on every load.
    const msg = (e && e.message) || String(e);
    if (msg !== LAST_INJECT_ERROR) {
      LAST_INJECT_ERROR = msg;
      await browser.storage.local.set({ injectError: msg }).catch(() => {});
    }
  }
}

if (browser.tabs && browser.tabs.onUpdated) {
  browser.tabs.onUpdated.addListener((tabId, info, tab) => {
    if (info.status !== "complete") return;
    const url = (tab && tab.url) || info.url;
    if (url) injectFallback(tabId, url);
  });
}

browser.runtime.onInstalled.addListener(() => { syncContentScript(); });
if (browser.runtime.onStartup) {
  browser.runtime.onStartup.addListener(() => { syncContentScript(); });
}
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.absUrl) syncContentScript();
});
