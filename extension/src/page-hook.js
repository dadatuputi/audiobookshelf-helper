/* Runs in the PAGE's own world, not the content script's isolated one.
 *
 * The problem: to badge a book card we need its library item id, and the DOM
 * never carries it - cards render as #book-card-{index} and the id lives in
 * Vue component state. Reading Vue internals would break on any Audiobookshelf
 * release.
 *
 * The reliable source is the data Audiobookshelf itself just fetched. This
 * wraps fetch and XMLHttpRequest, watches for library-item responses, and
 * forwards the id/title/author triples to the content script by postMessage.
 * We read what the page already asked for and never issue a request of our own.
 */
(function () {
  "use strict";
  const TAG = "ABSH_ITEMS";

  function harvest(url, body) {
    let data;
    try {
      data = typeof body === "string" ? JSON.parse(body) : body;
    } catch {
      return;
    }
    if (!data || typeof data !== "object") return;

    // /api/libraries/{id}/items and friends return {results:[...]}; a single
    // item comes back bare. Both are useful.
    // The library page's main request is /personalized, which returns a bare
    // ARRAY of shelves - {id,label,type,entities,total} - not {results}. Only
    // matching {results} meant the one endpoint the library page actually
    // calls produced nothing, so no card ever learned its item id.
    const shelves = (arr) =>
      arr.flatMap((sh) => (sh && Array.isArray(sh.entities) ? sh.entities : []));
    const list = Array.isArray(data.results) ? data.results
      : Array.isArray(data.libraryItems) ? data.libraryItems
      : Array.isArray(data.entities) ? data.entities
      : Array.isArray(data) ? shelves(data)
      : (data.id && data.media) ? [data] : null;
    if (!list || !list.length) return;

    const items = [];
    for (const it of list) {
      if (!it || !it.id) continue;
      const meta = (it.media && it.media.metadata) || {};
      const authors = meta.authorName ||
        (Array.isArray(meta.authors) ? meta.authors.map(a => a && a.name).filter(Boolean).join(", ") : "");
      items.push({ id: it.id, title: meta.title || it.relPath || "", author: authors || "" });
    }
    if (items.length) {
      window.postMessage({ source: TAG, url: String(url || ""), items }, window.location.origin);
    }
  }

  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function (...args) {
      return origFetch.apply(this, args).then((res) => {
        try {
          const u = (res && res.url) || String(args[0] || "");
          if (/\/api\/(libraries|items)/.test(u)) {
            // Clone so the page still gets to read its own body.
            res.clone().text().then((t) => harvest(u, t)).catch(() => {});
          }
        } catch { /* never break the page */ }
        return res;
      });
    };
  }

  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__abshUrl = url;
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      try {
        if (/\/api\/(libraries|items)/.test(String(this.__abshUrl || ""))) {
          harvest(this.__abshUrl, this.responseText);
        }
      } catch { /* never break the page */ }
    });
    return send.apply(this, args);
  };
})();
