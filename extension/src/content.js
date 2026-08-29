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
  /* Normalised title -> item id. See idForCard for why not by index. */
  let TITLES = null;
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
    TITLES = null;                       // rebuilt lazily on the next render
    scheduleRender();
  });

  function scheduleRender() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(render, 120);
  }

  /* ----------------------------------------------------------- device state */
  /* The status is where the card-to-book mapping comes from, so a failed call
     is not a cosmetic problem: it means no badges at all. Retry soon rather
     than waiting out the whole refresh interval - a transient failure used to
     leave the page bare for a full minute. */
  let statusRetry = 0;
  async function loadStatus() {
    try {
      STATUS = await send({ type: "status", readTags: true });
      statusRetry = 0;
    } catch (e) {
      STATUS = { error: String(e.message || e), both: [], serverOnly: [], deviceOnly: [] };
      // Keep trying rather than giving up after a few goes. The status is the
      // card-to-book mapping, so while it is failing the page has no badges at
      // all; stopping meant one hiccup left it dead until the next minute
      // tick. Back off, then settle at every 30s for as long as it is broken.
      const wait = [2000, 5000, 15000][statusRetry] || 30000;
      statusRetry += 1;
      clearTimeout(loadStatus.__t);
      loadStatus.__t = setTimeout(loadStatus, wait);
    }
    TITLES = null;                       // status feeds the mapping as well
    render();
  }

  function onDevice(id) {
    if (!STATUS || !Array.isArray(STATUS.both)) return null;
    return STATUS.both.find((b) => b.itemId === id) || null;
  }

  /* --------------------------------------------------------------- cards */
  function normTitle(s) {
    return String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
  }

  /* Ambiguous titles map to null rather than to one of the candidates: a badge
     on the wrong book is worse than no badge, and this is the same rule the
     rest of the file follows when the mapping is unavailable.
   *
   * Built from the helper's status as well as the page hook. The hook only
   * sees /personalized if it is injected before the app requests it, and it
   * loses that race often enough to matter - the badges then never appear
   * until a reload, which is what the real-server suite kept catching. The
   * helper already fetched the same library through the API, so the mapping
   * does not need to depend on winning a race against the page. */
  function titleIndex() {
    const byTitle = new Map();
    const add = (id, title) => {
      const k = normTitle(title);
      if (!k || !id) return;
      if (byTitle.has(k) && byTitle.get(k) !== id) byTitle.set(k, null);
      else if (!byTitle.has(k)) byTitle.set(k, id);
    };
    for (const [id, it] of KNOWN) add(id, it.title);
    if (STATUS && !STATUS.error) {
      for (const i of STATUS.serverOnly || []) add(i.id, i.title);
      for (const b of STATUS.both || []) add(b.item && b.item.id, b.item && b.item.title);
    }
    return byTitle;
  }

  /* Audiobookshelf renders the title as alt="<title>, Cover" on the cover
     image, and again in the placeholder shown before the cover loads. */
  function cardTitle(el) {
    const img = el.querySelector("img[alt]");
    if (img) return (img.getAttribute("alt") || "").replace(/,\s*cover\s*$/i, "");
    const p = el.querySelector('[cy-id="placeholderTitleText"]');
    return p ? p.textContent : "";
  }

  function idForCard(el) {
    // Prefer an id the page put in the DOM itself, if a future version does.
    const explicit = el.getAttribute("data-libraryitemid") || el.getAttribute("data-id");
    if (explicit && KNOWN.has(explicit)) return explicit;

    // Not by card index. Card ids are NOT unique - every shelf numbers its own
    // cards from zero, so a real library page carries several #book-card-0 -
    // and the index is per shelf, not into any one listing response. Indexing
    // a flat list was wrong on every page with more than one shelf, which is
    // the default page.
    if (!TITLES) TITLES = titleIndex();
    return TITLES.get(normTitle(cardTitle(el))) || null;
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
    const known = id ? KNOWN.get(id) : null;
    const here = id ? onDevice(id) : null;

    let badge = card.querySelector("." + BADGE);
    if (!badge) {
      badge = document.createElement("div");
      badge.className = BADGE;
      // The card is positioned; an absolute badge rides along with it.
      if (getComputedStyle(card).position === "static") card.style.position = "relative";
      card.appendChild(badge);
    }
    badge.innerHTML = "";
    if (id) badge.dataset.abshId = id;
    else delete badge.dataset.abshId;

    // No id means the library has not been read yet, or this card's title
    // matches two books and guessing would badge the wrong one. Say so quietly
    // instead of leaving the card bare - an absent badge is indistinguishable
    // from the extension not being installed, which cost a lot of time.
    if (!id) {
      badge.classList.add("absh-quiet");
      badge.classList.remove("absh-on");
      badge.textContent = "?";
      badge.title = (STATUS && STATUS.error)
        ? `Audiobookshelf Helper: ${STATUS.error}`
        : "Audiobookshelf Helper: still identifying this book";
      return;
    }

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

  let renderObserver = null;

  function render() {
    toolbarButton();
    for (const card of document.querySelectorAll('[id^="book-card-"], [id^="item-card-"]')) {
      try {
        decorate(card);
      } catch { /* one bad card must not stop the rest */ }
    }
    renderPanel();
    // Discard the mutations we just made, so they cannot schedule another
    // render. Without this the observer feeds itself forever.
    if (renderObserver) renderObserver.takeRecords();
  }

  /* Vue re-renders the shelf constantly; watch rather than run once.
   *
   * render() mutates the DOM, so without draining the queue afterwards the
   * observer sees its own work and schedules another render - a loop that ran
   * every 120ms for as long as the page was open. It burned CPU and left the
   * badges permanently "not stable", which is how a click could never land. */
  const observer = new MutationObserver(scheduleRender);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  renderObserver = observer;

  render();
  loadStatus();
  // The device can be unplugged while the page is open.
  setInterval(loadStatus, 60000);
})();
