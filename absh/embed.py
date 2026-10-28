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
from pathlib import Path

from .naming import AUDIO_EXT


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


def _download_inos(afs, lfs):
    """The inode to download each audio file by.

    Audiobookshelf finds a file to download among the item's library files,
    by inode. The audio entry's own record can be out of date - its inode,
    even its path - after the file was replaced; asking by that inode is a
    404, and a rescan does not mend it, since the library files are right.
    So each audio file is matched to a library file: by path, then by file
    name, then, for what is left, in order when as many are left on each
    side. Only one with no match at all is asked for by its own inode."""
    audio = [lf for lf in lfs if lf.get("ino") is not None
             and (lf.get("fileType") == "audio" if lf.get("fileType")
                  else Path((lf.get("metadata") or {}).get("filename") or "").suffix.lower() in AUDIO_EXT)]
    meta = lambda x: x.get("metadata") or {}
    got = [None] * len(afs)
    free = list(audio)
    for key in ("path", "filename", "relPath"):
        for i, af in enumerate(afs):
            if got[i] is None and meta(af).get(key):
                hit = next((lf for lf in free if meta(lf).get(key) == meta(af)[key]), None)
                if hit is not None:
                    got[i] = hit
                    free.remove(hit)
    left = [i for i, g in enumerate(got) if g is None]
    if left and len(left) == len(free):
        free.sort(key=lambda lf: meta(lf).get("relPath") or meta(lf).get("filename") or "")
        for i, lf in zip(sorted(left, key=lambda i: meta(afs[i]).get("relPath") or ""), free):
            got[i] = lf
    return [str((g or af)["ino"]) for g, af in zip(got, afs)]


def audio_files(full):
    """The book's audio files, in the order the download zip lists them,
    with what identifies each on the server."""
    media = full.get("media") or {}
    afs = [af for af in media.get("audioFiles") or []
           if not af.get("exclude") and af.get("ino") is not None
           and (af.get("metadata") or {}).get("filename")]
    inos = _download_inos(afs, full.get("libraryFiles") or [])
    out = []
    for af, ino in zip(afs, inos):
        m = af["metadata"]
        out.append({
            "ino": ino,
            "index": af.get("index"),
            "filename": m["filename"],
            "relPath": m.get("relPath") or m["filename"],
            "size": int(m.get("size") or 0),
            "mtimeMs": int(float(m.get("mtimeMs") or 0)),
        })
    out.sort(key=lambda f: f["relPath"])
    return out
