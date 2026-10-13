/* Puts the player on Audiobookshelf's own pages.
 *
 * Each cover gets a strip saying whether that book is on the player, being
 * copied, or not there yet - and then the strip is the button that copies it.
 * A book's own page gets "Copy to <player>" beside Play, with the player's
 * state under it. Books on the player that the library does not have appear
 * in a panel of their own, with an upload button.
 *
 * Registered for the whole server (see background.js syncContentScript):
 * Audiobookshelf changes pages without loading them, so this decides what is
 * on the page every time the page changes, and does nothing where there is
 * nothing for it.
 *
 * Which book a cover is comes from page-hook.js, which reads it from
 * Audiobookshelf's own component and writes it on the cover as data-absh-id.
 * Titles are the fallback, and cannot tell two versions of a book apart.
 *
 * Everything here is additive. If the mapping is unavailable the page is left
 * exactly as it was, rather than showing badges that might be wrong.
 */
(function () {
  // The background also injects this as a fallback (see syncContentScript):
  // a registered content script does not always run, so a second copy may
  // arrive. Running twice would double the observers and the badges.
  if (window.__abshLoaded) return;
  window.__abshLoaded = true;

  "use strict";

  const BTN_ID = "absh-sync-btn";
  const PANEL_ID = "absh-panel";
  const BADGE = "absh-badge";
  const RESTORE_ID = "absh-folder-notice";
  const TAG = "ABSH_ITEMS";

  /* id -> {title, author}, learned from the page's own API traffic. */
  const KNOWN = new Map();
  /* The copy queue, as the background last said. */
  let JOB = { current: null, queue: [], last: null };

  /* The background tells this tab when the copy queue moves. A tab it does
     not find, though, waits forever - Firefox's own test run did, with the
     book on the player and the cover still saying "Copying". So while a
     copy is running the tab asks as well, and reads the player again when
     it ends. */
  const running = (j) => !!(j && (j.current || (j.queue && j.queue.length)));
  let asking = null;
  function setJob(job) {
    const was = running(JOB);
    JOB = job;
    scheduleRender();
    if (was && !running(job)) loadStatus();
    if (running(job) && !asking) {
      asking = setInterval(() => {
        send({ type: "jobState" }).then((j) => { if (j) setJob(j); }).catch(() => {});
      }, 1500);
    } else if (!running(job) && asking) {
      clearInterval(asking);
      asking = null;
    }
  }
  let PLAYER = "the player";
  const ITEM_ID = "absh-item";
  /* Normalised title -> item id. See idForCard for why not by index. */
  let TITLES = null;
  let STATUS = null;          // last known device status
  let refreshTimer = null;
  let busy = false;

  /* ------------------------------------------------------------ plumbing */
  function send(msg) {
    return browser.runtime.sendMessage(msg).then((r) => {
      if (!r || !r.ok) {
        const e = new Error((r && r.error) || "no response");
        // Set only by Chrome's folder backend; says what would fix it.
        e.code = r && r.code;
        throw e;
      }
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
  /* Chrome, writing to a chosen folder instead of through the helper. No
   * event says a player came or went, so the page looks again whenever the
   * user returns to it. Never true on Firefox. */
  let folderMode = false;
  async function loadStatus() {
    try {
      STATUS = await send({ type: "status", readTags: true });
      statusRetry = 0;
      folderMode = STATUS.backend === "folder";
      folderNotice(null);
    } catch (e) {
      STATUS = { error: String(e.message || e), both: [], serverOnly: [], deviceOnly: [] };
      if (e.code && String(e.code).startsWith("folder-")) {
        folderMode = true;
        folderNotice(e);
      }
      // Keep trying rather than giving up after a few goes: the status is the
      // card-to-book mapping, so while it is failing the page has no badges at
      // all, and stopping left it dead. The long tail is only a backstop now -
      // the common cause of a failure here is an unplugged player, and that
      // comes back as an event the moment it is plugged in.
      const wait = [2000, 5000, 15000][statusRetry] || 120000;
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
    // What page-hook.js read from the cover's own component: exact, even for
    // two versions of one book.
    const stamped = el.getAttribute("data-absh-id");
    if (stamped) return stamped;
    // An id the page put in the DOM itself, if a future version does.
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

  /* Where a book stands: on the player, in the copy queue, or neither. */
  function standing(id) {
    for (const j of [JOB.current, ...(JOB.queue || [])]) {
      const it = j && j.op === "pull" && j.items.find((i) => i.ref === id);
      if (it && (it.state === "waiting" || it.state === "copying")) return { it, job: j };
    }
    const last = JOB.last && JOB.last.op === "pull" && JOB.last.items.find((i) => i.ref === id);
    if (last && last.state === "failed") return { it: last, job: JOB.last };
    return null;
  }

  function serverItem(id) {
    const i = STATUS && (STATUS.serverOnly || []).find((x) => x.id === id);
    return i || (KNOWN.get(id) ? { id, ...KNOWN.get(id) } : { id });
  }

  function copy(id) {
    const i = serverItem(id);
    return send({ type: "jobAdd", op: "pull", player: PLAYER,
                  items: [{ key: id, ref: id, title: i.title || "", bytes: i.size || 0 }] })
      .then((job) => setJob(job))
      .catch((e) => note(String(e.message || e), true));
  }

  /* The strip along the bottom of a cover. A state when there is nothing to
     do; the button itself when there is. */
  function decorate(card) {
    const id = idForCard(card);
    let strip = card.querySelector("." + BADGE);
    // No book, or no word from the player: nothing true to say, so say nothing
    // on the cover. The toolbar button carries why.
    if (!id || !STATUS || STATUS.error) {
      if (strip) strip.remove();
      return;
    }
    const here = onDevice(id);
    const st = standing(id);
    const kind = here ? "on" : st && st.it.state === "copying" ? "run"
      : st && st.it.state === "waiting" ? "wait" : st ? "failed" : "copy";
    if (strip && strip.dataset.abshId === id && strip.dataset.kind === kind && kind !== "run") return;

    // On the cover itself, not the card: the card also holds the title and
    // author under the cover, and a strip at its foot sat on top of them.
    const cover = card.querySelector('[id^="cover-area-"]') || card;
    if (getComputedStyle(cover).position === "static") cover.style.position = "relative";
    const make = kind === "copy" || kind === "failed" ? "button" : "div";
    if (!strip || strip.tagName.toLowerCase() !== make || strip.parentElement !== cover) {
      if (strip) strip.remove();
      strip = document.createElement(make);
      cover.appendChild(strip);
    }
    // Clear of the cover's own ⋮ menu and listening bar in its bottom corner,
    // which are sized in the cover's em and so grow with the shelf's zoom.
    if (cover !== card) {
      const em = parseFloat(getComputedStyle(cover).fontSize) || 16;
      strip.style.bottom = `${Math.round(2.1 * em)}px`;
    }
    strip.className = `${BADGE} absh-${kind}`;
    strip.dataset.abshId = id;
    strip.dataset.kind = kind;
    strip.textContent = "";
    strip.onclick = null;
    if (kind === "on") {
      strip.textContent = `✓ On ${PLAYER}`;
      strip.title = `On ${PLAYER}`;
    } else if (kind === "run") {
      const t = document.createElement("span");
      t.textContent = "Copying…";
      const bar = document.createElement("span");
      const files = st.it.total > 1;
      bar.className = "absh-bar" + (files ? "" : " absh-indeterminate");
      const i = document.createElement("i");
      if (files) i.style.width = `${Math.round(100 * st.it.done / st.it.total)}%`;
      bar.appendChild(i);
      strip.append(t, bar);
      strip.title = `Copying to ${PLAYER}`;
    } else if (kind === "wait") {
      strip.textContent = "Waiting to copy";
      strip.title = `Queued to copy to ${PLAYER}`;
    } else {
      strip.type = "button";
      strip.textContent = kind === "failed" ? "Copy failed · Try again" : "Copy to player";
      strip.title = kind === "failed" && st.it.error ? st.it.error : `Copy to ${PLAYER}`;
      strip.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();          // the cover would open the book
        copy(id);
      });
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

  /* ------------------------------------------------------- a book's page */
  /* "Copy to <player>" beside Play, and what the player has under it. Found
     by its shape, not its label: Audiobookshelf is translated, and "Play"
     is not always "Play". */
  function itemPage() {
    const m = /\/item\/([^/?#]+)/.exec(location.pathname);
    const box = document.getElementById(ITEM_ID);
    if (!m) {
      if (box) box.remove();
      return;
    }
    const id = decodeURIComponent(m[1]);
    // The app keeps a second, hidden Play in its select-many bar; the one
    // that matters is on the page and drawn.
    const play = [...document.querySelectorAll("button.abs-btn.bg-success")]
      .find((b) => b.getClientRects().length && !b.closest(".modal, .h-16"));
    const row = play && play.parentElement;
    if (!row) return;                       // not rendered yet; the next change brings it
    let el = box;
    if (!el || el.dataset.abshId !== id || el.previousElementSibling !== row) {
      if (el) el.remove();
      el = document.createElement("div");
      el.id = ITEM_ID;
      el.dataset.abshId = id;
      row.after(el);
    }
    el.textContent = "";
    if (!STATUS || STATUS.error) {
      el.append(line(STATUS && STATUS.error ? `Audiobookshelf Helper: ${STATUS.error}` : "", true));
      return;
    }
    const here = onDevice(id);
    const st = standing(id);
    const actions = document.createElement("div");
    actions.className = "absh-item-actions";
    if (here) {
      actions.append(bigButton(`Remove from ${PLAYER}`, "absh-ghost",
        (b) => act(b, { type: "remove", names: [here.name] }, "removing")));
      el.append(actions, line(`✓ On ${PLAYER}`));
    } else if (st && st.it.state !== "failed") {
      el.append(line(st.it.state === "copying" ? `Copying to ${PLAYER}…` : `Waiting to copy to ${PLAYER}`));
    } else {
      actions.append(bigButton(`Copy to ${PLAYER}`, "", () => copy(id)));
      const free = STATUS.free && STATUS.free.free;
      el.append(actions, line(st ? `Copy failed: ${st.it.error || "try again"}`
        : `Not on ${PLAYER} yet${free ? ` · ${sizeOf(free)} free` : ""}`, !!st));
    }
  }

  function line(text, bad) {
    const d = document.createElement("div");
    d.className = "absh-item-line" + (bad ? " absh-bad-line" : "");
    d.textContent = text;
    return d;
  }

  function bigButton(label, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "absh-btn absh-big " + cls;
    b.textContent = label;
    b.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      onClick(b);
    });
    return b;
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

  /* Why the folder cannot be used, and the way to fix it, kept on screen
   * until it is fixed - a passing note would be gone before anyone acted on
   * it. Restoring access takes a click on one of the extension's own pages,
   * so the button opens Options rather than trying from here. */
  function folderNotice(err) {
    let el = document.getElementById(RESTORE_ID);
    if (!err) {
      if (el) el.remove();
      return;
    }
    if (!el) {
      el = document.createElement("div");
      el.id = RESTORE_ID;
      document.body.appendChild(el);
    }
    el.innerHTML = "";
    const text = document.createElement("span");
    text.textContent = `Audiobookshelf Helper: ${err.message}`;
    el.appendChild(text);
    if (err.code !== "folder-missing") {
      el.appendChild(button("Open Settings", "Open the extension's settings", "absh-primary", () => {
        browser.runtime.sendMessage({ type: "openFolderOptions" }).catch(() => {});
      }));
    }
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
  /* The button's words follow the player: with none answering, the covers say
     nothing, so this is where the reason is. */
  function toolbarState(b) {
    const down = STATUS && STATUS.error;
    const text = down ? "Player not connected" : "Sync to device";
    const label = b.querySelector(".absh-label");
    if (label && label.textContent !== text) label.textContent = text;
    b.title = down ? `Audiobookshelf Helper: ${STATUS.error}`
      : "Audiobookshelf Helper - pick books and sync to your player";
    b.classList.toggle("absh-down", !!down);
  }

  function toolbarButton() {
    const have = document.getElementById(BTN_ID);
    if (have) { toolbarState(have); return; }
    const bar = document.getElementById("toolbar");
    if (!bar) return;
    const b = document.createElement("button");
    b.id = BTN_ID;
    b.type = "button";
    b.className = "absh-btn";
    b.title = "Audiobookshelf Helper - pick books and sync to your player";
    b.innerHTML = '<span class="absh-ico">⤓</span><span class="absh-label">Sync to device</span>';
    b.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      browser.runtime.sendMessage({ type: "openPicker" }).catch(() => {});
    });
    bar.appendChild(b);
    toolbarState(b);
  }

  function render() {
    toolbarButton();
    itemPage();
    for (const card of document.querySelectorAll('[id^="book-card-"], [id^="item-card-"]')) {
      try {
        decorate(card);
      } catch { /* one bad card must not stop the rest */ }
    }
    renderPanel();
  }

  /* Did this mutation come from us? Draining the queue after a render was the
     obvious way to stop the observer feeding itself, and it was wrong: it also
     threw away Audiobookshelf's own mutations that landed mid-render, so about
     half of all page loads never learned the cards had appeared and sat with
     no badges at all. Judge each record instead. */
  function ownMutation(rec) {
    const own = `.${BADGE}, #${PANEL_ID}, #${BTN_ID}, #absh-note, #${RESTORE_ID}, #${ITEM_ID}`;
    const el = rec.target && rec.target.nodeType === 1
      ? rec.target : rec.target && rec.target.parentElement;
    if (el && el.closest && el.closest(own)) return true;
    const touched = [...rec.addedNodes, ...rec.removedNodes];
    return touched.length > 0 && touched.every((n) =>
      n.nodeType === 1 && n.matches && n.matches(own));
  }

  /* Vue re-renders the shelf constantly; watch rather than run once.
   *
   * render() mutates the DOM, so without draining the queue afterwards the
   * observer sees its own work and schedules another render - a loop that ran
   * every 120ms for as long as the page was open. It burned CPU and left the
   * badges permanently "not stable", which is how a click could never land. */
  const observer = new MutationObserver((records) => {
    if (records.some((r) => !ownMutation(r))) scheduleRender();
  });
  // data-absh-id too: page-hook.js writes it just after a cover appears.
  observer.observe(document.documentElement,
    { childList: true, subtree: true, attributes: true, attributeFilter: ["data-absh-id"] });

  // The helper says when a volume comes or goes, so plugging the player in
  // updates the page at once instead of on the next tick. Guarded because
  // losing the whole in-page UI to a failed listener registration would be the
  // same silent nothing-on-the-page this project has already chased twice; the
  // timer below still covers it.
  try {
    browser.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === "host-event" && msg.event === "devices-changed") {
        loadStatus();
      }
      // The copy queue moved on, or a copy finished and the player changed.
      if (msg && msg.type === "job" && msg.job) setJob(msg.job);
      if (msg && msg.type === "status" && msg.status) {
        STATUS = msg.status;
        TITLES = null;
        scheduleRender();
      }
    });
  } catch (e) { /* no live extension context; the backstop still runs */ }

  // The folder backend's stand-in for the helper's device events: coming back
  // to this tab - from plugging the player in, or from allowing access in
  // Options - is when something is likeliest to have changed.
  let lastLook = 0;
  const lookAgain = () => {
    if (!folderMode || document.visibilityState !== "visible") return;
    if (Date.now() - lastLook < 1000) return;      // focus and visibility arrive together
    lastLook = Date.now();
    loadStatus();
  };
  document.addEventListener("visibilitychange", lookAgain);
  window.addEventListener("focus", lookAgain);

  // What to call the player, and where the copy queue stands. Neither may
  // throw: anything that does here takes the whole page UI with it.
  try {
    browser.storage.local.get({ devicePath: "" }).then(({ devicePath }) => {
      const parts = String(devicePath || "").replace(/[\\/]+$/, "").split(/[\\/]/);
      if (parts[parts.length - 1]) PLAYER = parts[parts.length - 1];
      scheduleRender();
    }).catch(() => {});
  } catch { /* no storage here; "the player" it is */ }
  send({ type: "jobState" }).then((job) => { if (job) setJob(job); }).catch(() => {});

  render();
  loadStatus();
  // A backstop, not the mechanism. Mounting and unmounting arrive as events;
  // this is here for what they cannot cover - books added on the server, and a
  // helper that went away and came back while nothing was watching.
  setInterval(loadStatus, 300000);
})();
