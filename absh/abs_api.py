"""Audiobookshelf REST client.

Standard library only, deliberately: this module is imported by the native
messaging host, which the browser launches with whatever Python happens to be
on PATH. A missing `requests` there is a bug report that reads "the extension
does nothing and says nothing".

The endpoints and fields used here are pinned in tests/fixtures/abs/contract.json
and asserted by the contract tests, so an upstream change fails a build rather
than silently emptying the book list.
"""
import base64
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 900  # a whole audiobook over a slow LAN


class AbsError(RuntimeError):
    """Anything the server refused, with enough context to act on."""

    def __init__(self, message, status=None, path=None):
        super().__init__(message)
        self.status = status
        self.path = path


def _encode_multipart(fields, files):
    """Build a multipart/form-data body.

    urllib has no multipart support and this is the one place we need it, so a
    small encoder beats a dependency in a module the browser has to be able to
    import.

    fields: {name: str}
    files:  [(field_name, filename, bytes_or_path)]
    """
    boundary = "----abshelper" + uuid.uuid4().hex
    crlf = b"\r\n"
    out = []
    for name, value in fields.items():
        if value is None:
            continue
        out.append(b"--" + boundary.encode())
        out.append(('Content-Disposition: form-data; name="%s"' % name).encode())
        out.append(b"")
        out.append(str(value).encode("utf-8"))
    for field, filename, payload in files:
        if not isinstance(payload, (bytes, bytearray)):
            payload = Path(payload).read_bytes()
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out.append(b"--" + boundary.encode())
        out.append(('Content-Disposition: form-data; name="%s"; filename="%s"'
                    % (field, filename)).encode("utf-8"))
        out.append(("Content-Type: %s" % ctype).encode())
        out.append(b"")
        out.append(bytes(payload))
    out.append(b"--" + boundary.encode() + b"--")
    out.append(b"")
    return crlf.join(out), "multipart/form-data; boundary=" + boundary


