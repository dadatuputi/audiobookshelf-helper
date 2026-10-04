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

  function build() {
    const box = h("div", { class: "grant", id: "folderBox" },
      h("strong", {}, "Chrome can copy to your player itself"),
      h("div", { class: "hint" },
        "If you can't install the helper, or would rather not, choose the player's top " +
        "folder - the one your books folder is in - and Chrome writes there directly. The " +
        "popup and your library page then work without the helper. Whenever the helper is " +
        "installed and answering, it is used instead."),
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
        h("button", { id: "folderPick" }, "Choose the player's folder…"),
        h("button", { id: "folderAllow", class: "hidden" }, "Allow access"),
        h("button", { id: "folderForget", class: "secondary hidden" }, "Forget this folder")),
      h("div", { id: "folderState", class: "note" }),
      h("div", { id: "folderServer", class: "note" }),
      h("div", { id: "folderUse", class: "hint" }),
      h("div", { class: "chk" },
        h("input", { type: "checkbox", id: "folderAlways" }),
        h("label", { for: "folderAlways", style: "margin:0" },
          "Use this folder even when the helper is installed")));
    const naming = [...document.querySelectorAll("h2")].find((x) => x.textContent === "Naming");
    const title = h("h2", { id: "folder" }, "Without the helper (Chrome only)");
    naming.before(title, box);
  }

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

    let use = "";
    if (st.state !== "none") {
      const r = await browser.runtime.sendMessage({ type: "ping" }).catch(() => null);
      const p = r && r.ok ? r.data : null;
      if (p && p.backend === "folder") {
        use = folderMode === "always"
          ? "Books go to this folder, as you chose, even though the helper may be installed."
          : "The helper isn't answering, so books go to this folder.";
      } else if (p && p.ok) {
        use = "The helper is installed and answering, so it is used, not this folder.";
      }
    }
    $("folderUse").textContent = use;
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
      $("folderBox").scrollIntoView({ block: "center" });
      $("folderAllow").focus();
    }
  });
})();
