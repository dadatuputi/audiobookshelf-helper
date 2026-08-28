/* Pure logic shared by the background script and the unit tests.
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

  /** ABS accepts the JWT as ?token= as well as a Bearer header; downloads.
   *  cannot set headers, so the URL form is what the host is handed. */
  function downloadUrl(absUrl, itemId, apiKey) {
    if (!absUrl) throw new Error("server URL not set");
    if (!itemId) throw new Error("item id required");
    return `${baseUrl(absUrl)}/api/items/${encodeURIComponent(itemId)}` +
           `/download?token=${encodeURIComponent(apiKey || "")}`;
  }

  /** Flatten an ABS library item into the shape the popup and host use. */
  function normalizeBook(it) {
    const m = it.media || {};
    const meta = m.metadata || {};
    const authors = meta.authorName ||
      (Array.isArray(meta.authors) ? meta.authors.map(a => a.name).filter(Boolean).join(", ") : "");
    const series = Array.isArray(meta.series)
      ? meta.series.map(s => s.name || s).filter(Boolean).join(", ") : "";
    return {
      id: it.id,
      title: meta.title || it.relPath || "(untitled)",
      author: authors || "",
      series,
      relPath: it.relPath || "",
      numTracks: m.numTracks || (Array.isArray(m.audioFiles) ? m.audioFiles.length : 0) || 0,
      size: it.size || m.size || 0
    };
  }

  function sortBooks(list) {
    return list.slice().sort((a, b) =>
      (a.author + " " + a.title).localeCompare(b.author + " " + b.title));
  }

  function filterBooks(list, q) {
    const s = String(q || "").trim().toLowerCase();
    if (!s) return list;
    return list.filter(b =>
      `${b.title} ${b.author} ${b.series}`.toLowerCase().includes(s));
  }

  /** The device options every host command needs. */
  function deviceOpts(cfg) {
    if (!cfg.devicePath) throw new Error("device path not set");
    return {
      devicePath: cfg.devicePath,
      subdir: cfg.subdir || "AUDIOBOOKS",
      renameM4b: cfg.renameM4b !== false,
      folderTemplate: cfg.folderTemplate || "{author} - {title}"
    };
  }

  /** Everything the native host needs for one sync request. */
  function buildSyncPayload(cfg, items) {
    const opts = deviceOpts(cfg);
    if (!Array.isArray(items) || items.length === 0) throw new Error("no items selected");
    return {
      cmd: "sync",
      ...opts,
      sourceMode: cfg.sourceMode || "auto",
      localRoot: cfg.localRoot || "",
      items: items.map(i => ({
        id: i.id, title: i.title, author: i.author, series: i.series,
        relPath: i.relPath,
        url: downloadUrl(cfg.absUrl, i.id, cfg.apiKey)
      }))
    };
  }

  /** Ask the host which of these books are already on the device.
   *
   *  The books are sent along because the on-device name is derived from the
   *  folder template - the host owns that rule, so it does the matching. */
  function buildListPayload(cfg, items) {
    return {
      cmd: "list",
      ...deviceOpts(cfg),
      items: (items || []).map(i => ({
        id: i.id, title: i.title, author: i.author, series: i.series
      }))
    };
  }

  /** Delete named entries from the device. Names come from a previous list. */
  function buildRemovePayload(cfg, names) {
    if (!Array.isArray(names) || names.length === 0) throw new Error("nothing to remove");
    return { cmd: "remove", ...deviceOpts(cfg), names: names.slice() };
  }

  /** Fold a host `list` reply into the book list as an `onDevice` field. */
  function annotateOnDevice(books, listReply) {
    const byId = new Map();
    for (const e of (listReply && listReply.onDevice) || []) {
      if (e.id) byId.set(e.id, e);
    }
    return books.map(b => {
      const e = byId.get(b.id);
      return e ? { ...b, onDevice: { name: e.name, kind: e.kind, bytes: e.bytes, files: e.files } }
                : { ...b, onDevice: null };
    });
  }

  function formatBytes(n) {
    n = Number(n) || 0;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)}${u[i]}`;
  }

  const lib = {
    DEFAULTS, baseUrl, originPattern, libraryPattern, downloadUrl,
    normalizeBook, sortBooks, filterBooks, deviceOpts,
    buildSyncPayload, buildListPayload, buildRemovePayload,
    annotateOnDevice, formatBytes
  };
  root.ABSH = lib;
  if (typeof module !== "undefined" && module.exports) module.exports = lib;
})(typeof globalThis !== "undefined" ? globalThis : self);
