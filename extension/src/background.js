/* Audiobookshelf Helper - background
 *
 * The extension itself CANNOT write to a USB device. Two hard limits:
 *   - downloads.download({filename}) rejects absolute paths and "../", so
 *     everything would land under the Downloads folder;
 *   - Firefox does not implement showDirectoryPicker().
 * So the filesystem work is delegated to the native messaging host, and this
 * script is only an API client plus a bridge.
 *
 * Chrome does implement it, so the Chrome build carries a second way for a
 * user who will not install the helper: folder.js, against a folder they
 * chose. It sits beside the helper rather than replacing it - see "the
 * player's folder" below for which one answers.
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
  let heard = false;
  PORT.onMessage.addListener((msg) => {
    heard = true;
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

  PORT.onDisconnect.addListener((p) => {
    // A port this script already let go of (see dropPort) has nothing left
    // to say about the one that replaced it.
    if (PORT !== self) return;
    // Why it closed: Firefox puts it on the port, Chrome in lastError. Reading
    // only lastError left every Firefox user with "disconnected" and no reason.
    const why = (p && p.error && p.error.message) ||
                (browser.runtime.lastError && browser.runtime.lastError.message);
    // Firefox gives no reason when the helper exits by itself. One that quit
    // before saying a word did not start: most often its files were moved or
    // deleted - it used to run from wherever it was unzipped - or the Python
    // it was installed with is gone. Its stderr is in the Browser Console.
    // Chrome says only "Native host has exited." or, depending on timing,
    // "Error when communicating with the native messaging host.", which are
    // no better. Not found, and not allowed, are left as they are.
    const quit = !heard && (!why || /has exited|error when communicating/i.test(why));
    dropPort(quit ? "The helper app quit as soon as it started. If its folder was " +
                    "moved or deleted, install it again."
      : why || "native helper disconnected");
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

/* The in-page UI is registered at runtime for the one server the user
 * configured, instead of shipping a content script matching every site.
 *
 * For the whole server, not just its library pages. Audiobookshelf is a
 * single-page app: going from its home page to a library, or from a cover to
 * a book's page, changes the address without loading a page, and a content
 * script is only matched against the address a page loaded at. Matching
 * <server>/library/* left nothing on the page for anyone who came in through
 * the home page - which is everyone. The script decides for itself what is on
 * the page, and does nothing where there is nothing for it. */
async function syncContentScript() {
  if (!browser.scripting || !browser.scripting.registerContentScripts) return;
  const c = await cfg();
  let pattern = null;
  try {
    pattern = c.absUrl ? ABSH.originPattern(c.absUrl) : null;
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

/* A helper that started but never answers used to leave every page at
 * "Checking the helper…" for good. Past this, it is treated as stuck: the
 * connection is dropped, which ends that copy, and the next ask starts a
 * fresh one. */
const PING_SECONDS = 15;

function within(promise, seconds, what) {
  let timer;
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} didn't answer within ${seconds} seconds`)),
                       seconds * 1000);
  });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
}

