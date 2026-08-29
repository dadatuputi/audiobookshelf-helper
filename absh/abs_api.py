"""Audiobookshelf REST client.

Standard library only, deliberately: this module is imported by the native
messaging host, which the browser launches with whatever Python happens to be
on PATH. A missing `requests` there is a bug report that reads "the extension
does nothing and says nothing".

The endpoints and fields used here are pinned in tests/fixtures/abs/contract.json
and asserted by the contract tests, so an upstream change fails a build rather
than silently emptying the book list.
"""
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .naming import AUDIO_EXT

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

    Auth is an API key from Settings -> API Keys. It goes in the Authorization
    header for API calls, and as ?token= on download URLs - Audiobookshelf
    accepts both, which is what lets a plain URL be handed to a downloader that
    cannot set headers.
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

    def download_url(self, item_id):
        """A URL that authenticates itself, for handing to a plain downloader."""
        return (f"{self.base}/api/items/{urllib.parse.quote(str(item_id))}"
                f"/download?token={urllib.parse.quote(self.api_key)}")

    def open_download(self, item_id, timeout=DOWNLOAD_TIMEOUT):
        """Open the download stream for an item. Caller closes it."""
        return self._request(f"/api/items/{urllib.parse.quote(str(item_id))}/download",
                             timeout=timeout)

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
    tracks = media.get("numTracks")
    if not tracks and isinstance(media.get("audioFiles"), list):
        tracks = len(media["audioFiles"])
    return {
        "id": it.get("id"),
        "title": meta.get("title") or it.get("relPath") or "(untitled)",
        "author": authors or "",
        "series": series or "",
        "relPath": it.get("relPath") or "",
        "numTracks": tracks or 0,
        "size": it.get("size") or media.get("size") or 0,
    }


def local_files(local_root, rel_path):
    """Audio files for a book on a mounted share, in stable order.

    Sorting by parent directory first is what flattens "CD 1"/"CD 2" folders
    into the right order rather than interleaving the discs.
    """
    if not local_root or not rel_path:
        return []
    d = Path(local_root) / rel_path
    if not d.is_dir():
        return []
    fs = [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT]
    return sorted(fs, key=lambda p: (str(p.parent).lower(), p.name.lower()))
