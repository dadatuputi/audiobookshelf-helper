/* Popup: one list, three directions, over the same status the CLI shows.
 *
 * The helper does the work and owns the Audiobookshelf client; this only picks
 * things and renders what came back. */
const $ = (s) => document.querySelector(s);

let TAB = "server";                    // server | device | only
let ST = { both: [], serverOnly: [], deviceOnly: [] };
// Whether ST is a real answer. Counts are shown only then: an unknown is
// blank, never a zero.
let KNOWN = false;
let SEL = new Set();
// No usable player: the shelf has nothing true to say about "the device".
let NO_PLAYER = false;
let PANEL_OPEN = false;
let LIB = "";                          // the server's host, as the Library card names it
let FOLDER = "";                       // Chrome's folder, when that is what is in use
let SETUP = false;                     // showing what to do first, not the shelf

/* ----------------------------------------------------------- transport */
const PORT = browser.runtime.connect({ name: "absh" });
let RID = 0;
const WAITING = new Map();

PORT.onMessage.addListener((m) => {
  // Said by the background unasked: a job moved on, or the shelf changed.
  if (m && m.event === "job") { onJob(m.job); return; }
  if (m && m.event === "status") { showStatus(m.status); return; }
  const w = WAITING.get(m && m.rid);
  if (!w) return;
  if (m.progress) { if (w.onProgress) w.onProgress(m.progress); return; }
  WAITING.delete(m.rid);
  m.ok ? w.resolve(m.data) : w.reject(new Error(m.error || "no response"));
});
PORT.onDisconnect.addListener(() => {
  for (const [, w] of WAITING) w.reject(new Error("background disconnected"));
  WAITING.clear();
});

function send(msg, onProgress) {
  return new Promise((resolve, reject) => {
    const rid = ++RID;
    WAITING.set(rid, { resolve, reject, onProgress });
    PORT.postMessage({ ...msg, rid });
  });
}

