/* Annotates the Audiobookshelf library with what is on your device.
 *
 * Each book card gets a badge saying whether it is on the player, and buttons
 * to put it there or take it off. Books that are on the device but NOT in the
 * library appear in a panel of their own, with an upload button.
 *
 * Card -> item id mapping comes from page-hook.js, which watches the API
 * responses Audiobookshelf itself fetched. The DOM never carries the id: cards
 * are #book-card-{index} and the id lives in Vue state, so scraping it would
 * break on any upstream release.
 *
 * Everything here is additive. If the mapping is unavailable the page is left
 * exactly as it was, rather than showing badges that might be wrong.
 */
(function () {
  "use strict";

  const BTN_ID = "absh-sync-btn";
  const PANEL_ID = "absh-panel";
  const BADGE = "absh-badge";
  const TAG = "ABSH_ITEMS";

  /* id -> {title, author}, learned from the page's own API traffic. */
  const KNOWN = new Map();
  /* index on screen -> item id, for the current library listing. */
  let ORDER = [];
  let STATUS = null;          // last known device status
  let refreshTimer = null;
  let busy = false;

  /* ------------------------------------------------------------ plumbing */
  function send(msg) {
    return browser.runtime.sendMessage(msg).then((r) => {
      if (!r || !r.ok) throw new Error((r && r.error) || "no response");
      return r.data;
    });
  }

  window.addEventListener("message", (ev) => {
    if (ev.source !== window || ev.origin !== window.location.origin) return;
    const d = ev.data;
    if (!d || d.source !== TAG || !Array.isArray(d.items)) return;
    for (const it of d.items) KNOWN.set(it.id, it);
    // A listing response defines the order the cards are rendered in.
    if (/\/items(\?|$)/.test(d.url) && d.items.length > 1) {
      ORDER = d.items.map((i) => i.id);
    }
    scheduleRender();
  });

  function scheduleRender() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(render, 120);
  }

  /* ----------------------------------------------------------- device state */
  async function loadStatus() {
    try {
      STATUS = await send({ type: "status", readTags: true });
    } catch (e) {
      STATUS = { error: String(e.message || e), both: [], serverOnly: [], deviceOnly: [] };
    }
    render();
  }

  function onDevice(id) {
    if (!STATUS || !Array.isArray(STATUS.both)) return null;
    return STATUS.both.find((b) => b.itemId === id) || null;
  }

  /* --------------------------------------------------------------- cards */
  function cardIndex(el) {
    const m = /(?:book|item)-card-(\d+)/.exec(el.id || "");
    return m ? parseInt(m[1], 10) : null;
  }

  function idForCard(el) {
    // Prefer an id the page put in the DOM itself, if a future version does.
    const explicit = el.getAttribute("data-libraryitemid") || el.getAttribute("data-id");
    if (explicit && KNOWN.has(explicit)) return explicit;
    const i = cardIndex(el);
    return i != null && i < ORDER.length ? ORDER[i] : null;
  }

  function button(label, title, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "absh-mini " + (cls || "");
    b.textContent = label;
    b.title = title;
    b.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();   // do not open the book
      onClick(b);
    });
    return b;
  }

  function decorate(card) {
    const id = idForCard(card);
    if (!id) return;
    const known = KNOWN.get(id);
    const here = onDevice(id);

    let badge = card.querySelector("." + BADGE);
    if (!badge) {
      badge = document.createElement("div");
      badge.className = BADGE;
      // The card is positioned; an absolute badge rides along with it.
      if (getComputedStyle(card).position === "static") card.style.position = "relative";
      card.appendChild(badge);
    }
    badge.innerHTML = "";
    badge.dataset.abshId = id;

    if (!STATUS || STATUS.error) {
      badge.classList.add("absh-quiet");
      badge.textContent = STATUS && STATUS.error ? "?" : "";
      badge.title = (STATUS && STATUS.error) || "";
      return;
    }
    badge.classList.remove("absh-quiet");

    if (here) {
      badge.classList.add("absh-on");
      const dot = document.createElement("span");
      dot.className = "absh-dot";
      dot.textContent = "on device";
      badge.append(dot, button("✕", "Remove from the device", "absh-danger",
        (b) => act(b, { type: "remove", names: [here.name] }, "removing")));
    } else {
      badge.classList.remove("absh-on");
      badge.append(button("⤓", "Copy to the device", "",
        (b) => act(b, { type: "pull", ids: [id] }, "copying")));
    }
  }

  async function act(btn, msg, verb) {
    if (busy) return;
    busy = true;
    const was = btn.textContent;
    btn.textContent = "···";
    btn.disabled = true;
    btn.title = verb + "…";
    try {
      const r = await send(msg);
      if (r.errors && r.errors.length) note(r.errors[0], true);
      await loadStatus();
    } catch (e) {
      note(String(e.message || e), true);
      btn.textContent = was;
      btn.disabled = false;
    } finally {
      busy = false;
    }
  }

  /* ---------------------------------------------------- device-only panel */
  function renderPanel() {
    const only = (STATUS && STATUS.deviceOnly) || [];
    let panel = document.getElementById(PANEL_ID);
    if (!only.length) {
      if (panel) panel.remove();
      return;
    }
    if (!panel) {
      panel = document.createElement("div");
      panel.id = PANEL_ID;
      document.body.appendChild(panel);
    }
    panel.innerHTML = "";

    const head = document.createElement("div");
    head.className = "absh-panel-head";
    head.textContent = `${only.length} on your device, not in this library`;
    const close = button("✕", "Hide", "absh-quiet-btn", () => panel.remove());
    head.appendChild(close);
    panel.appendChild(head);

    for (const e of only) {
      const row = document.createElement("div");
      row.className = "absh-panel-row";
      const meta = document.createElement("div");
      meta.className = "absh-panel-meta";
      const t = document.createElement("span");
      t.className = "absh-panel-title";
      t.textContent = e.title || e.name;
      const a = document.createElement("span");
      a.className = "absh-panel-sub";
      a.textContent = [e.author, sizeOf(e.bytes)].filter(Boolean).join(" · ");
      meta.append(t, a);
      row.append(meta,
        button("Upload", "Add this book to Audiobookshelf", "absh-primary",
          (b) => act(b, { type: "push", names: [e.name] }, "uploading")),
        button("✕", "Delete from the device", "absh-danger",
          (b) => act(b, { type: "remove", names: [e.name] }, "removing")));
      panel.appendChild(row);
    }
  }

  function sizeOf(n) {
    n = Number(n) || 0;
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)}${u[i]}`;
  }

  function note(text, bad) {
    let el = document.getElementById("absh-note");
    if (!el) {
      el = document.createElement("div");
      el.id = "absh-note";
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.className = bad ? "absh-bad" : "";
    clearTimeout(el.__t);
    el.__t = setTimeout(() => el.remove(), 6000);
  }

  /* -------------------------------------------------------------- toolbar */
  function toolbarButton() {
    if (document.getElementById(BTN_ID)) return;
    const bar = document.getElementById("toolbar");
    if (!bar) return;
    const b = document.createElement("button");
    b.id = BTN_ID;
    b.type = "button";
    b.className = "absh-btn";
    b.title = "Audiobookshelf Helper - pick books and sync to your player";
    b.innerHTML = '<span class="absh-ico">⤓</span><span>Sync to device</span>';
    b.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      browser.runtime.sendMessage({ type: "openPicker" }).catch(() => {});
    });
    bar.appendChild(b);
  }

  function render() {
    toolbarButton();
    for (const card of document.querySelectorAll('[id^="book-card-"], [id^="item-card-"]')) {
      try {
        decorate(card);
      } catch { /* one bad card must not stop the rest */ }
    }
    renderPanel();
  }

  /* Vue re-renders the shelf constantly; watch rather than run once. */
  new MutationObserver(scheduleRender)
    .observe(document.documentElement, { childList: true, subtree: true });

  render();
  loadStatus();
  // The device can be unplugged while the page is open.
  setInterval(loadStatus, 60000);
})();