async function ping() {
  try {
    let reply;
    try {
      reply = await within(call("ping"), PING_SECONDS, "The helper");
    } catch (e) {
      if (/didn't answer within/.test(e.message)) dropPort(e.message);
      throw e;
    }
    // The watch was sent first on this connection, so its answer is already
    // in or about to be. null when the helper is too old to watch at all.
    const watch = WATCH ? await within(WATCH, 2, "watch").catch(() => null) : null;
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

// This extension's release, for the cached answer to say which one asked.
const EXTENSION_RELEASE = (globalThis.ABSH_CONFIG || {}).release
  || browser.runtime.getManifest().version;

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
  check.extension = EXTENSION_RELEASE;
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
  const due = ABSH.updateCheckDue(updateCheck, helper.release, Date.now(), force,
                                 EXTENSION_RELEASE);
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

/* ---------------------------------------------------- the player's folder
 *
 * Chrome only. build.py imports folder.js into the Chrome bundle and nowhere
 * else, so on Firefox FOLDER is null and every request below goes to the
 * helper exactly as it always has.
 *
 * On Chrome a user who cannot install the helper can choose the player's
 * folder instead. The helper still wins whenever it answers: it finds the
 * player by itself, hears it plugged in, and needs no permission renewed.
 * The folder is used when the helper does not answer, or when the user asked
 * for it outright, and never before a folder has been chosen. */
const FOLDER = globalThis.ABSH_FOLDER || null;

async function folderChosen() {
  try {
    return !!(await FOLDER.loadHandle());
  } catch {
    return false;
  }
}

/** Which one answers a request about the device: "helper" or "folder". */
async function backend() {
  if (!FOLDER || !(await folderChosen())) return "helper";
  const { folderMode } = await browser.storage.local.get({ folderMode: "auto" });
  if (folderMode === "always") return "folder";
  return (await ping()).ok ? "helper" : "folder";
}

/* The settings each command takes from the request - the same picks call()
 * makes for the helper below. */
const FOLDER_ARGS = {
  status: (m) => ({ libraryId: m.libraryId, readTags: m.readTags !== false }),
  pull: (m) => ({ ids: m.ids, libraryId: m.libraryId, force: !!m.force }),
  push: (m) => ({ names: m.names, libraryId: m.libraryId, folderId: m.folderId }),
  remove: (m) => ({ names: m.names }),
  libraries: () => ({}),
  folders: (m) => ({ libraryId: m.libraryId }),
  group: (m) => ({ kind: m.kind, id: m.id, libraryId: m.libraryId }),
};

async function viaFolder(type, msg, onProgress) {
  // A copy runs here, in the worker, and a book can take minutes. Calling an
  // extension API resets the worker's idle timer, which is the documented
  // way to keep it alive through work that makes no other API calls.
  const keepAlive = setInterval(() => {
    Promise.resolve(browser.runtime.getPlatformInfo()).catch(() => {});
  }, 20_000);
  try {
    const r = await FOLDER.run(type, { ...(await cfg()), ...FOLDER_ARGS[type](msg) },
                               onProgress || undefined);
    if (type === "libraries") return r.libraries;
    if (type === "folders") return r.folders;
    if (type === "group") return r.ids;
    return r;
  } finally {
    clearInterval(keepAlive);
  }
}

/** ping, from whichever backend would answer. When neither can, the helper's
 *  error - plus, on Chrome, word that a folder would do instead. */
async function pingEither() {
  if (!FOLDER) return ping();
  if ((await backend()) === "folder") return FOLDER.run("ping", await cfg());
  const p = await ping();
  return p.ok ? p : { ...p, folderAvailable: true };
}

/* --------------------------------------------------------------- routing */
async function route(msg, onProgress) {
  const type = msg && msg.type;
  if (FOLDER) {
    if (FOLDER_ARGS[type] && (await backend()) === "folder") {
      const data = await viaFolder(type, msg, onProgress);
      if (type === "status") LAST_STATUS = data;
      return { ok: true, data };
    }
    if (type === "ping") return { ok: true, data: await pingEither() };
    if (type === "openFolderOptions") {
      // Restoring access needs a click on one of the extension's own pages;
      // the library page can only send the user there.
      await browser.runtime.openOptionsPage();
      return { ok: true };
    }
  }
  switch (type) {
    case "libraries": return { ok: true, data: (await call("libraries")).libraries };
    case "folders":   return { ok: true, data: (await call("folders", { libraryId: msg.libraryId })).folders };
    case "devices":   return { ok: true, data: (await call("devices")).devices };
    case "group":     return { ok: true, data: (await call("group", { kind: msg.kind, id: msg.id,
                                                                     libraryId: msg.libraryId })).ids };
    case "status":
      LAST_STATUS = await call("status", { libraryId: msg.libraryId, readTags: msg.readTags !== false });
      // The player answered: a run waiting for it can carry on.
      if (JOBS.paused && !RESUMING) setTimeout(tryResume, 0);
      return { ok: true, data: LAST_STATUS };
    case "lastStatus": return { ok: true, data: LAST_STATUS };
    case "jobAdd":    return { ok: true, data: addJob(msg.op, msg.items || [], msg.player, msg.writeTags,
                                                    !!msg.force) };
    case "tagsAsk":   return { ok: true, data: await tagsAsk() };
    case "tagsRemember":
      await browser.storage.local.set({ writeTags: msg.value === "always" ? "always" : "never" });
      return { ok: true, data: await tagsAsk() };
    case "jobState":  return { ok: true, data: snapshot() };
    case "jobCancel": return { ok: true, data: cancelPaused() };
    case "jobSeen":
      JOBS.seen = true;
      badge();
      return { ok: true, data: snapshot() };
    case "pull":      return { ok: true, data: await call("pull", { ids: msg.ids, libraryId: msg.libraryId,
                                                                    ...(msg.force ? { force: true } : {}),
                                                                    ...tagsArg(msg.writeTags) }, onProgress) };
    case "push":      return { ok: true, data: await call("push", { names: msg.names, libraryId: msg.libraryId, folderId: msg.folderId }, onProgress) };
    case "remove":    return { ok: true, data: await call("remove", { names: msg.names }, onProgress) };
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

/* ---------------------------------------------------------------- copies
 *
 * A copy, an upload or a removal belongs to the extension, not to the popup
 * that asked for it. A popup closes the moment it loses focus, and the
 * progress it alone was told about went with it: opening it again showed an
 * empty shelf, as if nothing were happening. So the work is queued here, its
 * state is kept here, and every open popup and library page is told as it
 * changes. The toolbar icon carries it while nothing is open, and the
 * computer says when it is done.
 *
 * Jobs run one at a time, in the order asked for; a new one asked for while
 * another runs waits its turn. */
const JOBS = { current: null, queue: [], last: null, seq: 0, seen: true, batch: null,
               paused: null };
const POPUPS = new Set();                // open popups and settings pages
let LAST_STATUS = null;                  // so a popup opened mid-copy has a shelf

function snapshot() {
  return { current: JOBS.current, queue: JOBS.queue, last: JOBS.last, seen: JOBS.seen,
           batch: JOBS.batch, paused: JOBS.paused };
}

let BROADCAST = null;
function changed() {
  // Progress arrives far faster than anyone reads it; four a second is plenty.
  if (BROADCAST) return;
  BROADCAST = setTimeout(() => {
    BROADCAST = null;
    const snap = snapshot();
    for (const p of POPUPS) {
      try { p.postMessage({ event: "job", job: snap }); } catch { POPUPS.delete(p); }
    }
    tellPages({ type: "job", job: snap });
    badge();
    saveJobs();
  }, 250);
}

// What the toolbar button says when the pointer is on it and nothing runs.
const TITLE = (browser.runtime.getManifest().action || {}).default_title || "Audiobookshelf Helper";

/* The toolbar icon: books still to go, while copying, with "done of all"
 * when the pointer is on it; a check when it finished, until the popup has
 * been opened; "!" when it failed. */
function badge() {
  const a = browser.action;
  if (!a || !a.setBadgeText) return;
  const j = JOBS.current;
  let text = "";
  let title = TITLE;
  let color = "#3443C9";
  if (j || JOBS.paused) {
    const run = (JOBS.batch && JOBS.batch.finished) || [];
    const all = [...run, ...[j, ...JOBS.queue].filter(Boolean).flatMap((x) => x.items)];
    const done = all.filter((i) => i.state === "done").length;
    text = ABSH.badgeCount(all.length - done);
    title = `${TITLE}: ${JOBS.paused ? "paused at" : "copying"} ${done} of ${all.length}`;
    if (JOBS.paused) color = "#6B7280";          // waiting, not working
  } else if (JOBS.last && !JOBS.seen) {
    const failed = JOBS.last.error || JOBS.last.items.some((i) => i.state === "failed");
    text = failed ? "!" : "✓";
    color = failed ? "#B42318" : "#1D7A43";
  }
  Promise.resolve(a.setBadgeText({ text })).catch(() => {});
  if (text) Promise.resolve(a.setBadgeBackgroundColor({ color })).catch(() => {});
  if (a.setTitle) Promise.resolve(a.setTitle({ title })).catch(() => {});
}

const VERB = { pull: "Copied", push: "Uploaded", remove: "Removed" };

/* Only when nothing of ours is open: a popup already shows it. */
function notify(job) {
  if (POPUPS.size || !browser.notifications || !browser.notifications.create) return;
  const done = job.items.filter((i) => i.state === "done");
  const failed = job.items.filter((i) => i.state === "failed");
  const bytes = done.reduce((n, i) => n + (Number(i.bytes) || 0), 0);
  const mins = Math.max(1, Math.round((job.finishedAt - job.startedAt) / 60000));
  const where = job.op === "push" ? "the library" : job.player || "the player";
  const title = failed.length || job.error
    ? `${VERB[job.op]} ${done.length} of ${job.items.length}; ${failed.length || "the rest"} failed`
    : `${VERB[job.op]} ${done.length} ${done.length === 1 ? "book" : "books"} ${job.op === "remove" ? "from" : "to"} ${where}`;
  const noted = job.items.find((i) => i.note);
  const message = job.error ? job.error
    : `${ABSH.formatBytes(bytes).replace(/^([\d.]+)/, "$1 ")} in ${mins} min.` +
      (noted ? ` ${noted.title}: ${noted.note}` : "");
  Promise.resolve(browser.notifications.create(`absh-job-${job.id}`, {
    type: "basic", iconUrl: browser.runtime.getURL("icons/icon-96.png"), title, message,
  })).catch(() => {});
}

/* Queue a job: op is pull, push or remove; each item names what it acts on
 * (`ref`: a library item id to pull, a name on the player otherwise). */
/* Whether a copy should ask before writing Audiobookshelf's tags into the
 * files, and what to do when it does not ask. Only the helper writes tags:
 * Chrome's folder backend copies the server's files as they are. */
async function tagsAsk() {
  const { writeTags } = await browser.storage.local.get({ writeTags: ABSH.DEFAULTS.writeTags });
  const can = (await backend()) !== "folder";
  return { ask: can && writeTags === "ask", write: can && writeTags === "always" };
}

/* A yes or no from the person goes to the helper; anything else is left out,
 * so the saved setting decides - never a stray "ask" read as no. */
function tagsArg(v) {
  return typeof v === "boolean" ? { writeTags: v } : {};
}

function addJob(op, items, player, writeTags, force = false) {
  // A run is everything queued from idle until idle again - ten covers
  // clicked one after another are one run of ten, whatever the jobs. Counted
  // as a run, so a finished book is "2 of 10", not gone from "1 of 9".
  if (!JOBS.current && !JOBS.queue.length && !JOBS.paused) {
    JOBS.batch = { startedAt: Date.now(), finished: [] };
  }
  const job = {
    id: ++JOBS.seq, op, player: player || "",
    ...(op === "pull" && typeof writeTags === "boolean" ? { writeTags } : {}),
    // Copy again: books already on the player, checked file by file.
    ...(op === "pull" && force ? { force: true } : {}),
    items: items.map((i) => ({ key: i.key, ref: i.ref, title: i.title || i.ref,
                              bytes: Number(i.bytes) || 0, state: "waiting", done: 0, total: 0 })),
    startedAt: 0, finishedAt: 0, result: null, error: "",
  };
  JOBS.queue.push(job);
  JOBS.seen = false;
  changed();
  runJobs();
  return snapshot();
}

async function runJobs() {
  if (JOBS.current || JOBS.paused) return;
  while (JOBS.queue.length && !JOBS.paused) {
    const job = JOBS.queue.shift();
    JOBS.current = job;
    job.startedAt = job.startedAt || Date.now();
    changed();
    // The helper reports each item as it starts - in library order, not the
    // order asked for, so matched by what it names (a pull's id, an upload's
    // or removal's name). Matching by position put one book's title on
    // another's progress.
    let at = -1;
    const onProgress = (ev) => {
      if (ev.event === "item") {
        const key = ev.id ?? ev.name ?? (job.op === "remove" ? ev.title : undefined);
        let i = key == null ? -1 : job.items.findIndex((x) => x.ref === key);
        if (i < 0) i = at + 1;
        for (const it of job.items) if (it.state === "copying") it.state = "done";
        at = i;
        if (job.items[at]) Object.assign(job.items[at], { state: "copying", done: 0, total: 0 });
      } else if (ev.event === "progress" && job.items[at]) {
        job.items[at].done = ev.done || 0;
        job.items[at].total = ev.total || 0;
      }
      changed();
    };
    const todo = job.items.filter((i) => i.state !== "done");
    const refs = todo.map((i) => i.ref);
    const msg = job.op === "pull" ? { type: "pull", ids: refs, ...(job.force ? { force: true } : {}),
                                          ...tagsArg(job.writeTags) }
      : { type: job.op, names: refs };
    let left = new Set();
    try {
      const r = await route(msg, onProgress);
      if (!r || !r.ok) throw new Error((r && r.error) || "no response");
      job.result = r.data || {};
      // The player went away partway: those were not tried, and wait for it.
      left = new Set(job.result.interrupted || []);
      if (left.size) JOBS.paused = { why: "player", player: job.player, since: Date.now() };
      // An error line reads "<title>: why"; those items failed, the rest are done.
      const errs = (job.result.errors || []).map(String);
      // A note is worth saying but the book is fine - copied without tags.
      const notes = (job.result.notes || []).map(String);
      for (const it of todo) {
        if (left.has(it.ref)) { it.state = "waiting"; continue; }
        const bad = errs.find((e) => e.startsWith(`${it.title}:`));
        it.state = bad ? "failed" : "done";
        if (bad) it.error = bad.slice(it.title.length + 1).trim();
        const said = notes.find((e) => e.startsWith(`${it.title}:`));
        if (said) it.note = said.slice(it.title.length + 1).trim();
      }
    } catch (e) {
      // Did the job fail, or did what it needed go away - the player, or the
      // helper itself? Ask: if the player answers, the failure is real.
      const why = String((e && e.message) || e);
      const st = await route({ type: "status", readTags: false }).catch(() => null);
      if (st && st.ok) {
        job.error = why;
        for (const it of todo) if (it.state !== "done") it.state = "failed";
      } else {
        const helper = /helper|native|quit as soon|disconnect|exited|answer/i.test(why);
        JOBS.paused = { why: helper ? "helper" : "player", player: job.player,
                        since: Date.now(), error: why };
        for (const it of todo) if (it.state !== "done") { it.state = "waiting"; left.add(it.ref); }
      }
    }
    if (left.size) {
      // What was copied stays copied; the rest waits at the front of the queue.
      const rest = { ...job, id: ++JOBS.seq, result: null, error: "",
                     items: job.items.filter((i) => left.has(i.ref))
                       .map((i) => ({ ...i, state: "waiting", done: 0, total: 0 })) };
      job.items = job.items.filter((i) => !left.has(i.ref));
      JOBS.queue.unshift(rest);
    }
    job.finishedAt = Date.now();
    if (JOBS.batch) JOBS.batch.finished.push(...job.items);
    JOBS.current = null;
    if (!JOBS.paused) JOBS.last = job;
    changed();
    // What is on the player changed: refresh the shelf everyone shows.
    refreshStatus();
  }
  if (JOBS.paused) {
    notifyPaused();
    watchForReturn();
  } else if (JOBS.batch) {
    notify(runSummary());
  }
}

/* The whole run as one job, for the notification at its end. */
function runSummary() {
  const last = JOBS.last || {};
  return { ...last, id: `run-${JOBS.batch.startedAt}`, items: JOBS.batch.finished,
           startedAt: JOBS.batch.startedAt, finishedAt: Date.now() };
}

/* ------------------------------------------------ waiting for the player
 *
 * A run whose player went away waits for it rather than failing what is
 * left. It carries on when the player answers again: told by the helper's
 * device events where there are any, and otherwise asked every 15 seconds
 * and whenever a page asks after the shelf. */
let RESUMING = false;
let WATCHING = null;

async function tryResume() {
  if (!JOBS.paused || JOBS.current || RESUMING) return;
  RESUMING = true;
  try {
    const r = await route({ type: "status", readTags: false });
    if (r && r.ok && JOBS.paused) {
      JOBS.paused = null;
      changed();
      runJobs();
    }
  } catch { /* still away */ } finally {
    RESUMING = false;
  }
}

function watchForReturn() {
  if (WATCHING) return;
  WATCHING = setInterval(() => {
    if (!JOBS.paused) { clearInterval(WATCHING); WATCHING = null; return; }
    tryResume();
  }, 15_000);
}

function notifyPaused() {
  if (POPUPS.size || !browser.notifications || !browser.notifications.create) return;
  const p = JOBS.paused;
  const n = JOBS.queue.reduce((k, j) => k + j.items.length, 0);
  Promise.resolve(browser.notifications.create("absh-paused", {
    type: "basic", iconUrl: browser.runtime.getURL("icons/icon-96.png"),
    title: p.why === "helper" ? "Copying paused: the helper stopped"
                              : `Copying paused: ${p.player || "the player"} disconnected`,
    message: `${n} ${n === 1 ? "book is" : "books are"} waiting. ` +
             (p.why === "helper" ? "It carries on when the helper is back."
                                 : "Plug it back in to carry on."),
  })).catch(() => {});
}

/* ----------------------------------------------- surviving a restart
 *
 * What is queued is kept in storage, so quitting the browser - or Chrome
 * stopping the service worker - mid-run does not lose it. On the way back
 * it carries on; books already on the player are skipped by the helper. */
let SAVING = null;
function saveJobs() {
  if (SAVING) return;
  SAVING = setTimeout(() => {
    SAVING = null;
    const busy = JOBS.current || JOBS.queue.length || JOBS.paused;
    const cur = JOBS.current;
    const queue = [
      ...(cur ? [{ ...cur, items: cur.items.filter((i) => i.state !== "done")
                     .map((i) => ({ ...i, state: "waiting", done: 0, total: 0 })) }] : []),
      ...JOBS.queue,
    ].filter((j) => j.items.length);
    const batch = JOBS.batch && { ...JOBS.batch,
      finished: [...JOBS.batch.finished, ...(cur ? cur.items.filter((i) => i.state === "done") : [])] };
    Promise.resolve(browser.storage.local.set({
      jobsSaved: busy ? { queue, batch, paused: JOBS.paused } : null,
    })).catch(() => {});
  }, 1000);
}

const RESTORED = Promise.resolve(browser.storage.local.get({ jobsSaved: null }))
  .then(({ jobsSaved: s }) => {
    if (!s || !Array.isArray(s.queue) || !s.queue.length) return;
    JOBS.queue = [...s.queue, ...JOBS.queue];
    JOBS.batch = JOBS.batch || s.batch || { startedAt: Date.now(), finished: [] };
    JOBS.paused = s.paused || null;
    // Nobody has seen how this run ends yet; whoever opens a popup next should.
    JOBS.seen = false;
    changed();
    if (JOBS.paused) { tryResume(); watchForReturn(); } else runJobs();
  })
  .catch(() => {});

/* A paused run can be let go: what is copied stays, what waits is dropped. */
function cancelPaused() {
  if (!JOBS.paused || JOBS.current) return snapshot();
  const dropped = JOBS.queue.reduce((k, j) => k + j.items.length, 0);
  const last = JOBS.queue[0] || JOBS.last || {};
  JOBS.last = { ...last, id: ++JOBS.seq, items: JOBS.batch ? JOBS.batch.finished : [],
                startedAt: JOBS.batch ? JOBS.batch.startedAt : Date.now(),
                finishedAt: Date.now(), cancelled: dropped, error: "" };
  JOBS.queue = [];
  JOBS.paused = null;
  JOBS.seen = true;
  changed();
  return snapshot();
}

async function refreshStatus() {
  try {
    const r = await route({ type: "status", readTags: true });
    if (!r || !r.ok) return;
    LAST_STATUS = r.data;
    tellPages({ type: "status", status: LAST_STATUS });
    for (const p of POPUPS) {
      try { p.postMessage({ event: "status", status: LAST_STATUS }); } catch { POPUPS.delete(p); }
    }
  } catch { /* the next popup or page asks again */ }
}

/* Tell the library pages - the tabs on the user's own server. */
async function tellPages(msg) {
  const { absUrl } = await cfg();
  let pattern;
  try { pattern = ABSH.originPattern(absUrl); } catch { return; }
  const tabs = await browser.tabs.query({ url: pattern }).catch(() => []);
  const prefix = pattern.replace(/\*$/, "");
  for (const tab of tabs) {
    if (!tab.url || !tab.url.startsWith(prefix)) continue;
    browser.tabs.sendMessage(tab.id, msg).catch(() => { /* no content script there */ });
  }
}

/* Pass a helper event on to the pages that can act on it.
 *
 * The page is what shows device state, so it is the page that has to hear
 * this. Only tabs matching the one registered pattern are told: no other tab
 * has a content script listening, and telling every tab would mean asking for
 * every tab. */
async function onHostEvent(msg) {
  if (msg.event === "devices-changed") tryResume();
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

/* A refusal can carry a code saying what would fix it - the folder backend's
 * "Chrome needs your OK again", say - so the page can offer the fix rather
 * than only the sentence. The helper's refusals carry none. */
const failure = (e) => ({ ok: false, error: String((e && e.message) || e),
                          ...(e && e.code ? { code: e.code } : {}) });

/* Chrome does NOT support returning a promise from an onMessage listener - the
 * sender just receives undefined. Firefox does. Answering through
 * sendResponse and returning true works in both. */
browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  route(msg).then(
    (r) => sendResponse(r),
    (e) => sendResponse(failure(e))
  );
  return true;
});

/* The popup uses a port so that sync progress can stream back to it. */
browser.runtime.onConnect.addListener((p) => {
  if (p.name !== "absh") return;
  POPUPS.add(p);
  p.onDisconnect.addListener(() => POPUPS.delete(p));
  p.onMessage.addListener(async (msg) => {
    const reply = (body) => {
      try { p.postMessage({ ...body, rid: msg && msg.rid }); } catch { /* popup closed */ }
    };
    const streams = msg && ["pull", "push", "update"].includes(msg.type);
    const onProgress = streams ? (ev) => reply({ ok: true, progress: ev }) : null;
    try {
      reply(await route(msg, onProgress));
    } catch (e) {
      reply(failure(e));
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
    prefix = c.absUrl ? ABSH.originPattern(c.absUrl).replace(/\*$/, "") : null;
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