class Client:
    """A thin Audiobookshelf client.

    Auth is an API key from Settings -> API Keys, sent only in the
    Authorization header - downloads included. Audiobookshelf would also take
    it as ?token= in a URL, and this used to build such URLs; nothing used
    them, and a key in a URL ends up in server logs and proxies, so it never
    goes in one. store/PRIVACY.md says so, and a test holds the code to it.
    """

    def __init__(self, url, api_key, timeout=DEFAULT_TIMEOUT, opener=None):
        self.base = str(url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        # Injectable so tests can drive this without a server.
        self._opener = opener or urllib.request.urlopen
        if not self.base:
            raise AbsError("Audiobookshelf URL is not set")
        scheme = urllib.parse.urlparse(self.base).scheme.lower()
        if scheme not in ("http", "https"):
            raise AbsError(f"server URL must be http or https, got {scheme or 'nothing'!r}")

    # ------------------------------------------------------------- transport
    def _request(self, path, data=None, content_type=None, method=None, timeout=None):
        url = self.base + path
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
        req.add_header("Authorization", "Bearer " + self.api_key)
        req.add_header("Accept", "application/json")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            return self._opener(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            hint = ""
            if e.code in (401, 403):
                hint = " - check the API key"
            elif e.code == 404:
                hint = " - check the server URL"
            raise AbsError(f"Audiobookshelf {path} responded {e.code}{hint}. {detail}".strip(),
                           status=e.code, path=path)
        except urllib.error.URLError as e:
            raise AbsError(f"cannot reach Audiobookshelf at {self.base}: {e.reason}", path=path)

    def _json(self, path, **kw):
        with self._request(path, **kw) as r:
            body = r.read().decode("utf-8")
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except ValueError:
            raise AbsError(f"Audiobookshelf {path} did not return JSON "
                           f"(is the URL the server root?)", path=path)

    # ------------------------------------------------------------------ API
    def ping(self):
        """Confirm the URL and key work, and report the server version."""
        try:
            me = self._json("/api/me")
        except AbsError:
            raise
        return {"ok": True, "user": me.get("username") or me.get("id") or "?"}

    def libraries(self):
        d = self._json("/api/libraries")
        libs = d if isinstance(d, list) else d.get("libraries", [])
        return [{"id": l.get("id"), "name": l.get("name"),
                 "mediaType": l.get("mediaType")}
                for l in libs if l.get("mediaType") == "book"]

    def library_folders(self, library_id):
        """Folders a library watches - upload needs one to put the book in."""
        d = self._json(f"/api/libraries/{urllib.parse.quote(str(library_id))}")
        lib = d.get("library", d)
        return [{"id": f.get("id"), "fullPath": f.get("fullPath")}
                for f in (lib.get("folders") or [])]

    def items(self, library_id):
        """Every book in a library, flattened to what this tool needs."""
        path = (f"/api/libraries/{urllib.parse.quote(str(library_id))}"
                f"/items?limit=0&minified=1")
        d = self._json(path)
        return [normalize_item(it) for it in (d.get("results") or [])]

    # What each kind of Audiobookshelf page lists, asked the way that page
    # asks. A series page filters its library; the filter value is base64 of
    # the id, as the app's own $encode makes it. An author page asks for the
    # author with their books, which also finds them in whichever library.
    GROUPS = ("series", "authors", "collection", "playlist")

    def group_ids(self, kind, group_id, library_id=None):
        """The library item ids a series, author, collection or playlist page
        shows, in its order. Podcast episodes in a playlist are left out."""
        q = urllib.parse.quote
        if kind == "authors":
            d = self._json(f"/api/authors/{q(str(group_id))}?include=items")
            return [it.get("id") for it in (d.get("libraryItems") or []) if it.get("id")]
        if kind == "series":
            enc = base64.b64encode(str(group_id).encode("utf-8")).decode("ascii")
            d = self._json(f"/api/libraries/{q(str(library_id))}/items?limit=0&minified=1"
                           f"&filter={kind}.{q(enc, safe='')}")
            return [it.get("id") for it in (d.get("results") or []) if it.get("id")]
        if kind == "collection":
            d = self._json(f"/api/collections/{q(str(group_id))}")
            return [b.get("id") for b in (d.get("books") or []) if b.get("id")]
        if kind == "playlist":
            d = self._json(f"/api/playlists/{q(str(group_id))}")
            return [i.get("libraryItemId") for i in (d.get("items") or [])
                    if i.get("libraryItemId") and not i.get("episodeId")]
        raise AbsError(f"unknown kind of group: {kind}")

    def open_download(self, item_id, timeout=DOWNLOAD_TIMEOUT):
        """Open the download stream for an item. Caller closes it."""
        return self._request(f"/api/items/{urllib.parse.quote(str(item_id))}/download",
                             timeout=timeout)

    def item(self, item_id):
        """One library item in full: every audio file with its inode, size
        and modification time, and the metadata Audiobookshelf would embed."""
        return self._json(f"/api/items/{urllib.parse.quote(str(item_id))}?expanded=1")

    def open_file(self, item_id, ino, timeout=DOWNLOAD_TIMEOUT):
        """Open the download stream for one file of an item, by its inode."""
        q = urllib.parse.quote
        return self._request(f"/api/items/{q(str(item_id))}/file/{q(str(ino))}/download",
                             timeout=timeout)

    def embed_metadata(self, item_id):
        """Start Audiobookshelf's own Embed Metadata on a book: it writes the
        book's details, cover and chapters into its files in the library.
        Admins only. It runs as a task; see embedding()."""
        with self._request(f"/api/tools/item/{urllib.parse.quote(str(item_id))}/embed-metadata",
                           data=b"", method="POST"):
            pass

    def scan_item(self, item_id):
        """Have Audiobookshelf read a book's folder again, so its list of the
        book's files matches the disk. Admins only."""
        with self._request(f"/api/items/{urllib.parse.quote(str(item_id))}/scan",
                           data=b"", method="POST"):
            pass

    def embedding(self, item_id):
        """Whether an embed for this book is still queued or running."""
        d = self._json("/api/tasks?include=queue")
        running = any(t.get("action") == "embed-metadata" and not t.get("isFinished")
                      and (t.get("data") or {}).get("libraryItemId") == item_id
                      for t in d.get("tasks") or [])
        queued = any(q.get("libraryItemId") == item_id
                     for q in (d.get("queuedTaskData") or {}).get("embedMetadata") or [])
        return running or queued

    def upload(self, library_id, folder_id, title, author, files, series=None,
               timeout=DOWNLOAD_TIMEOUT):
        """Create a new library item from local files.

        `files` is [(filename, path_or_bytes)]. Audiobookshelf places the book
        under <folder>/<author>/<title>, so title and author decide where it
        lands as well as what it is called.
        """
        if not files:
            raise AbsError("nothing to upload")
        fields = {"library": library_id, "folder": folder_id,
                  "title": title, "author": author or ""}
        if series:
            fields["series"] = series
        payload = [("file%d" % i, name, src) for i, (name, src) in enumerate(files)]
        body, ctype = _encode_multipart(fields, payload)
        with self._request("/api/upload", data=body, content_type=ctype,
                           method="POST", timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        try:
            return json.loads(raw) if raw.strip() else {"ok": True}
        except ValueError:
            return {"ok": True, "raw": raw[:200]}


def normalize_item(it):
    """Flatten an Audiobookshelf library item into this tool's shape.

    Handles both the minified form (metadata.authorName) and the full form
    (metadata.authors[]), because which one you get depends on the query.
    """
    media = it.get("media") or {}
    meta = media.get("metadata") or {}
    authors = meta.get("authorName")
    if not authors and isinstance(meta.get("authors"), list):
        authors = ", ".join(a.get("name") for a in meta["authors"] if a.get("name"))
    series = meta.get("seriesName")
    if not series and isinstance(meta.get("series"), list):
        series = ", ".join(s.get("name") if isinstance(s, dict) else str(s)
                           for s in meta["series"] if s)
    narrators = meta.get("narratorName")
    if not narrators and isinstance(meta.get("narrators"), list):
        narrators = ", ".join(str(n) for n in meta["narrators"] if n)
    tracks = media.get("numTracks")
    if not tracks and isinstance(media.get("audioFiles"), list):
        tracks = len(media["audioFiles"])
    return {
        "id": it.get("id"),
        "title": meta.get("title") or it.get("relPath") or "(untitled)",
        "author": authors or "",
        "series": series or "",
        "narrator": narrators or "",
        # What tells two versions of one book apart, beyond the narrator.
        "subtitle": meta.get("subtitle") or "",
        # Halves round up, as Math.round does in the Chrome folder backend;
        # round() would take 2.5 to 2.
        "duration": int(float(media.get("duration") or 0) + 0.5),
        "publishedYear": str(meta.get("publishedYear") or ""),
        "relPath": it.get("relPath") or "",
        "numTracks": tracks or 0,
        "size": it.get("size") or media.get("size") or 0,
        # Moves when the book's details or files change on the server; the
        # index keeps the value it had when the book was copied.
        "updatedAt": it.get("updatedAt") or 0,
    }
