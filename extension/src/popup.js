const $ = (s) => document.querySelector(s);
let BOOKS = [], SEL = new Set();

function status(msg, cls = "") {
  const el = $("#status");
  el.textContent = msg || "";
  el.className = "status " + cls;
}

async function send(m) {
  const r = await browser.runtime.sendMessage(m);
  if (!r || !r.ok) throw new Error((r && r.error) || "no response");
  return r.data;
}

function render() {
  const q = $("#filter").value.trim().toLowerCase();
  const shown = BOOKS.filter(b =>
    !q || (b.title + " " + b.author + " " + b.series).toLowerCase().includes(q));
  const ul = $("#list");
  ul.innerHTML = "";
  for (const b of shown) {
    const li = document.createElement("li");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = SEL.has(b.id);
    cb.addEventListener("change", () => {
      cb.checked ? SEL.add(b.id) : SEL.delete(b.id);
      updateCount();
    });
    const meta = document.createElement("div");
    meta.className = "meta";
    const mb = b.size ? ` · ${(b.size / 1048576).toFixed(0)} MB` : "";
    const tr = b.numTracks > 1 ? ` · ${b.numTracks} files` : "";
    meta.innerHTML = `<span class="t"></span><span class="a"></span>`;
    meta.querySelector(".t").textContent = b.title;
    meta.querySelector(".a").textContent = [b.author, b.series].filter(Boolean).join(" · ") + mb + tr;
    li.append(cb, meta);
    ul.appendChild(li);
  }
  $("#all").checked = shown.length > 0 && shown.every(b => SEL.has(b.id));
  $("#all").onchange = () => {
    shown.forEach(b => $("#all").checked ? SEL.add(b.id) : SEL.delete(b.id));
    render();
  };
  updateCount();
}

function updateCount() {
  $("#count").textContent = SEL.size ? `${SEL.size} selected` : "";
  $("#sync").disabled = SEL.size === 0;
}

async function load() {
  try {
    status("checking helper…");
    const p = await send({ type: "ping" });
    if (!p.ok) {
      status("Native helper not reachable.\nRun install.sh, then reopen Firefox.\n" + (p.error || ""), "err");
    } else {
      status(`helper ok (${p.version || "?"})`, "ok");
    }
    const libs = await send({ type: "libraries" });
    const sel = $("#library");
    sel.innerHTML = "";
    libs.forEach(l => {
      const o = document.createElement("option");
      o.value = l.id; o.textContent = l.name; sel.appendChild(o);
    });
    const stored = await browser.storage.local.get({ libraryId: "" });
    if (stored.libraryId && libs.some(l => l.id === stored.libraryId)) sel.value = stored.libraryId;
    sel.onchange = async () => {
      await browser.storage.local.set({ libraryId: sel.value });
      BOOKS = await send({ type: "books", libraryId: sel.value });
      SEL.clear(); render();
    };
    BOOKS = await send({ type: "books", libraryId: sel.value });
    render();
  } catch (e) {
    status(String(e.message || e), "err");
  }
}

$("#filter").addEventListener("input", render);
$("#opts").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });
$("#sync").addEventListener("click", async () => {
  const items = BOOKS.filter(b => SEL.has(b.id));
  $("#sync").disabled = true;
  status(`syncing ${items.length} book(s)…`);
  try {
    const r = await send({ type: "sync", items });
    const lines = [];
    if (r.copied != null) lines.push(`copied ${r.copied} file(s)`);
    if (r.skipped) lines.push(`skipped ${r.skipped} already present`);
    if (r.freeAfter) lines.push(`free: ${r.freeAfter}`);
    status(lines.join(" · ") || "done", "ok");
    if (r.errors && r.errors.length) status(lines.join(" · ") + "\n" + r.errors.join("\n"), "err");
  } catch (e) {
    status(String(e.message || e), "err");
  } finally {
    $("#sync").disabled = SEL.size === 0;
  }
});

load();
