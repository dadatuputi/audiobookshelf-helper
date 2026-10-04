/* Popup: one list, three views, over the same status the CLI shows.
 *
 * The helper does the work and owns the Audiobookshelf client; this only picks
 * things and renders what came back. */
const $ = (s) => document.querySelector(s);

let TAB = "server";                    // server | device | only
let ST = { both: [], serverOnly: [], deviceOnly: [] };
let SEL = new Set();
let PROGRESS = null;
let BUSY = false;
// No usable player: the shelf has nothing true to say about "the device".
let NO_PLAYER = false;

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

const rowsFor = {
  server: () => ST.serverOnly.map((i) => ({ key: i.id, id: i.id, title: i.title,
    sub: [i.author, ABSH.formatBytes(i.size)].filter(Boolean).join(" · "), bytes: i.size })),
  device: () => ST.both.map((b) => ({ key: b.name, name: b.name, id: b.itemId, title: b.title,
    sub: [b.author, ABSH.formatBytes(b.bytes), b.matchedBy && b.matchedBy !== "id"
      ? `matched by ${b.matchedBy}` : ""].filter(Boolean).join(" · "), bytes: b.bytes })),
  only: () => ST.deviceOnly.map((e) => ({ key: e.name, name: e.name, title: e.title || e.name,
    sub: [e.author || "unknown author", ABSH.formatBytes(e.bytes)].join(" · "), bytes: e.bytes })),
};

const ACTION = {
  server: { label: (n) => `Copy ${n} to device`, run: (rows) =>
    send({ type: "pull", ids: rows.map((r) => r.id) }, onProgress) },
  device: { label: (n) => `Remove ${n} from device`, run: (rows) =>
    send({ type: "remove", names: rows.map((r) => r.name) }) },
  only: { label: (n) => `Upload ${n} to server`, run: (rows) =>
    send({ type: "push", names: rows.map((r) => r.name) }, onProgress) },
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
    b.classList.toggle("active", b.dataset.tab === TAB);
  }
  for (const [tab, n] of [["server", ST.serverOnly.length], ["device", ST.both.length],
                          ["only", ST.deviceOnly.length]]) {
    const badge = $("#n-" + tab);
    badge.textContent = n || "";
    badge.classList.toggle("show", n > 0);
  }
  $("#free").textContent = ST.free && ST.free.free ? `${ABSH.formatBytes(ST.free.free)} free` : "";

  const rows = visible();
  const ul = $("#list");
  ul.innerHTML = "";
  if (NO_PLAYER) {
    ul.appendChild(el("li", "empty", "Pick your player above to see what is on it."));
  } else if (!rows.length) {
    ul.appendChild(el("li", "empty", {
      server: "Everything on the server is already on the device.",
      device: "Nothing from this library is on the device yet.",
      only: "Nothing on the device that the server does not have.",
    }[TAB]));
  }
  for (const r of rows) {
    const li = el("li");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = SEL.has(r.key);
    cb.disabled = BUSY;
    cb.addEventListener("change", () => {
      cb.checked ? SEL.add(r.key) : SEL.delete(r.key);
      updateAction();
    });
    const meta = el("div", "meta");
    const t = el("span", "t", r.title);
    if (PROGRESS && PROGRESS.title === r.title) t.appendChild(el("span", "chip working", "…"));
    meta.append(t, el("span", "a", r.sub));
    if (PROGRESS && PROGRESS.title === r.title && PROGRESS.total > 1) {
      const bar = el("div", "bar");
      const i = document.createElement("i");
      i.style.width = `${Math.round(100 * PROGRESS.done / PROGRESS.total)}%`;
      bar.appendChild(i);
      meta.appendChild(bar);
    }
    li.append(cb, meta);
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
  btn.disabled = BUSY || !rows.length;
  btn.textContent = rows.length ? ACTION[TAB].label(rows.length) : "Select books";
  btn.classList.toggle("danger", TAB === "device" && rows.length > 0);
}

/* ------------------------------------------------------------ lifecycle */
async function refresh() {
  try {
    ST = await send({ type: "status" });
    NO_PLAYER = false;
    SEL.clear();
    render();
    if (!ST.both.length && !ST.serverOnly.length && !ST.deviceOnly.length) {
      status("Nothing on either side yet.");
    }
  } catch (e) {
    const msg = String(e.message || e);
    ST = { both: [], serverOnly: [], deviceOnly: [] };
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

/* A line under the status that opens Options. Only the folder backend
 * (Chrome) asks for one: what fixes its problems is a click there. */
function optionsLink(text) {
  const a = el("a", "update-hint", text);
  a.href = "#";
  a.addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });
  $("#status").after(a);
}

/* -------------------------------------------------------------- the player
 * The device is the one setting that changes between visits, so it is chosen
 * here rather than in Options. Every change is saved as it is made: a popup
 * closes the moment it loses focus, and a Save button would lose whatever was
 * typed before it was pressed. */
const PLAYER = { devicePath: "", subdir: ABSH.DEFAULTS.subdir || "AUDIOBOOKS" };
let BACKEND = "helper";                // or "folder" (Chrome without the helper)

function baseName(path) {
  const parts = String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || String(path || "");
}

function showPlayer(name, missing = false) {
  const n = $("#playerName");
  n.textContent = name;
  n.title = BACKEND === "folder" ? "" : PLAYER.devicePath;
  n.classList.toggle("missing", missing);
}

function playerNote(text, cls = "") {
  const n = $("#playerNote");
  n.textContent = text || "";
  n.className = "player-note " + cls;
}

