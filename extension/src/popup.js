/* Popup: the library picker and the on-device shelf.
 *
 * Talks to the background over a long-lived port rather than sendMessage,
 * because a sync streams progress back while it runs. */
const $ = (s) => document.querySelector(s);

let BOOKS = [];            // library items, annotated with .onDevice
let SEL = new Set();       // selected library item ids
let DEVICE = { onDevice: [], orphans: [], free: "" };
let PROGRESS = null;       // {id, done, total, file} while a sync runs
let BUSY = false;

/* ----------------------------------------------------------- transport */
const PORT = browser.runtime.connect({ name: "absh" });
let RID = 0;
const WAITING = new Map();

PORT.onMessage.addListener((msg) => {
  const w = WAITING.get(msg && msg.rid);
  if (!w) return;
  if (msg.progress) { if (w.onProgress) w.onProgress(msg.progress); return; }
  WAITING.delete(msg.rid);
  msg.ok ? w.resolve(msg.data) : w.reject(new Error(msg.error || "no response"));
});
PORT.onDisconnect.addListener(() => {
  for (const [, w] of WAITING) w.reject(new Error("background disconnected"));
  WAITING.clear();
});

function send(m, onProgress) {
  return new Promise((resolve, reject) => {
    const rid = ++RID;
    WAITING.set(rid, { resolve, reject, onProgress });
    PORT.postMessage({ ...m, rid });
  });
}

/* --------------------------------------------------------------- chrome */
function status(msg, cls = "") {
  const el = $("#status");
  el.textContent = msg || "";
  el.className = "status " + cls;
}

function setTab(name) {
  for (const b of document.querySelectorAll(".tab")) {
    b.classList.toggle("active", b.dataset.tab === name);
  }
  $("#pane-library").classList.toggle("hidden", name !== "library");
  $("#pane-device").classList.toggle("hidden", name !== "device");
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/* ---------------------------------------------------------- library pane */
function renderLibrary() {
  const q = $("#filter").value.trim().toLowerCase();
  const shown = ABSH.filterBooks(BOOKS, q);
  const ul = $("#list");
  ul.innerHTML = "";

  if (!shown.length) {
    ul.appendChild(el("li", "empty", BOOKS.length ? "Nothing matches that filter." : "No books in this library."));
  }

  for (const b of shown) {
    const li = el("li");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = SEL.has(b.id);
    cb.disabled = BUSY;
    cb.addEventListener("change", () => {
      cb.checked ? SEL.add(b.id) : SEL.delete(b.id);
      updateCount();
    });

    const meta = el("div", "meta");
    const t = el("span", "t", b.title);
    if (b.onDevice) t.appendChild(el("span", "chip on", "on device"));
    if (PROGRESS && PROGRESS.id === b.id) {
      t.appendChild(el("span", "chip working", "copying"));
    }
    const bits = [b.author, b.series].filter(Boolean).join(" · ");
    const size = b.size ? ` · ${ABSH.formatBytes(b.size)}` : "";
    const tracks = b.numTracks > 1 ? ` · ${b.numTracks} files` : "";
    meta.append(t, el("span", "a", bits + size + tracks));

    if (PROGRESS && PROGRESS.id === b.id && PROGRESS.total) {
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
  all.checked = shown.length > 0 && shown.every(b => SEL.has(b.id));
  all.disabled = BUSY || !shown.length;
  all.onchange = () => {
    shown.forEach(b => all.checked ? SEL.add(b.id) : SEL.delete(b.id));
    renderLibrary();
  };
  updateCount();
}

function updateCount() {
  $("#count").textContent = SEL.size ? `${SEL.size} selected` : "";
  $("#sync").disabled = BUSY || SEL.size === 0;
}

/* ----------------------------------------------------------- device pane */
function deviceRow(entry, label) {
  const li = el("li");
  const meta = el("div", "meta");
  meta.append(el("span", "t", label));
  const bits = [ABSH.formatBytes(entry.bytes)];
  if (entry.files > 1) bits.push(`${entry.files} files`);
  meta.append(el("span", "a", bits.join(" · ")));

  const del = el("button", "danger", "Remove");
  // Two-step instead of confirm(): dialogs in an extension popup are
  // unreliable, and deleting from a device should never be one stray click.
  let armed = false;
  del.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      del.textContent = "Confirm?";
      del.classList.add("confirm");
      setTimeout(() => {
        if (!armed) return;
        armed = false; del.textContent = "Remove"; del.classList.remove("confirm");
      }, 4000);
      return;
    }
    del.disabled = true;
    try {
      const r = await send({ type: "remove", names: [entry.name] });
      if (r.errors && r.errors.length) status(r.errors.join("\n"), "err");
      else status(`removed ${label} · freed ${r.freed}`, "ok");
      await refreshDevice();
    } catch (e) {
      status(String(e.message || e), "err");
      del.disabled = false;
    }
  });

  li.append(meta, del);
  return li;
}

