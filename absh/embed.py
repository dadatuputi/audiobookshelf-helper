"""The tags Audiobookshelf's own "Embed Metadata" would write.

Mirrors getFFMetadataObject in server/utils/ffmpegHelpers.js, which ffmpeg
then writes with -map_metadata 1 -map_metadata 0: these keys win, anything
else already in the file stays. Each file also gets its track number, as
ffmpeg is given -metadata track=<index>.

Audiobookshelf itself does the writing, through its Embed Metadata tool;
this is what lets a copy know whether what was embedded is still current
without asking the server to embed again.
"""
import hashlib
import json


def _names(v, key="name"):
    if isinstance(v, list):
        return [x.get(key) if isinstance(x, dict) else str(x) for x in v if x]
    return []


def ffmetadata(full):
    """The ffmetadata keys for a full library item (GET /api/items/:id?expanded=1)."""
    media = full.get("media") or {}
    meta = media.get("metadata") or {}
    authors = ", ".join(n for n in _names(meta.get("authors")) if n) or meta.get("authorName") or ""
    narrators = ", ".join(n for n in _names(meta.get("narrators"), None) if n) \
        if isinstance(meta.get("narrators"), list) else (meta.get("narratorName") or "")
    series = []
    for s in meta.get("series") or []:
        if isinstance(s, dict) and s.get("name"):
            seq = s.get("sequence") or (s.get("bookSeries") or {}).get("sequence")
            series.append(s["name"] + (f" #{seq}" if seq else ""))
    title = meta.get("title") or ""
    subtitle = meta.get("subtitle") or ""
    out = {
        "title": title,
        "artist": authors,
        "album_artist": authors,
        "album": title + (f": {subtitle}" if subtitle else ""),
        "genre": "; ".join(g for g in (meta.get("genres") or []) if g),
        "date": str(meta.get("publishedYear") or ""),
        "comment": meta.get("description") or "",
        "description": meta.get("description") or "",
        "composer": narrators,
        "copyright": meta.get("publisher") or "",
        "grouping": "; ".join(series),
    }
    # ffmpeg drops empty values, and so does Audiobookshelf before it.
    return {k: v for k, v in out.items() if v}


def digest(full):
    """A fingerprint of what Embed Metadata would write for this book: its
    details and its files' track numbers. The same details, the same value."""
    want = [ffmetadata(full), [(f["relPath"], f["index"]) for f in audio_files(full)]]
    raw = json.dumps(want, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def audio_files(full):
    """The book's audio files, in the order the download zip lists them,
    with what identifies each on the server."""
    media = full.get("media") or {}
    # A file is downloaded by the inode in the item's list of files. The one
    # its audio entry carries can be out of date - after the file has been
    # replaced, say - and asking for that one is a 404.
    by_path = {}
    for lf in full.get("libraryFiles") or []:
        m = lf.get("metadata") or {}
        if lf.get("ino") is not None and m.get("path"):
            by_path[m["path"]] = str(lf["ino"])
    out = []
    for af in media.get("audioFiles") or []:
        if af.get("exclude"):
            continue
        m = af.get("metadata") or {}
        if af.get("ino") is None or not m.get("filename"):
            continue
        out.append({
            "ino": by_path.get(m.get("path"), str(af["ino"])),
            "index": af.get("index"),
            "filename": m["filename"],
            "relPath": m.get("relPath") or m["filename"],
            "size": int(m.get("size") or 0),
            "mtimeMs": int(float(m.get("mtimeMs") or 0)),
        })
    out.sort(key=lambda f: f["relPath"])
    return out