/* -------------------------------------------------------------- helpers */
function status(msg, cls = "") {
  const el = $("#status");
  el.textContent = msg || "";
  el.className = "status " + cls;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/** "17.6 GB" - formatBytes with the space a reader expects. */
const size = (n) => ABSH.formatBytes(n).replace(/^([\d.]+)/, "$1 ");

/** "17.6 of 29.7 GB free": the denominator, without saying the unit twice. */
function freeOf(f) {
  if (!f || !f.free) return "";
  if (!f.total) return `${size(f.free)} free`;
  const [a, ua] = size(f.free).split(" ");
  const [b, ub] = size(f.total).split(" ");
  return ua === ub ? `${a} of ${b} ${ub} free` : `${a} ${ua} of ${b} ${ub} free`;
}

function baseName(path) {
  const parts = String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || String(path || "");
}

/** What the Player card, and the button that copies to it, call the player. */
function playerLabel() {
  if (BACKEND === "folder") return FOLDER || "the folder";
  return PLAYER.devicePath ? baseName(PLAYER.devicePath) : "the player";
}

/* The narrator tells two editions of one book apart: a full-cast recording
 * and a single reader share the author and the title. */
/* ------------------------------------------------------------- the rows
 *
 * What tells two versions of one book apart is who reads it and how long it
 * runs, not the author they share - so that is the second line, and the rest
 * opens under the row. */
function hm(sec) {
  sec = Math.round(Number(sec) || 0);
  if (!sec) return "";
  if (sec < 60) return `${sec} s`;
  // Round once, in minutes: rounding the remainder alone gave "1 h 60 m".
  const mins = Math.round(sec / 60);
  const h = Math.floor(mins / 60), m = mins % 60;
  return h ? (m ? `${h} h ${m} m` : `${h} h`) : `${m} m`;
}

/** "Hugh Laurie, Matthew Macfadyen +43" - the first two, and how many more. */
function readers(narrator) {
  const all = String(narrator || "").split(/\s*,\s*/).filter(Boolean);
  return all.length > 2 ? `${all[0]}, ${all[1]} +${all.length - 2}` : all.join(", ");
}

/** "Harry Potter #1" is how Audiobookshelf writes it; "Harry Potter, book 1" reads. */
function seriesWords(series) {
  return String(series || "").split(/\s*,\s*(?=[^,#]+#)/).map((x) => {
    const m = /^(.*?)\s*#\s*([\d.]+)$/.exec(x.trim());
    return m ? `${m[1]}, book ${m[2]}` : x.trim();
  }).filter(Boolean).join("; ");
}

function line2(i, fallbackAuthor) {
  const who = i && i.narrator ? readers(i.narrator) : (fallbackAuthor || "");
  return [who, hm(i && i.duration)].filter(Boolean).join(" · ");
}

/** The details under an opened row: label and value, only what is known. */
function details(r) {
  const i = r.item || {};
  const files = i.numTracks ? `${i.numTracks} ${i.numTracks === 1 ? "file" : "files"}` : "";
  return [
    ["Title", [r.title, i.subtitle].filter(Boolean).join(": ")],
    ["Author", i.author || r.author],
    ["Read by", i.narrator],
    ["Series", seriesWords(i.series)],
    ["Length", [hm(i.duration), files].filter(Boolean).join(" · ")],
    ["Published", i.publishedYear],
    ["Size", r.bytes ? size(r.bytes) : ""],
  ].filter(([, v]) => v);
}

const rowsFor = {
  // What to send to the player: books it does not have, and books that
  // changed in Audiobookshelf since they were copied - those write only what
  // differs.
  server: () => ST.serverOnly.map((i) => ({ key: i.id, id: i.id, title: i.title, item: i,
    author: i.author, sub: line2(i, i.author), bytes: i.size }))
    .concat(ST.both.filter((b) => b.stale && b.item).map((b) => ({
      key: b.itemId, id: b.itemId, title: b.title, item: b.item, author: b.author,
      sub: ["Changed since it was copied · update", line2(b.item, b.author)]
        .filter(Boolean).join(" · "),
      bytes: b.item.size || b.bytes }))),
  device: () => ST.both.map((b) => ({ key: b.name, name: b.name, id: b.itemId, title: b.title,
    item: b.item, author: b.author,
    sub: [line2(b.item, b.author),
          b.matchedBy && b.matchedBy !== "id" ? `matched by ${b.matchedBy}` : ""]
      .filter(Boolean).join(" · "), bytes: b.bytes })),
  only: () => ST.deviceOnly.map((e) => ({ key: e.name, name: e.name, title: e.title || e.name,
    item: { author: e.author, numTracks: (e.files || []).length || 0 }, author: e.author,
    sub: e.author || "unknown author", bytes: e.bytes })),
};

/* What a tab's main button does. The work itself is queued in the background,
 * which keeps it going and keeps everyone told after this popup closes. */
const ACTION = {
  server: {
    op: "pull", none: "Select books to copy",
    label: (n) => `Copy ${n} to ${playerLabel()} →`,
    ref: (r) => r.id,
  },
  device: {
    op: "remove", none: "Select books to remove",
    label: (n) => `Remove ${n} from ${playerLabel()}`,
    ref: (r) => r.name,
  },
  only: {
    op: "push", none: "Select books to upload",
    label: (n) => `← Upload ${n} to ${LIB || "the library"}`,
    ref: (r) => r.name,
  },
};

/* --------------------------------------------------------------- the job */
let JOB = { current: null, queue: [], last: null, seen: true };
const OPEN = new Set();                 // rows opened to show their details

/** This row's part in a copy that is running, queued, or just finished. */
function jobItem(key) {
  for (const j of [JOB.current, ...JOB.queue, JOB.last]) {
    const it = j && j.items.find((i) => i.key === key);
    if (it) return { job: j, it };
  }
  return null;
}

const DOING = { pull: "Copying", push: "Uploading", remove: "Removing" };
const DID = { pull: "Copied", push: "Uploaded", remove: "Removed" };
const PILL = { waiting: "Waiting", done: "", failed: "Failed" };

function pill(found) {
  const { job, it } = found;
  if (it.state === "copying") return el("span", "pill run", DOING[job.op]);
  if (it.state === "done") return el("span", "pill ok", DID[job.op]);
  return el("span", "pill" + (it.state === "failed" ? " bad" : ""), PILL[it.state] || "");
}

/* The footer while something is happening, or has just happened. */
let SHOW_LAST = false;                  // the last job finished while we could see it, or unseen

function renderJob() {
  const box = $("#jobBox");
  // Waiting for the player (or the helper) to come back: the run is not over.
  const paused = !JOB.current && JOB.paused && JOB.queue.length ? JOB.paused : null;
  const live = !!(JOB.current || paused);
  const j = JOB.current || (paused ? JOB.queue[0] : null) || (SHOW_LAST ? JOB.last : null);
  box.classList.toggle("hidden", !j);
  if (!j) return;
  const where = j.op === "push" ? (LIB || "the library") : (j.player || playerLabel());
  // The whole run: what finished earlier in it, what is running, what waits.
  // Once it is over, the run is what is summed up, not just its last job.
  const run = (JOB.batch && JOB.batch.finished) || [];
  const items = live
    ? [...run, ...[JOB.current, ...JOB.queue].filter(Boolean).flatMap((x) => x.items)]
    : run.length ? run : j.items;
  const startedAt = (JOB.batch && JOB.batch.startedAt) || j.startedAt;
  const done = items.filter((i) => i.state === "done");
  const failed = items.filter((i) => i.state === "failed");
  const total = items.reduce((n, i) => n + i.bytes, 0);
  const cur = items.find((i) => i.state === "copying");
  // Bytes: whole books once done, and the share of files done of the one in
  // flight. Nothing finer is reported, so nothing finer is claimed.
  const moved = done.reduce((n, i) => n + i.bytes, 0) +
    (cur && cur.total ? cur.bytes * cur.done / cur.total : 0);
  box.classList.toggle("ok", !live && !failed.length && !j.error && !j.cancelled);
  box.classList.toggle("bad", !live && (failed.length > 0 || !!j.error));
  box.classList.toggle("paused", !!paused);
  $("#jobCancel").classList.toggle("hidden", !paused);
  const left = items.length - done.length - failed.length;
  const head = paused
    ? (paused.why === "helper" ? "Paused · the helper stopped"
                               : `Paused · ${paused.player || where} disconnected`)
    : JOB.current
    ? `${DOING[j.op]} ${Math.min(done.length + 1, items.length)} of ${items.length} ` +
      `${j.op === "remove" ? "from" : "to"} ${where}`
    : j.cancelled
      ? `${DID[j.op]} ${done.length} ${j.op === "remove" ? "from" : "to"} ${where}; ${j.cancelled} cancelled`
    : failed.length || j.error
      ? `${DID[j.op]} ${done.length} of ${items.length}; ${failed.length || items.length - done.length} failed`
      : `${DID[j.op]} ${done.length} ${j.op === "remove" ? "from" : "to"} ${where}`;
  $("#jobHead").textContent = head;
  $("#jobSize").textContent = total ? (live ? `${size(moved)} of ${size(total)}` : size(total)) : "";
  const bar = $("#jobBar");
  bar.classList.toggle("hidden", !live);
  bar.firstElementChild.style.width = total ? `${Math.round(100 * moved / total)}%` : "0%";
  let sub = "";
  if (paused) {
    const n = `${left} ${left === 1 ? "book" : "books"} waiting`;
    sub = paused.why === "helper" ? `Carries on when the helper is back · ${n}`
                                  : `Plug it back in to carry on · ${n}`;
  } else if (JOB.current && done.length && startedAt) {
    const rate = (moved || 1) / (Date.now() - startedAt);
    const left = Math.round((total - moved) / rate / 60000);
    sub = left >= 1 ? `About ${left} ${left === 1 ? "minute" : "minutes"} left` : "Under a minute left";
  } else if (JOB.current && cur) {
    sub = cur.title;
  } else if (!live) {
    const mins = Math.max(1, Math.round((j.finishedAt - startedAt) / 60000));
    sub = j.error || (failed.length ? failed.map((i) => `${i.title}: ${i.error || "failed"}`).join("\n")
                                    : `${mins} min`);
  }
  $("#jobSub").textContent = sub;
}

function onJob(job, first = false) {
  // A run waiting for its player is still under way: when it carries on and
  // ends inside one update, the popup sees paused, then done - and that end
  // is to be shown, as if it had watched it run.
  const wasRunning = !!(JOB.current || (JOB.paused && JOB.queue && JOB.queue.length));
  JOB = job;
  if ((first && JOB.last && !JOB.seen) || (wasRunning && !JOB.current)) SHOW_LAST = true;
  // Shown here, so seen: the check mark on the toolbar icon can go.
  if (SHOW_LAST && !JOB.current && !JOB.seen) send({ type: "jobSeen" }).catch(() => {});
  render();
}

/* --------------------------------------------------------------- render */
function matches(r) {
  const q = $("#filter").value.trim().toLowerCase();
  if (!q) return true;
  const i = r.item || {};
  return [r.title, r.sub, i.author, i.narrator, i.series, i.subtitle]
    .filter(Boolean).join(" ").toLowerCase().includes(q);
}

function visible() {
  return rowsFor[TAB]().filter(matches);
}

function render() {
  for (const b of document.querySelectorAll(".tab")) {
    const on = b.dataset.tab === TAB;
    b.setAttribute("aria-selected", String(on));
    b.tabIndex = on ? 0 : -1;
  }
  const counted = KNOWN && !NO_PLAYER;
  const filtering = !!$("#filter").value.trim();
  document.querySelector(".tabs").classList.toggle("counted", filtering);
  for (const tab of ["server", "device", "only"]) {
    const all = rowsFor[tab]();
    // The count says what the list below it shows, by the same rule.
    $("#n-" + tab).textContent = !counted ? ""
      : filtering ? `${all.filter(matches).length} of ${all.length}` : String(all.length);
  }
  $("#libCount").textContent = counted
    ? `${ST.serverOnly.length + ST.both.length} books` : "";
  if (!NO_PLAYER) $("#free").textContent = counted ? freeOf(ST.free) : "";

  const rows = visible();
  const ul = $("#list");
  ul.innerHTML = "";
  if (NO_PLAYER) {
    ul.appendChild(el("li", "empty", "Choose your player to see what is on it."));
  } else if (!rows.length && KNOWN) {
    ul.appendChild(el("li", "empty", filtering ? "Nothing matches." : {
      server: "Everything in the library is already on the player.",
      device: "Nothing from this library is on the player yet.",
      only: "Nothing on the player that the library does not have.",
    }[TAB]));
  }
  for (const r of rows) {
    const found = jobItem(r.key);
    const busy = found && (found.it.state === "waiting" || found.it.state === "copying");
    if (busy) SEL.delete(r.key);
    const li = el("li");
    const head = el("div", "rowhead");
    const row = el("label", "book" + (busy ? " busy" : ""));
    li.classList.toggle("sel", SEL.has(r.key));
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = SEL.has(r.key);
    cb.disabled = !!busy;
    cb.addEventListener("change", () => {
      cb.checked ? SEL.add(r.key) : SEL.delete(r.key);
      li.classList.toggle("sel", cb.checked);
      updateAction();
    });
    const meta = el("span", "meta");
    meta.append(el("span", "t", r.title));
    const working = found && found.it.state === "copying";
    // The pill says what is happening; this line keeps saying which version.
    meta.append(el("span", "a", working && found.it.total > 1
      ? `${r.sub} · file ${found.it.done} of ${found.it.total}` : r.sub));
    if (working) {
      const bar = el("span", "bar" + (found.it.total > 1 ? "" : " indeterminate"));
      const i = document.createElement("i");
      if (found.it.total > 1) i.style.width = `${Math.round(100 * found.it.done / found.it.total)}%`;
      bar.appendChild(i);
      meta.appendChild(bar);
    }
    const right = found ? pill(found) : el("span", "size", r.bytes ? size(r.bytes) : "");
    row.append(cb, meta, right);
    const more = el("button", "more");
    more.type = "button";
    const open = OPEN.has(r.key);
    more.setAttribute("aria-expanded", String(open));
    more.setAttribute("aria-label", open ? "Hide details" : "Show details");
    more.title = open ? "Hide details" : "Show details";
    more.textContent = open ? "⌃" : "⌄";
    more.addEventListener("click", () => {
      open ? OPEN.delete(r.key) : OPEN.add(r.key);
      render();
    });
    head.append(row, more);
    li.append(head);
    if (open) {
      const dl = el("dl", "detail");
      for (const [k, v] of details(r)) dl.append(el("dt", "", k), el("dd", "", v));
      li.append(dl);
    }
    ul.appendChild(li);
  }
  const all = $("#all");
  const pickable = rows.filter((r) => { const f = jobItem(r.key); return !f || !["waiting", "copying"].includes(f.it.state); });
  all.checked = pickable.length > 0 && pickable.every((r) => SEL.has(r.key));
  all.disabled = !pickable.length;
  renderJob();
  updateAction();
}

function updateAction() {
  const rows = visible().filter((r) => SEL.has(r.key));
  const btn = $("#act");
  const bytes = rows.reduce((n, r) => n + (Number(r.bytes) || 0), 0);
  const free = ST.free && ST.free.free;
  let sub = "";
  if (rows.length) {
    if (TAB === "server") {
      sub = !free ? size(bytes)
        : bytes > free ? `${size(bytes)} - ${size(bytes - free)} more than is free`
        : `${size(bytes)} of ${size(free)} free`;
    } else if (TAB === "device") {
      sub = `Frees ${size(bytes)}`;
    } else {
      sub = size(bytes);
    }
  }
  // While something runs, the footer is its progress; the button comes back
  // only to add to the queue.
  const running = !!JOB.current;
  btn.classList.toggle("hidden", SETUP || PANEL_OPEN || (running && !rows.length));
  btn.disabled = !rows.length;
  $("#actLabel").textContent = !rows.length ? ACTION[TAB].none
    : running ? `Add ${rows.length} to the queue` : ACTION[TAB].label(rows.length);
  $("#actSub").textContent = sub;
  btn.classList.toggle("danger", TAB === "device" && rows.length > 0);
}

/* ------------------------------------------------------------ lifecycle */
function showStatus(st) {
  ST = st;
  KNOWN = true;
  NO_PLAYER = false;
  for (const k of [...SEL]) if (!visible().some((r) => r.key === k)) SEL.delete(k);
  render();
}

async function refresh() {
  try {
    showStatus(await send({ type: "status" }));
    if (!ST.both.length && !ST.serverOnly.length && !ST.deviceOnly.length) {
      status("Nothing on either side yet.");
    }
  } catch (e) {
    const msg = String(e.message || e);
    ST = { both: [], serverOnly: [], deviceOnly: [] };
    KNOWN = false;
    render();
    if (BACKEND === "helper" && /device not mounted at|no device path is set/.test(msg)) {
      status("");
      await needPlayer(PLAYER.devicePath
        ? `${baseName(PLAYER.devicePath)} isn't plugged in. Connect it, or pick another:`
        : "Choose the player to copy books to:");
      return;
    }
    status(msg, "err");
  }
}

/* Something has to happen before there is anything to copy. Say what, with
 * the one button that does it, in place of a shelf whose every control could
 * only do nothing. Each step is [text] or [text, code]. */
function setup({ title, why = "", steps = [], get = null, opts = false, retry = false }) {
  $("#setupTitle").textContent = title;
  $("#setupWhy").textContent = why;
  const ol = $("#setupSteps");
  ol.replaceChildren(...steps.map(([t, code]) => {
    const li = el("li", "", t);
    if (code) li.append(" ", el("code", "", code));
    return li;
  }));
  const a = $("#setupGet");
  a.classList.toggle("hidden", !get);
  if (get) {
    a.href = get.url;
    a.textContent = get.label;
  }
  // Only beside an install: a Snap or Flatpak browser on Linux reaches the
  // helper differently, or not at all.
  const sandbox = get && ABSH.sandboxHelp(navigator.platform, globalThis.ABSH_CONFIG);
  const sb = $("#setupSandbox");
  if (sandbox) {
    sb.href = sandbox.url;
    sb.textContent = sandbox.label;
  } else {
    sb.removeAttribute("href");
    sb.textContent = "";
  }
  $("#setupOpts").classList.toggle("hidden", !opts);
  $("#setupRetry").classList.toggle("hidden", !retry);
  $("#setup").classList.remove("hidden");
  $("#shelfView").classList.add("hidden");
  $("#act").classList.add("hidden");
  SETUP = true;
}

function clearSetup() {
  if (!SETUP) return;
  SETUP = false;
  $("#setup").classList.add("hidden");
  $("#shelfView").classList.remove("hidden");
  $("#act").classList.remove("hidden");
}

/* The helper, from the release this extension came from. */
function getHelper() {
  return ABSH.helperDownload(globalThis.ABSH_CONFIG);
}

/* A line under the status that opens Settings. Only the folder backend
 * (Chrome) asks for one: what fixes its problems is a click there. */
function optionsLink(text) {
  const a = el("a", "note-link options-link", text);
  a.href = "#";
  a.addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });
  $("#status").after(a);
}

