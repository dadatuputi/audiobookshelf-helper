/* Options: using the player's folder without the helper. Chrome only.
 *
 * build.py adds this, and folder.js before it, to the Chrome bundle's options
 * page and to nothing in Firefox's, so the section below exists only where
 * showDirectoryPicker does.
 *
 * This page is where a folder is chosen and where access is restored, because
 * both need a click on a page of the extension's own: the service worker that
 * does the copying can use a granted folder but cannot show a picker or ask
 * for permission, and the toolbar popup cannot renew a grant. */
(function () {
  "use strict";

  const F = globalThis.ABSH_FOLDER;
  const $ = (id) => document.getElementById(id);
  let HANDLE = null;

  const h = (tag, attrs, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") n.className = v;
      else n.setAttribute(k, v);
    }
    for (const k of kids) n.append(k);
    return n;
  };

  /** The row's chevron, built rather than parsed. */
  function chevron() {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    for (const [k, v] of Object.entries({ class: "chev", width: 16, height: 16, viewBox: "0 0 24 24",
      fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round",
      "stroke-linejoin": "round" })) svg.setAttribute(k, v);
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", "m9 6 6 6-6 6");
    svg.append(path);
    return svg;
  }

  function build() {
    const head = h("button", { class: "sec-head", "aria-expanded": "false",
                               "aria-controls": "folderBox" },
      h("span", { class: "dot" }),
      h("span", { class: "sec-name", id: "folder" }, "Folder"),
      h("span", { class: "sec-sum", id: "sum-folder" }, "Chrome only"),
      chevron());
    const box = h("div", { class: "sec-body", id: "folderBox", hidden: "" },
      h("div", { class: "hint" },
        "Chrome can copy to your player itself. If you can't install the helper, or would " +
        "rather not, choose the player's top folder - the one your books folder is in - and " +
        "Chrome writes there directly. Whenever the helper is installed and answering, it " +
        "is used instead."),
      h("details", {},
        h("summary", { class: "hint" }, "What you give up without the helper"),
        h("ul", { class: "hint" },
          h("li", {}, "Finding the player: you choose its folder yourself, and again if it " +
                      "mounts somewhere else. The helper lists what is plugged in."),
          h("li", {}, "Plugging in and out: Chrome isn't told when the player comes or goes. " +
                      "Your library page checks again when you come back to it; the popup " +
                      "checks each time it opens."),
          h("li", {}, "Staying allowed: Chrome keeps the extension's access to the folder " +
                      "only while one of the extension's tabs is open - this page, for " +
                      "instance - and asks again after it restarts. If Chrome offers " +
                      "“Allow on every visit” when you allow access, choose it and " +
                      "access lasts."),
          h("li", {}, "Free space: Chrome doesn't say how much room is left on the player."),
          h("li", {}, "Some tags: books that are only on the player are recognised from MP3 " +
                      "and M4A/M4B tags, or by their names. The helper with the optional " +
                      "mutagen package also reads FLAC, Ogg and Opus tags."),
          h("li", {}, "The absh command line and full-screen picker, which come with the " +
                      "helper."))),
      h("div", { class: "actions" },
        h("button", { id: "folderPick", class: "primary" }, "Choose the player's folder…"),
        h("button", { id: "folderAllow", class: "primary hidden" }, "Allow access"),
        h("button", { id: "folderForget", class: "secondary hidden" }, "Forget this folder")),
      h("div", { id: "folderState", class: "note" }),
      h("div", { id: "folderServer", class: "note" }),
      h("div", { id: "folderUse", class: "hint" }),
      h("label", { class: "chk" },
        h("input", { type: "checkbox", id: "folderAlways" }),
        h("span", {}, "Use this folder even when the helper is installed")));
    const sec = h("section", { class: "sec", id: "sec-folder", "data-state": "off" }, head, box);
    const group = $("optional");
    group.append(sec);
    group.classList.remove("hidden");
  }

  /** The row's one line: whether the folder is in use, and why not. Without
   *  the helper this is the way to copy at all, so then it asks for you. */
  function row(st) {
    let state = "off";
    let text;
    if (st.state === "none") {
      text = HELPER_UP ? "Not used" : "Not chosen · the helper isn't answering";
      if (!HELPER_UP) state = "warn";
    } else if (st.state === "granted") {
      text = `“${st.name}” · ` + (USING ? "in use" : "not used");
      if (USING) state = "ok";
    } else {
      text = `“${st.name}” · ` + (st.state === "prompt" ? "access paused" : "not there");
      if (USING || !HELPER_UP) state = "err";
    }
    globalThis.ABSH_SETTINGS.setRow("folder", state, text, { counted: false });
  }
  let USING = false;                   // Chrome is copying to this folder
  let HELPER_UP = true;

  function say(id, text, cls) {
    $(id).textContent = text;
    $(id).className = "note " + (cls || "");
  }

  /** What is in the chosen folder, said the way the helper's Detect says it. */
  async function contents(handle) {
    const { subdir } = await browser.storage.local.get({ subdir: ABSH.DEFAULTS.subdir });
    const parts = F.safeSubdir(subdir);
    let d = handle;
    try {
      for (const p of parts) d = await d.getDirectoryHandle(p);
    } catch {
      const named = F.safeSubdir(handle.name).join("/") === parts.join("/");
      return named
        ? ` That looks like the books folder itself; choose the folder it is in, or ` +
          `books will go into ${handle.name}/${parts.join("/")}.`
        : ` There is no ${parts.join("/")} folder in it yet; one is made on the first copy.`;
    }
    let n = 0;
    for await (const k of d.keys()) if (!k.startsWith(".")) n++;
    return ` It has your ${parts.join("/")} folder (${n} item${n === 1 ? "" : "s"}).`;
  }

  async function refresh() {
    HANDLE = await F.loadHandle().catch(() => null);
    const st = await F.handleState(HANDLE);
    const allow = $("folderAllow");
    allow.classList.toggle("hidden", st.state !== "prompt");
    allow.textContent = `Allow access to “${st.name}”`;
    $("folderForget").classList.toggle("hidden", st.state === "none");
    $("folderPick").textContent = st.state === "none"
      ? "Choose the player's folder…" : "Choose a different folder…";

    switch (st.state) {
      case "none":
        say("folderState", "No folder chosen.", "");
        break;
      case "granted":
        say("folderState", `Using “${st.name}”.` + (await contents(HANDLE)), "ok");
        break;
      case "prompt":
        say("folderState", `Chrome has paused access to “${st.name}” - it does after ` +
            "a restart, and when the extension's last tab closes. Click Allow access to carry " +
            "on, and leave this tab open while you copy, unless Chrome offers “Allow on " +
            "every visit”.", "warn");
        break;
      default:
        say("folderState", `“${st.name}” isn't there right now. Plug the player in - ` +
            "this page checks again when you come back to it - or choose its folder again.",
            "err");
    }

    // Chrome makes the requests to the server itself here, which the helper
    // never needed the grant for.
    const { absUrl, folderMode } = await browser.storage.local.get({ absUrl: "", folderMode: "auto" });
    $("folderAlways").checked = folderMode === "always";
    let granted = true;
    try {
      granted = !absUrl || await browser.permissions.contains({ origins: [ABSH.originPattern(absUrl)] });
    } catch { /* a bad URL is reported in the Server section */ }
    say("folderServer", st.state !== "none" && !granted
      ? "Grant access to your server above as well: without the helper, Chrome itself " +
        "has to reach it." : "", "warn");

    // Which of the two is copying: the helper when it answers, unless told
    // otherwise. The helper's own row on this page follows the answer.
    const r = await browser.runtime.sendMessage({ type: "ping" }).catch(() => null);
    const p = r && r.ok ? r.data : null;
    USING = !!(p && p.backend === "folder");
    HELPER_UP = !!(p && p.ok && !USING);
    globalThis.ABSH_SETTINGS.helperUnused(USING);
    let use = "";
    if (st.state !== "none") {
      if (USING) {
        use = folderMode === "always"
          ? "Books go to this folder, as you chose, even though the helper may be installed."
          : "The helper isn't answering, so books go to this folder.";
      } else if (p && p.ok) {
        use = "The helper is installed and answering, so it is used, not this folder.";
      }
    }
    $("folderUse").textContent = use;
    row(st);
    return st;
  }

  build();

  $("folderPick").addEventListener("click", async () => {
    let picked;
    try {
      // Straight from the click: the picker needs the user's gesture, and an
      // await before it could spend it.
      picked = await window.showDirectoryPicker({ id: "absh-player", mode: "readwrite" });
    } catch (e) {
      if (e && e.name === "AbortError") return;     // closed without choosing
      say("folderState", `Chrome didn't open that folder: ${e.message || e}`, "err");
      return;
    }
    await F.saveHandle(picked);
    await refresh();
  });

  $("folderAllow").addEventListener("click", async () => {
    if (!HANDLE) return;
    let r;
    try {
      r = await HANDLE.requestPermission({ mode: "readwrite" });
    } catch (e) {
      r = String(e.message || e);
    }
    const st = await refresh();
    if (st.state === "prompt") {
      say("folderState", "Chrome didn't allow it" + (r && r !== "prompt" && r !== "denied"
        ? ` (${r})` : "") + ". Click Allow access again and choose Allow when Chrome asks.", "err");
    }
  });

  $("folderForget").addEventListener("click", async () => {
    await F.forgetHandle();
    await refresh();
  });

  $("folderAlways").addEventListener("change", async () => {
    await browser.storage.local.set({ folderMode: $("folderAlways").checked ? "always" : "auto" });
    await refresh();
  });

  // No event says the player arrived or left, so look again whenever the user
  // comes back to this page.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });

  refresh().then((st) => {
    // Sent here from the library page or the popup to restore access: put
    // the button where the eye lands.
    if (st.state === "prompt") {
      globalThis.ABSH_SETTINGS.openRow("folder", true);
      $("folderBox").scrollIntoView({ block: "center" });
      $("folderAllow").focus();
    }
  });
})();
