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
let PROGRESS = null;
let BUSY = false;
// No usable player: the shelf has nothing true to say about "the device".
let NO_PLAYER = false;
let PANEL_OPEN = false;
let LIB = "";                          // the server's host, as the Library card names it
let FOLDER = "";                       // Chrome's folder, when that is what is in use

/* ----------------------------------------------------------- transport */
const PORT = browser.runtime.connect({ name: "absh" });
let RID = 0;
const WAITING = new Map();

PORT.onMessage.addListener((m) => {
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

const rowsFor = {
  server: () => ST.serverOnly.map((i) => ({ key: i.id, id: i.id, title: i.title,
    sub: i.author || "", bytes: i.size })),
  device: () => ST.both.map((b) => ({ key: b.name, name: b.name, id: b.itemId, title: b.title,
    sub: [b.author, b.matchedBy && b.matchedBy !== "id" ? `matched by ${b.matchedBy}` : ""]
      .filter(Boolean).join(" · "), bytes: b.bytes })),
  only: () => ST.deviceOnly.map((e) => ({ key: e.name, name: e.name, title: e.title || e.name,
    sub: e.author || "unknown author", bytes: e.bytes })),
};

const ACTION = {
  server: {
    none: "Select books to copy",
    label: (n) => `Copy ${n} to ${playerLabel()} →`,
    run: (rows) => send({ type: "pull", ids: rows.map((r) => r.id) }, onProgress),
  },
  device: {
    none: "Select books to remove",
    label: (n) => `Remove ${n} from ${playerLabel()}`,
    run: (rows) => send({ type: "remove", names: rows.map((r) => r.name) }),
  },
  only: {
    none: "Select books to upload",
    label: (n) => `← Upload ${n} to ${LIB || "the library"}`,
    run: (rows) => send({ type: "push", names: rows.map((r) => r.name) }, onProgress),
  },
};

function onProgress(ev) {
  if (ev.event === "item") {
    status(`${ev.op || "working"} ${ev.index}/${ev.count} — ${ev.title}`);
  }
  PROGRESS = { title: ev.title, done: ev.done || 0, total: ev.total || 0 };
  render();
}

/* --------------------------------------------------------------- render */
function visible() {
  const q = $("#filter").value.trim().toLowerCase();
  const rows = rowsFor[TAB]();
  return q ? rows.filter((r) => `${r.title} ${r.sub}`.toLowerCase().includes(q)) : rows;
}

function render() {
  for (const b of document.querySelectorAll(".tab")) {
    const on = b.dataset.tab === TAB;
    b.setAttribute("aria-selected", String(on));
    b.tabIndex = on ? 0 : -1;
  }
  const counted = KNOWN && !NO_PLAYER;
  for (const [tab, n] of [["server", ST.serverOnly.length], ["device", ST.both.length],
                          ["only", ST.deviceOnly.length]]) {
    $("#n-" + tab).textContent = counted ? String(n) : "";
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
    ul.appendChild(el("li", "empty", $("#filter").value.trim() ? "Nothing matches." : {
      server: "Everything in the library is already on the player.",
      device: "Nothing from this library is on the player yet.",
      only: "Nothing on the player that the library does not have.",
    }[TAB]));
  }
  for (const r of rows) {
    const li = el("li");
    const row = el("label", "book" + (SEL.has(r.key) ? " sel" : ""));
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = SEL.has(r.key);
    cb.disabled = BUSY;
    cb.addEventListener("change", () => {
      cb.checked ? SEL.add(r.key) : SEL.delete(r.key);
      row.classList.toggle("sel", cb.checked);
      updateAction();
    });
    const meta = el("span", "meta");
    const t = el("span", "t", r.title);
    const working = PROGRESS && PROGRESS.title === r.title;
    if (working) t.appendChild(el("span", "chip", "copying…"));
    meta.append(t);
    if (r.sub) meta.append(el("span", "a", r.sub));
    if (working && PROGRESS.total > 1) {
      const bar = el("span", "bar");
      const i = document.createElement("i");
      i.style.width = `${Math.round(100 * PROGRESS.done / PROGRESS.total)}%`;
      bar.appendChild(i);
      meta.appendChild(bar);
    }
    row.append(cb, meta, el("span", "size", r.bytes ? size(r.bytes) : ""));
    li.append(row);
    ul.appendChild(li);
  }
  const all = $("#all");
  all.checked = rows.length > 0 && rows.every((r) => SEL.has(r.key));
  all.disabled = BUSY || !rows.length;
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
  btn.disabled = BUSY || !rows.length;
  $("#actLabel").textContent = rows.length ? ACTION[TAB].label(rows.length) : ACTION[TAB].none;
  $("#actSub").textContent = sub;
  btn.classList.toggle("danger", TAB === "device" && rows.length > 0);
}

/* ------------------------------------------------------------ lifecycle */
async function refresh() {
  try {
    ST = await send({ type: "status" });
    KNOWN = true;
    NO_PLAYER = false;
    SEL.clear();
    render();
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

/* A line under the status that opens Settings. Only the folder backend
 * (Chrome) asks for one: what fixes its problems is a click there. */
function optionsLink(text) {
  const a = el("a", "note-link", text);
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
  let p;
  try {
    p = await send({ type: "ping" });
    if (!p.ok) {
      status("Native helper not reachable.\nRun native/install.py, then restart the browser.\n"
             + (p.error || ""), "err");
      if (p.folderAvailable) optionsLink("Or, in Chrome, choose your player's folder in Options");
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
      status(`Chrome has paused access to the folder “${p.folder}”. ` +
             "Allow it again in Options.", "err");
      optionsLink(`Allow access to “${p.folder}” in Options`);
      return;
    }
    if (p.backend === "folder" && p.access === "missing") {
      status(`The folder “${p.folder}” isn't there. Plug the player in, then press ↻.`, "err");
      $("#refresh").addEventListener("click", () => load(), { once: true });
      return;
    }
    if (!p.configured) {
      const missing = p.missing || [];
      const serverGaps = missing.filter((m) => !m.startsWith("devicePath"));
      if (serverGaps.length) {
        status("Set up your server first: " + serverGaps.join(", ")
               + "\nOpen Options, or run `absh config`.", "err");
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
  await refresh();
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
                    (s.canUpdate ? "update it in Options" : "see Options");
    a.classList.remove("hidden");
  } catch { /* a failed check says nothing here; Settings says why */ }
}

/* ---------------------------------------------------------------- wiring */
$("#filter").addEventListener("input", render);
$("#opts").addEventListener("click", () => browser.runtime.openOptionsPage());
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
$("#act").addEventListener("click", async () => {
  const rows = visible().filter((r) => SEL.has(r.key));
  if (!rows.length) return;
  BUSY = true;
  render();
  try {
    const r = await ACTION[TAB].run(rows);
    PROGRESS = null;
    const bits = [];
    if (r.copied) bits.push(`copied ${r.copied} file(s)`);
    if (r.uploaded) bits.push(`uploaded ${r.uploaded}`);
    if (r.removed && r.removed.length) bits.push(`removed ${r.removed.length}`);
    if (r.skipped) bits.push(`skipped ${r.skipped}`);
    status(bits.join(" · ") || "done", r.errors && r.errors.length ? "err" : "ok");
    if (r.errors && r.errors.length) {
      status((bits.join(" · ") + "\n" + r.errors.join("\n")).trim(), "err");
    }
  } catch (e) {
    PROGRESS = null;
    status(String(e.message || e), "err");
  } finally {
    BUSY = false;
    await refresh();
  }
});

/* Every action has a key, and the key is printed on it. */
document.addEventListener("keydown", (e) => {
  if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
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
  if (k === "/" && !PANEL_OPEN) {
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
