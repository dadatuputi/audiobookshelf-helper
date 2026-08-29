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
  if (!rows.length) {
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
    SEL.clear();
    render();
    if (!ST.both.length && !ST.serverOnly.length && !ST.deviceOnly.length) {
      status("Nothing on either side yet.");
    }
  } catch (e) {
    status(String(e.message || e), "err");
    ST = { both: [], serverOnly: [], deviceOnly: [] };
    render();
  }
}

async function load() {
  status("checking helper…");
  let p;
  try {
    p = await send({ type: "ping" });
    if (!p.ok) {
      status("Native helper not reachable.\nRun native/install.py, then restart the browser.\n"
             + (p.error || ""), "err");
      return;
    }
    if (!p.configured) {
      status("Not configured yet: " + (p.missing || []).join(", ")
             + "\nOpen options, or run `absh config`.", "err");
      return;
    }
  } catch (e) {
    status(String(e.message || e), "err");
    return;
  }
  await refresh();
  // Set last: refresh() reports its own outcome, and this should be what
  // remains on screen when everything is fine.
  if (!$("#status").classList.contains("err")) {
    status(`helper ok (${p.version}, tags: ${p.tags})`, "ok");
  }
}

/* ---------------------------------------------------------------- wiring */
$("#filter").addEventListener("input", render);
$("#opts").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });
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

load();