/* -------------------------------------------------------------- the player
 * The device is the one setting that changes between visits, so it is chosen
 * here rather than in Settings.
 *
 * What you type is saved as you type it: a popup closes the moment it loses
 * focus, and a Save button would lose whatever was typed before it was
 * pressed. What the extension suggests is only a suggestion - it is
 * pre-selected, in the machine's colour, and nothing is saved until you
 * press Use. */
const PLAYER = { devicePath: "", subdir: ABSH.DEFAULTS.subdir || "AUDIOBOOKS" };
let BACKEND = "helper";                // or "folder" (Chrome without the helper)
let DEVICES = [];

function showPlayer(name, { missing = false, was = "" } = {}) {
  const n = $("#playerName");
  n.textContent = name;
  n.classList.toggle("missing", missing);
  $("#playerToggle").classList.toggle("missing", missing);
  $("#playerToggle").title = BACKEND === "folder" ? "Change player" : (PLAYER.devicePath || "Choose player");
  $("#playerWas").textContent = was;
  if (missing) $("#free").textContent = "";
}

function showSavedPlayer() {
  if (BACKEND === "folder") return;
  showPlayer(PLAYER.devicePath ? baseName(PLAYER.devicePath) : "Not chosen");
}

function playerNote(text, cls = "") {
  const n = $("#playerNote");
  n.textContent = text || "";
  n.className = "player-note " + cls;
}