function renderDevice() {
  const ul = $("#device-list");
  ul.innerHTML = "";
  const items = DEVICE.onDevice || [];
  if (!items.length) {
    ul.appendChild(el("li", "empty", "No books from this library are on the device yet."));
  }
  for (const e of items) ul.appendChild(deviceRow(e, e.title || e.name));

  const orphans = DEVICE.orphans || [];
  $("#orphans-wrap").classList.toggle("hidden", !orphans.length);
  const ol = $("#orphan-list");
  ol.innerHTML = "";
  for (const e of orphans) ol.appendChild(deviceRow(e, e.name));

  const badge = $("#device-count");
  badge.textContent = items.length || "";
  badge.classList.toggle("show", items.length > 0);

  const total = items.reduce((n, e) => n + (e.bytes || 0), 0);
  $("#device-summary").textContent = items.length
    ? `${items.length} book(s) · ${ABSH.formatBytes(total)}`
    : "";
  $("#free").textContent = DEVICE.free ? `${DEVICE.free} free` : "";
}

async function refreshDevice() {
  try {
    DEVICE = await send({ type: "listDevice", items: BOOKS });
    BOOKS = ABSH.annotateOnDevice(BOOKS, DEVICE);
    renderDevice();
    renderLibrary();
  } catch (e) {
    // A missing device is normal (nothing plugged in), not an error worth
    // shouting about - but the shelf should say why it is empty.
    DEVICE = { onDevice: [], orphans: [], free: "" };
    renderDevice();
    $("#device-summary").textContent = String(e.message || e);
  }
}

/* ------------------------------------------------------------- lifecycle */
async function loadBooks(libraryId) {
  BOOKS = await send({ type: "books", libraryId });
  SEL.clear();
  await refreshDevice();
}

async function load() {
  try {
    status("checking helper…");
    const p = await send({ type: "ping" });
    if (!p.ok) {
      status("Native helper not reachable.\nRun native/install.py, then restart the browser.\n" +
             (p.error || ""), "err");
    } else {
      status(`helper ok (${p.version || "?"})`, "ok");
    }

    const libs = await send({ type: "libraries" });
    const sel = $("#library");
    sel.innerHTML = "";
    for (const l of libs) {
      const o = document.createElement("option");
      o.value = l.id; o.textContent = l.name;
      sel.appendChild(o);
    }
    const stored = await browser.storage.local.get({ libraryId: "" });
    if (stored.libraryId && libs.some(l => l.id === stored.libraryId)) sel.value = stored.libraryId;
    sel.onchange = async () => {
      await browser.storage.local.set({ libraryId: sel.value });
      await loadBooks(sel.value);
    };
    await loadBooks(sel.value);
  } catch (e) {
    status(String(e.message || e), "err");
  }
}

/* ---------------------------------------------------------------- wiring */
$("#filter").addEventListener("input", renderLibrary);
$("#opts").addEventListener("click", (e) => {
  e.preventDefault();
  browser.runtime.openOptionsPage();
});
for (const b of document.querySelectorAll(".tab")) {
  b.addEventListener("click", () => setTab(b.dataset.tab));
}
$("#refresh").addEventListener("click", refreshDevice);

$("#sync").addEventListener("click", async () => {
  const items = BOOKS.filter(b => SEL.has(b.id));
  BUSY = true;
  updateCount();
  status(`syncing ${items.length} book(s)…`);
  try {
    const r = await send({ type: "sync", items }, (ev) => {
      if (ev.event === "item") {
        status(`syncing ${ev.index}/${ev.count} — ${ev.title}`);
      }
      PROGRESS = { id: ev.id, done: ev.done || 0, total: ev.total || 0 };
      renderLibrary();
    });
    PROGRESS = null;
    const lines = [];
    if (r.copied != null) lines.push(`copied ${r.copied} file(s)`);
    if (r.skipped) lines.push(`skipped ${r.skipped} already present`);
    if (r.freeAfter) lines.push(`free: ${r.freeAfter}`);
    status(lines.join(" · ") || "done", r.errors && r.errors.length ? "err" : "ok");
    if (r.errors && r.errors.length) {
      status(lines.join(" · ") + "\n" + r.errors.join("\n"), "err");
    }
    SEL.clear();
    await refreshDevice();
  } catch (e) {
    PROGRESS = null;
    status(String(e.message || e), "err");
  } finally {
    BUSY = false;
    updateCount();
    renderLibrary();
  }
});

load();