function openPlayer(open) {
  $("#playerPanel").classList.toggle("hidden", !open);
  $("#playerToggle").textContent = open ? "Done" : "Change";
  $("#playerToggle").setAttribute("aria-expanded", String(open));
}

async function readPlayer() {
  const s = await browser.storage.local.get({ devicePath: "", subdir: PLAYER.subdir });
  PLAYER.devicePath = s.devicePath || "";
  PLAYER.subdir = s.subdir;
  $("#devicePath").value = PLAYER.devicePath;
  $("#subdir").value = PLAYER.subdir;
}

async function savePlayer(patch) {
  Object.assign(PLAYER, patch);
  await browser.storage.local.set(patch);
  if (BACKEND === "helper") {
    showPlayer(PLAYER.devicePath ? baseName(PLAYER.devicePath) : "not chosen");
  }
}

/* Saved as you type (so closing the popup loses nothing), re-read once you
 * stop: Enter, or leaving the field. */
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
const saveTyped = debounce((patch) => savePlayer(patch), 250);

function freeText(d) {
  return d.free ? `${ABSH.formatBytes(d.free)} free` : "";
}

async function detect() {
  const btn = $("#detect");
  btn.disabled = true;
  btn.textContent = "Looking…";
  const list = $("#playerList");
  try {
    const devices = await send({ type: "devices" });
    list.innerHTML = "";
    if (!devices || !devices.length) {
      playerNote("No player is plugged in. Some players have to be switched to " +
                 "USB storage (MSC) mode before they show up as a drive.", "err");
      return [];
    }
    for (const d of devices) {
      const b = el("button", "player-pick" + (d.path === PLAYER.devicePath ? " current" : ""));
      b.type = "button";
      b.setAttribute("role", "listitem");
      b.title = d.path;
      b.append(el("span", "n", d.name || d.path));
      if (d.hasSubdir) b.append(el("span", "chip on", "has your books"));
      b.append(el("span", "d", freeText(d)));
      b.addEventListener("click", async () => {
        $("#devicePath").value = d.path;
        await savePlayer({ devicePath: d.path });
        openPlayer(false);
        playerNote("");
        await load();
      });
      list.appendChild(b);
    }
    return devices;
  } catch (e) {
    playerNote("Couldn't ask the helper for players: " + (e.message || e), "err");
    return [];
  } finally {
    btn.disabled = false;
    btn.textContent = "Detect";
  }
}

/* The player isn't usable: open the strip with what is plugged in, so picking
 * the right one is a click rather than a trip to Options. */
async function needPlayer(why) {
  NO_PLAYER = true;
  ST = { both: [], serverOnly: [], deviceOnly: [] };
  render();
  showPlayer(PLAYER.devicePath ? baseName(PLAYER.devicePath) : "not chosen", true);
  playerNote(why, "err");
  openPlayer(true);
  await detect();
}

$("#playerToggle").addEventListener("click", async () => {
  const open = $("#playerPanel").classList.contains("hidden");
  openPlayer(open);
  if (open && BACKEND === "helper" && !$("#playerList").children.length) await detect();
  if (!open) await load();
});
$("#detect").addEventListener("click", detect);
$("#devicePath").addEventListener("input", () => saveTyped({ devicePath: $("#devicePath").value.trim() }));
$("#devicePath").addEventListener("change", async () => {
  await savePlayer({ devicePath: $("#devicePath").value.trim() });
  await load();
});
$("#subdir").addEventListener("input", () => saveTyped({ subdir: $("#subdir").value.trim() }));
$("#subdir").addEventListener("change", async () => {
  await savePlayer({ subdir: $("#subdir").value.trim() });
  await refresh();
});
$("#folderChange").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });

async function load() {
  status("checking helper…");
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
    $("#pathField").classList.toggle("hidden", BACKEND === "folder");
    $("#folderChange").classList.toggle("hidden", BACKEND !== "folder");
    $("#pollNote").classList.toggle("hidden", !(BACKEND === "helper" && p.polls === true));
    if (BACKEND === "folder") showPlayer(`folder “${p.folder}”`, p.access === "prompt" || p.access === "missing");
    else showPlayer(PLAYER.devicePath ? baseName(PLAYER.devicePath) : "not chosen");
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
  if (!$("#status").classList.contains("err")) {
    status(p.backend === "folder"
      ? `using the folder “${p.folder}”, not the helper (tags: ${p.tags})`
      : `helper ok (${p.release || p.version}, tags: ${p.tags})`, "ok");
  }
}

/* One line, and only when there is something to do. The popup is where people
 * actually are, so it is where a waiting update gets noticed; the options page
 * is where it is explained and installed. Nothing is said on the library page
 * itself - that is Audiobookshelf's screen, not ours to nag on. */
async function updateHint() {
  try {
    const { helper, check } = await send({ type: "updateStatus" });
    const s = ABSH.updateState(helper, check);
    if (s.kind !== "available") return;
    const a = $("#updateHint");
    a.textContent = `Helper ${s.latest} is available - ` +
                    (s.canUpdate ? "update it in Options" : "see Options");
    a.classList.remove("hidden");
  } catch { /* a failed check says nothing here; Options says why */ }
}

/* ---------------------------------------------------------------- wiring */
$("#filter").addEventListener("input", render);
$("#opts").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });
$("#updateHint").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });
$("#refresh").addEventListener("click", refresh);
$("#all").addEventListener("change", () => {
  const rows = visible();
  rows.forEach((r) => $("#all").checked ? SEL.add(r.key) : SEL.delete(r.key));
  render();
});
for (const b of document.querySelectorAll(".tab")) {
  b.addEventListener("click", () => { TAB = b.dataset.tab; SEL.clear(); render(); });
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

readPlayer().then(load);