function openPlayer(open) {
  PANEL_OPEN = open;
  $("#playerPanel").classList.toggle("hidden", !open);
  $("#shelfView").classList.toggle("hidden", open);
  $("#act").classList.toggle("hidden", open);
  $("#use").classList.toggle("hidden", !open);
  // Cancel goes back to a shelf - there is none to go back to without a player.
  $("#cancelPlayer").classList.toggle("hidden", !open || NO_PLAYER);
  $("#playerToggle").setAttribute("aria-expanded", String(open));
  if (open) updateUse();
}

async function closePlayer() {
  openPlayer(false);
  playerNote("");
  await load();
}

async function readPlayer() {
  const s = await browser.storage.local.get({ devicePath: "", subdir: PLAYER.subdir, absUrl: "" });
  PLAYER.devicePath = s.devicePath || "";
  PLAYER.subdir = s.subdir;
  $("#devicePath").value = PLAYER.devicePath;
  $("#subdir").value = PLAYER.subdir;
  try {
    LIB = s.absUrl ? new URL(s.absUrl).host : "";
  } catch {
    LIB = "";
  }
  $("#libName").textContent = LIB || "Not set";
}

async function savePlayer(patch) {
  Object.assign(PLAYER, patch);
  await browser.storage.local.set(patch);
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
const saveTyped = debounce((patch) => savePlayer(patch), 250);

/** Mark one detected player as chosen. `how` is "picked" when you chose it,
 *  "guess" when the extension did. */
function choose(d, how) {
  for (const row of document.querySelectorAll(".player-pick")) {
    const on = !!d && row.title === d.path;
    row.classList.toggle("picked", on && how === "picked");
    row.classList.toggle("guess", on && how === "guess");
    row.querySelector("input").checked = on;
  }
  const path = $("#devicePath");
  if (d) path.value = d.path;
  path.classList.toggle("machine", !!d && how === "guess");
  updateUse();
}

function updateUse() {
  const btn = $("#use");
  if (BACKEND === "folder") {
    btn.disabled = false;
    $("#useLabel").textContent = `Use ${playerLabel()}`;
    return;
  }
  const path = $("#devicePath").value.trim();
  const d = DEVICES.find((x) => x.path === path);
  // The player that just went missing is not a choice; plug it in and Detect.
  const gone = NO_PLAYER && !d && path === PLAYER.devicePath;
  btn.disabled = !path || gone;
  $("#useLabel").textContent = !path ? "Choose a player"
    : gone ? `${baseName(path)} isn't plugged in` : `Use ${d ? d.name : baseName(path)}`;
}

/** What makes a detected player look like yours, in a few words. */
function evidence(d) {
  if (d.slow) return "Not answering";
  if (!d.hasSubdir) return "No books folder";
  return `${PLAYER.subdir || "AUDIOBOOKS"}/ · ${d.books} book${d.books === 1 ? "" : "s"}`;
}

function pickRow(d) {
  const row = el("label", "player-pick" + (d.path === PLAYER.devicePath ? " current" : ""));
  row.title = d.path;
  const radio = document.createElement("input");
  radio.type = "radio";
  radio.name = "player";
  // click, not change: clicking the suggestion that is already selected is
  // still you choosing it, and that fires no change.
  radio.addEventListener("click", () => choose(d, "picked"));
  const who = el("span", "who");
  const n = el("span", "n", d.name || d.path);
  if (d.path === PLAYER.devicePath) n.append(el("span", "in-use", "in use"));
  who.append(n, el("span", "why", evidence(d)));
  const free = d.total ? `${size(d.free).split(" ")[0]} / ${size(d.total)}` : "";
  if (d.slow) row.classList.add("slow");
  row.append(radio, who, el("span", "d", free));
  return row;
}

async function detect() {
  const btn = $("#detect");
  const lbl = $("#detect .lbl");
  if (btn.disabled) return DEVICES;
  btn.disabled = true;
  lbl.textContent = "Looking…";
  const list = $("#playerList");
  try {
    const devices = await send({ type: "devices" });
    DEVICES = devices || [];
    list.innerHTML = "";
    if (!DEVICES.length) {
      playerNote("No player is plugged in. Some players have to be switched to " +
                 "USB storage (MSC) mode before they show up as a drive.", "err");
      updateUse();
      return [];
    }
    for (const d of DEVICES) list.appendChild(pickRow(d));
    // On a Mac, a USB player that will not answer is most often waiting on
    // macOS's "would like to access files on a removable volume", which holds
    // the helper's first look at it until someone clicks Allow (N7).
    const hint = ABSH.slowPlayerHint(DEVICES, navigator.platform || navigator.userAgent);
    if (hint) {
      const was = $("#playerNote").textContent;
      playerNote(was ? `${was} ${hint}` : hint, $("#playerNote").className.includes("err") ? "err" : "");
    }
    // The player you chose before, if it is here; otherwise the one most like
    // a player - or the only one there is - offered rather than taken. A path
    // you typed that matches nothing here is left as you typed it.
    const typed = $("#devicePath").value.trim();
    const current = DEVICES.find((d) => d.path === typed);
    const likely = DEVICES[0].score > 0 || DEVICES.length === 1;
    if (current) choose(current, "picked");
    else if ((NO_PLAYER || !typed) && likely) choose(DEVICES[0], "guess");
    else updateUse();
    return DEVICES;
  } catch (e) {
    playerNote("Couldn't ask the helper for players: " + (e.message || e), "err");
    return [];
  } finally {
    btn.disabled = false;
    lbl.textContent = "Detect";
  }
}

/* The player isn't usable: open the chooser with what is plugged in, so
 * picking the right one is a click rather than a trip to Settings. */
async function needPlayer(why) {
  NO_PLAYER = true;
  KNOWN = false;
  ST = { both: [], serverOnly: [], deviceOnly: [] };
  render();
  if (PLAYER.devicePath) {
    showPlayer("Not connected", { missing: true, was: baseName(PLAYER.devicePath) });
  } else {
    showPlayer("Not chosen");
  }
  playerNote(why, PLAYER.devicePath ? "err" : "");
  openPlayer(true);
  await detect();
}

$("#playerToggle").addEventListener("click", async () => {
  if (PANEL_OPEN) {
    // Nothing to go back to without a player; stay on the chooser.
    if (!NO_PLAYER) await closePlayer();
    return;
  }
  openPlayer(true);
  if (BACKEND === "helper" && !$("#playerList").children.length) await detect();
});
$("#detect").addEventListener("click", detect);
$("#cancelPlayer").addEventListener("click", closePlayer);
$("#use").addEventListener("click", async () => {
  if (BACKEND === "helper") {
    const path = $("#devicePath").value.trim();
    if (!path) return;
    await savePlayer({ devicePath: path });
  }
  await savePlayer({ subdir: $("#subdir").value.trim() });
  NO_PLAYER = false;
  showSavedPlayer();
  await closePlayer();
});
$("#devicePath").addEventListener("input", () => {
  const path = $("#devicePath").value.trim();
  saveTyped({ devicePath: path });
  // Typed by you, so no longer the machine's suggestion.
  const d = DEVICES.find((x) => x.path === path);
  choose(d || null, "picked");
});
$("#devicePath").addEventListener("change", () => savePlayer({ devicePath: $("#devicePath").value.trim() }));
$("#subdir").addEventListener("input", () => saveTyped({ subdir: $("#subdir").value.trim() }));
$("#subdir").addEventListener("change", () => savePlayer({ subdir: $("#subdir").value.trim() }));
$("#folderChange").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });

async function load() {
  status("Checking the helper…");
  // Links a previous pass added under the status; nothing else carries the class.
  for (const a of document.querySelectorAll("a.options-link")) a.remove();
  clearSetup();
  let p;
  try {
    p = await send({ type: "ping" });
    if (!p.ok) {
      // Not installed, and installed but failing to start, are different
      // problems. Only the second has a reason worth showing: the browser's
      // own words for it.
      const absent = ABSH.helperAbsent(p.error);
      status("");
      $("#playerName").textContent = "No helper";
      const browserName = /Firefox\//.test(navigator.userAgent) ? "Firefox" : "the browser";
      if (ABSH.helperStuck(p.error)) {
        // Installed and started, then silent. Reinstalling would not help;
        // a fresh start usually does, and Look again makes one.
        setup({ title: "Helper not answering", why: p.error, retry: true,
                steps: [[`If it happens again, restart ${browserName}.`]] });
        return;
      }
      setup({
        title: absent ? "Install the helper app" : "Reinstall the helper app",
        why: absent ? "" : p.error,
        steps: [["Unzip it."], ["In that folder, run", "python3 install.py"],
                [`Restart ${browserName}.`]],
        get: getHelper(),
      });
      if (p.folderAvailable) optionsLink("Or, in Chrome, choose your player's folder in Settings");
      return;
    }
    BACKEND = p.backend === "folder" ? "folder" : "helper";
    FOLDER = p.folder || "";
    $("#pathField").classList.toggle("hidden", BACKEND === "folder");
    $("#detect").classList.toggle("hidden", BACKEND === "folder");
    $("#folderChange").classList.toggle("hidden", BACKEND !== "folder");
    $("#pollNote").classList.toggle("hidden", !(BACKEND === "helper" && p.polls === true));
    if (BACKEND === "folder") {
      const gone = p.access === "prompt" || p.access === "missing";
      showPlayer(gone ? "Not available" : FOLDER, { missing: gone, was: gone ? FOLDER : "" });
    } else {
      showSavedPlayer();
    }
    // Chrome without the helper: the folder has to be usable before anything
    // on it can be listed. The popup cannot restore access itself - Chrome
    // only renews it from one of the extension's tabs - so it sends you there.
    if (p.backend === "folder" && p.access === "prompt") {
      status("");
      setup({ title: "Allow access again", why: `Chrome paused access to “${p.folder}”.`,
              opts: true });
      return;
    }
    if (p.backend === "folder" && p.access === "missing") {
      status("");
      setup({ title: "Plug the player in", why: `“${p.folder}” isn't there.`, retry: true });
      return;
    }
    if (!p.configured) {
      const missing = p.missing || [];
      const serverGaps = missing.filter((m) => !m.startsWith("devicePath"));
      if (serverGaps.length) {
        status("");
        setup({ title: "Connect your library", opts: true });
        return;
      }
      // Only the player is missing, and that is chosen right here.
      status("");
      await needPlayer("Choose the player to copy books to:");
      return;
    }
  } catch (e) {
    status(String(e.message || e), "err");
    return;
  }
  // Not awaited: the shelf should not wait on GitHub.
  updateHint();
  // What the background already knows, at once: a copy under way, and the
  // shelf as last read. A fresh read waits behind a running copy, so it is not
  // asked for then; the background sends the new shelf when the copy ends.
  const job = await send({ type: "jobState" }).catch(() => null);
  if (job) onJob(job, true);
  const cached = await send({ type: "lastStatus" }).catch(() => null);
  if (cached) showStatus(cached);
  status(cached ? "" : "Reading the player…");
  await refresh();
  if ($("#status").textContent === "Reading the player…") status("");
  // Set last: refresh() reports its own outcome, and this should be what
  // remains on screen when everything is fine.
  if (!$("#status").classList.contains("err") && !NO_PLAYER) {
    status(p.backend === "folder"
      ? `using the folder “${p.folder}”, not the helper (tags: ${p.tags})`
      : `helper ok (${p.release || p.version}, tags: ${p.tags})`, "ok");
  }
}

