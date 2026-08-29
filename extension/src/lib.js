/* The little that still belongs in JavaScript.
 *
 * Building sync payloads, talking to Audiobookshelf and matching books to the
 * device all moved into the absh Python package, which the CLI, the TUI and
 * the native host share. What is left is what the extension itself needs:
 * turning the configured server URL into the one permission to request, and
 * formatting bytes.
 *
 * Still a classic script with a CommonJS tail so it loads three ways without a
 * bundler: Firefox's MV3 background "scripts", Chrome's module service worker,
 * and node for tests.
 *
 * Written as a classic script that also exports under CommonJS, so it works in
 * three places without a bundler: Firefox's MV3 background "scripts" array,
 * Chrome's module service worker, and node-based unit tests. */
(function (root) {
  "use strict";

  const DEFAULTS = {
    absUrl: "", apiKey: "", devicePath: "", libraryId: "",
    renameM4b: true, folderTemplate: "{author} - {title}",
    subdir: "AUDIOBOOKS", sourceMode: "auto", localRoot: ""
  };

  /** Trim a trailing slash so URL joining never doubles up. */
  function baseUrl(u) {
    return String(u || "").replace(/\/+$/, "");
  }

  /** The host permission this install actually needs: the user's own server.
   *
   *  The manifest ships no host permissions at all - asking every user for
   *  every site to reach one self-hosted server is the kind of thing store
   *  reviewers reject, and rightly. This turns the configured server URL into
   *  the single origin pattern to request at runtime. */
  function originPattern(absUrl) {
    const u = new URL(baseUrl(absUrl));
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      throw new Error("server URL must be http or https");
    }
    return `${u.protocol}//${u.host}/*`;
  }

  /** Where the toolbar button belongs: the library pages of that one server. */
  function libraryPattern(absUrl) {
    const u = new URL(baseUrl(absUrl));
    return `${u.protocol}//${u.host}/library/*`;
  }

  function formatBytes(n) {
    n = Number(n) || 0;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)}${u[i]}`;
  }

  const lib = { DEFAULTS, baseUrl, originPattern, libraryPattern, formatBytes };
  root.ABSH = lib;
  if (typeof module !== "undefined" && module.exports) module.exports = lib;
})(typeof globalThis !== "undefined" ? globalThis : self);
