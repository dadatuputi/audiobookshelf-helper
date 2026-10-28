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


def _library_file_for(afs, lfs):
    """The library file each audio entry is downloaded as, or None.

    Audiobookshelf serves a download by looking its inode up among the
    item's library files. An audio entry's own record can be out of date -
    its inode, its name, even its path - after the file was replaced or
    renamed on disk; asking by that inode is a 404, and a rescan does not
    mend it, since the library files are already right. So each audio entry
    is matched to a library file: by inode first (a file renamed on disk
    keeps it), then by path, then by relative path (which tells CD1/01.mp3
    from CD2/01.mp3), then by file name. What is left is paired in order,
    but only when as many are left on each side."""
    audio = [lf for lf in lfs if lf.get("ino") is not None
             and (lf.get("fileType") == "audio" if lf.get("fileType")
                  else Path((lf.get("metadata") or {}).get("filename") or "").suffix.lower() in AUDIO_EXT)]
    meta = lambda x: x.get("metadata") or {}
    keys = [(lambda x: str(x.get("ino")) if x.get("ino") is not None else None)] + \
        [(lambda x, k=k: meta(x).get(k) or None) for k in ("path", "relPath", "filename")]
    got = [None] * len(afs)
    free = list(audio)
    for key in keys:
        for i, af in enumerate(afs):
            if got[i] is None and key(af) is not None:
                hit = next((lf for lf in free if key(lf) == key(af)), None)
                if hit is not None:
                    got[i] = hit
                    free.remove(hit)
    left = [i for i, g in enumerate(got) if g is None]
    if left and len(left) == len(free):
        by_name = lambda x: meta(x).get("relPath") or meta(x).get("filename") or ""
        for i, lf in zip(sorted(left, key=lambda i: by_name(afs[i])), sorted(free, key=by_name)):
            got[i] = lf
    return got


def audio_files(full):
    """The book's audio files, in the order the download zip lists them,
    with what identifies each on the server."""
    media = full.get("media") or {}
    afs = [af for af in media.get("audioFiles") or []
           if not af.get("exclude") and af.get("ino") is not None
           and (af.get("metadata") or {}).get("filename")]
    lfs = _library_file_for(afs, full.get("libraryFiles") or [])
    out = []
    for af, lf in zip(afs, lfs):
        m = af["metadata"]
        # Size and time from the library file where there is one: that is
        # the file the download serves, and the audio entry's can be stale.
        src = (lf or {}).get("metadata") or m
        out.append({
            "ino": str((lf or af)["ino"]),
            "index": af.get("index"),
            "filename": m["filename"],
            "relPath": m.get("relPath") or m["filename"],
            "size": int(src.get("size") or m.get("size") or 0),
            "mtimeMs": int(float(src.get("mtimeMs") or m.get("mtimeMs") or 0)),
        })
    out.sort(key=lambda f: f["relPath"])
    return out