/* One line, and only when there is something to do. The popup is where people
 * actually are, so it is where a waiting update gets noticed; the settings
 * page is where it is explained and installed. Nothing is said on the library
 * page itself - that is Audiobookshelf's screen, not ours to nag on. */
async function updateHint() {
  try {
    const { helper, check } = await send({ type: "updateStatus" });
    const s = ABSH.updateState(helper, check);
    if (s.kind !== "available") return;
    const a = $("#updateHint");
    a.textContent = `Helper ${s.latest} is available - ` +
                    (s.canUpdate ? "update it in Settings" : "see Settings");
    a.classList.remove("hidden");
  } catch { /* a failed check says nothing here; Settings says why */ }
}

/* ---------------------------------------------------------------- wiring */
$("#filter").addEventListener("input", render);
$("#opts").addEventListener("click", () => browser.runtime.openOptionsPage());
$("#setupOpts").addEventListener("click", () => browser.runtime.openOptionsPage());
$("#setupRetry").addEventListener("click", () => load());
$("#updateHint").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });
$("#refresh").addEventListener("click", refresh);
$("#all").addEventListener("change", () => {
  const rows = visible();
  rows.forEach((r) => $("#all").checked ? SEL.add(r.key) : SEL.delete(r.key));
  render();
});
const TABS = [...document.querySelectorAll(".tab")];
function selectTab(name) {
  TAB = name;
  SEL.clear();
  render();
}
for (const b of TABS) {
  b.addEventListener("click", () => selectTab(b.dataset.tab));
  // A tablist moves with the arrow keys.
  b.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const next = TABS[(TABS.indexOf(b) + step + TABS.length) % TABS.length];
    selectTab(next.dataset.tab);
    next.focus();
  });
}
// A run waiting for its player can be let go: what is copied stays.
$("#jobCancel").addEventListener("click", async () => {
  try {
    SHOW_LAST = true;
    onJob(await send({ type: "jobCancel" }));
  } catch (e) {
    status(String(e.message || e), "err");
  }
});

