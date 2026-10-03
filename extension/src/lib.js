/* The little that still belongs in JavaScript.
 *
 * Building sync payloads, talking to Audiobookshelf and matching books to the
 * device all moved into the absh Python package, which the CLI, the TUI and
 * the native host share. What is left is what the extension itself needs:
 * turning the configured server URL into the one permission to request,
 * formatting bytes, and deciding when to say the helper is out of date.
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
    subdir: "AUDIOBOOKS"
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

  /** Where the toolbar button belongs: the library pages of that one server.
   *
   *  The path matters. Audiobookshelf is very often reverse-proxied under a
   *  subpath - https://host/audiobookshelf - and dropping it here produced a
   *  pattern for https://host/library/*, which never matches
   *  https://host/audiobookshelf/library/... The content script then simply
   *  never ran, with no error anywhere: no badges, no toolbar button, and a
   *  page that looks untouched. Keep whatever path the server is served
   *  under. */
  function libraryPattern(absUrl) {
    const u = new URL(baseUrl(absUrl));
    const base = u.pathname.replace(/\/+$/, "");
    return `${u.protocol}//${u.host}${base}/library/*`;
  }

  function formatBytes(n) {
    n = Number(n) || 0;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)}${u[i]}`;
  }

  /** The pattern without its trailing star, for a plain prefix test on a URL. */
  function libraryPrefix(absUrl) {
    return libraryPattern(absUrl).replace(/\*$/, "");
  }

  /* ------------------------------------------------------- helper updates
   *
   * The helper answers what is installed and what the latest release is; the
   * page has to decide, from a cached copy of the second, whether to say an
   * update is waiting and whether it is time to ask again. Same ordering rule
   * as absh/releases.py. */

  const SEMVER = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/;

  /** [major, minor, patch, prerelease ids], or null for anything that is not
   *  a version - "dev" and "unknown" are not older or newer than a release. */
  function parseVersion(v) {
    const m = SEMVER.exec(String(v == null ? "" : v).trim());
    if (!m) return null;
    const pre = m[4] ? m[4].split(".").map((p) => (/^\d+$/.test(p) ? Number(p) : p)) : [];
    return [Number(m[1]), Number(m[2]), Number(m[3]), pre];
  }

  /** -1, 0 or 1 as a is older than, the same as, or newer than b; null when
   *  either is not a version. Semver order: alpha.10 after alpha.9, and
   *  1.0.0 after every 1.0.0-something. */
  function compareVersions(a, b) {
    const pa = parseVersion(a);
    const pb = parseVersion(b);
    if (!pa || !pb) return null;
    for (let i = 0; i < 3; i++) {
      if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
    }
    const x = pa[3];
    const y = pb[3];
    if (!x.length || !y.length) return Math.sign(y.length - x.length) || 0;
    for (let i = 0; i < Math.min(x.length, y.length); i++) {
      if (x[i] === y[i]) continue;
      const xn = typeof x[i] === "number";
      const yn = typeof y[i] === "number";
      if (xn !== yn) return xn ? -1 : 1;     // numeric identifiers sort first
      return x[i] < y[i] ? -1 : 1;
    }
    return Math.sign(x.length - y.length) || 0;
  }

  /* How often to ask. The release feed is GitHub's unauthenticated API: 60
   * requests an hour, shared by everything behind the same address, and a
   * check costs two - four when there is something newer, whose signature
   * it also reads. A helper is released every few weeks at most, so once a
   * day finds a release within a day of it shipping, which is soon enough for
   * something that never updates without being asked. A failed check is tried
   * again sooner - it is usually being offline, and a failure that did reach
   * GitHub still costs at most a couple of dozen requests a day. */
  const UPDATE_CHECK_EVERY = 24 * 60 * 60 * 1000;
  const UPDATE_RETRY_AFTER = 60 * 60 * 1000;

  /** Whether to ask the release feed now, given the last answer.
   *
   *  Always when asked to, when there is no answer, or when the helper is no
   *  longer the one that answer was about - updated from here or from the
   *  command line, the old answer says nothing about the new copy. A
   *  timestamp from the future means the clock moved; trust nothing. */
  function updateCheckDue(cache, installed, now, force) {
    if (force) return true;
    if (!cache || typeof cache.checkedAt !== "number") return true;
    if (cache.installed !== installed) return true;
    const age = now - cache.checkedAt;
    if (!(age >= 0)) return true;
    return age >= (cache.error ? UPDATE_RETRY_AFTER : UPDATE_CHECK_EVERY);
  }

  /** What to tell the user about updating the helper.
   *
   *  `helper` is the ping answer, which is always fresh: the installed release
   *  and whether this copy can replace itself (`refused` - a checkout, a
   *  folder it cannot write, no signing key pinned). `check` is the cached
   *  feed answer, including whether this copy would accept the latest release
   *  (`releaseRefused` - unsigned, or signed by a key it does not trust).
   *  Either one means no button. Whether an update is waiting is worked out
   *  here rather than cached, so it is right the moment the helper changes.
   *
   *  kind: unreachable | unsupported | unversioned | unchecked | failed |
   *        available | current */
  function updateState(helper, check) {
    if (!helper || !helper.ok) {
      return { kind: "unreachable", error: (helper && helper.error) || "" };
    }
    check = check || {};
    const s = {
      installed: helper.release || "unknown",
      refused: helper.updateRefused || "",
      releaseRefused: check.releaseRefused || "",
      latest: check.latest || "",
      prerelease: !!check.prerelease,
      error: check.error || "",
      checkedAt: typeof check.checkedAt === "number" ? check.checkedAt : null,
    };
    if (check.unsupported) return { ...s, kind: "unsupported" };
    if (!parseVersion(s.installed)) return { ...s, kind: "unversioned" };
    const c = s.latest ? compareVersions(s.latest, s.installed) : null;
    if (c === null) return { ...s, kind: s.error ? "failed" : "unchecked" };
    if (c === 1) {
      return { ...s, kind: "available", canUpdate: !s.refused && !s.releaseRefused };
    }
    return { ...s, kind: "current", ahead: c === -1 };
  }

  const lib = { DEFAULTS, baseUrl, originPattern, libraryPattern, libraryPrefix,
                formatBytes, parseVersion, compareVersions, updateCheckDue,
                updateState, UPDATE_CHECK_EVERY, UPDATE_RETRY_AFTER };
  root.ABSH = lib;
  if (typeof module !== "undefined" && module.exports) module.exports = lib;
})(typeof globalThis !== "undefined" ? globalThis : self);