/* Whether a copy writes Audiobookshelf's tags into the files: true, false,
 * or null when the question was let go. Asked only when the setting says so. */
let ASKING = null;
async function askTags() {
  const t = (await send({ type: "tagsAsk" })) || {};
  if (!t.ask) return t.write;
  const box = $("#tagsAsk");
  $("#tagsPlayer").textContent = playerLabel();
  $("#tagsKeep").checked = false;
  box.classList.remove("hidden");
  $("#tagsYes").focus();
  return new Promise((resolve) => {
    ASKING = (v) => {
      ASKING = null;
      box.classList.add("hidden");
      if (v !== null && $("#tagsKeep").checked) {
        send({ type: "tagsRemember", value: v ? "always" : "never" }).catch(() => {});
      }
      resolve(v);
    };
  });
}
$("#tagsYes").addEventListener("click", () => ASKING && ASKING(true));
$("#tagsNo").addEventListener("click", () => ASKING && ASKING(false));

$("#act").addEventListener("click", async () => {
  const rows = visible().filter((r) => SEL.has(r.key));
  if (!rows.length) return;
  const a = ACTION[TAB];
  try {
    SHOW_LAST = false;
    const writeTags = a.op === "pull" ? await askTags() : undefined;
    if (writeTags === null) return;
    onJob(await send({ type: "jobAdd", op: a.op, player: playerLabel(), writeTags,
                       items: rows.map((r) => ({ key: r.key, ref: a.ref(r), title: r.title,
                                                 bytes: r.bytes })) }));
    SEL.clear();
    render();
  } catch (e) {
    status(String(e.message || e), "err");
  }
});

/* Every action has a key, and the key is printed on it. */
document.addEventListener("keydown", (e) => {
  if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
  // The tags question is the only thing the keys answer while it is up.
  if (ASKING) {
    const k = e.key.toLowerCase();
    if (k === "escape" || k === "y" || k === "n") {
      e.preventDefault();
      ASKING(k === "escape" ? null : k === "y");
    }
    return;
  }
  const t = e.target;
  if (e.key === "Enter") {
    // A focused button or link does its own thing on Enter.
    if (t.closest && t.closest("button, a")) return;
    const btn = PANEL_OPEN ? $("#use") : $("#act");
    if (!btn.disabled) {
      e.preventDefault();
      btn.click();
    }
    return;
  }
  if (e.key === "Escape" && PANEL_OPEN && !NO_PLAYER) {
    e.preventDefault();
    closePlayer();
    return;
  }
  if (t.matches && t.matches("input[type=text], input[type=search]")) return;
  const k = e.key.toLowerCase();
  if (k === ",") {
    e.preventDefault();
    browser.runtime.openOptionsPage();
  } else if (k === "r" && SETUP && !$("#setupRetry").classList.contains("hidden")) {
    e.preventDefault();
    load();
  } else if (SETUP) {
    // Nothing on the shelf to act on yet.
  } else if (k === "/" && !PANEL_OPEN) {
    e.preventDefault();
    $("#filter").focus();
  } else if (k === "d" && PANEL_OPEN && BACKEND === "helper") {
    e.preventDefault();
    detect();
  } else if (k === "r" && !PANEL_OPEN) {
    e.preventDefault();
    $("#refresh").click();
  }
});

readPlayer().then(load);
