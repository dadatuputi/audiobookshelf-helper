"""Read the metadata embedded in an audio file.

Needed because a book that is on the device but not on the server has no
library item to describe it - the only thing that knows what it is, is the file
itself.

mutagen is used when it is importable, because it handles the long tail. It is
optional on purpose: the native messaging host is launched by the browser with
whatever Python is on PATH, and must not fail because a package is missing. The
fallback below parses just the handful of fields this tool needs, straight out
of the MP4 atom tree and the ID3v2 frame list.
"""
import re
import struct
from pathlib import Path

try:  # pragma: no cover - presence depends on the environment
    import mutagen
except Exception:  # pragma: no cover
    mutagen = None

EMPTY = {"title": "", "author": "", "album": "", "series": "", "track": 0, "duration": 0.0}


# ------------------------------------------------------------------ MP4/M4A
def _mp4_atoms(data, start, end):
    """Yield (name, payload_start, payload_end) for atoms in a byte range."""
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", data[i:i + 4])[0]
        name = data[i + 4:i + 8]
        if size == 1:                      # 64-bit extended size
            if i + 16 > end:
                return
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            body = i + 16
        elif size == 0:                    # runs to the end of the file
            size = end - i
            body = i + 8
        else:
            body = i + 8
        if size < 8 or i + size > end:
            return
        yield name, body, i + size
        i += size


def _mp4_find(data, path, start=0, end=None):
    """Walk a path of atom names, e.g. (b"moov", b"udta", b"meta")."""
    end = len(data) if end is None else end
    for name, body, stop in _mp4_atoms(data, start, end):
        if name != path[0]:
            continue
        if len(path) == 1:
            return body, stop
        # `meta` carries a 4-byte version/flags before its children; nothing
        # else in this path does, and skipping it wrongly loses every tag.
        if name == b"meta":
            body += 4
        return _mp4_find(data, path[1:], body, stop)
    return None


def _mp4_text(data, start, end):
    """The `data` child of a metadata atom, as text."""
    for name, body, stop in _mp4_atoms(data, start, end):
        if name == b"data" and stop - body > 8:
            return data[body + 8:stop].decode("utf-8", "replace").strip()
    return ""


def _mp4_int(data, start, end):
    for name, body, stop in _mp4_atoms(data, start, end):
        if name == b"data":
            raw = data[body + 8:stop]
            if len(raw) >= 4:
                return struct.unpack(">H", raw[2:4])[0]
    return 0


MP4_KEYS = {
    b"\xa9nam": "title",
    b"\xa9ART": "author",
    b"aART": "albumartist",
    b"\xa9alb": "album",
    b"\xa9wrt": "composer",
    b"trkn": "track",
}


def _read_mp4(path):
    data = Path(path).read_bytes()
    found = _mp4_find(data, (b"moov", b"udta", b"meta", b"ilst"))
    out = dict(EMPTY)
    if not found:
        return out
    start, end = found
    raw = {}
    for name, body, stop in _mp4_atoms(data, start, end):
        key = MP4_KEYS.get(name)
        if not key:
            continue
        raw[key] = _mp4_int(data, body, stop) if key == "track" else _mp4_text(data, body, stop)
    out["title"] = raw.get("title", "")
    # An audiobook's "artist" is the narrator about as often as the author, so
    # prefer album artist, which conventionally carries the author.
    out["author"] = raw.get("albumartist") or raw.get("author") or raw.get("composer") or ""
    out["album"] = raw.get("album", "")
    out["track"] = raw.get("track", 0) or 0
    return out


# ---------------------------------------------------------------------- MP3
def _syncsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


ID3_KEYS = {b"TIT2": "title", b"TPE1": "author", b"TALB": "album",
            b"TPE2": "albumartist", b"TRCK": "track"}


def _id3_text(payload):
    if not payload:
        return ""
    enc, body = payload[0], payload[1:]
    try:
        if enc == 0:
            s = body.decode("latin-1")
        elif enc == 1:
            s = body.decode("utf-16", "replace")
        elif enc == 2:
            s = body.decode("utf-16-be", "replace")
        else:
            s = body.decode("utf-8", "replace")
    except Exception:
        s = body.decode("latin-1", "replace")
    return s.replace("\x00", " ").strip()


def _read_id3(path):
    data = Path(path).read_bytes()
    out = dict(EMPTY)
    if len(data) < 10 or data[:3] != b"ID3":
        return out
    major = data[3]
    size = _syncsafe(data[6:10])
    end = min(10 + size, len(data))
    i = 10
    raw = {}
    while i + 10 <= end:
        fid = data[i:i + 4]
        if not fid.strip(b"\x00"):
            break
        # v2.4 uses syncsafe frame sizes; v2.3 uses plain big-endian.
        fsize = _syncsafe(data[i + 4:i + 8]) if major >= 4 else struct.unpack(">I", data[i + 4:i + 8])[0]
        i += 10
        if fsize <= 0 or i + fsize > end:
            break
        key = ID3_KEYS.get(fid)
        if key:
            raw[key] = _id3_text(data[i:i + fsize])
        i += fsize
    out["title"] = raw.get("title", "")
    out["author"] = raw.get("albumartist") or raw.get("author") or ""
    out["album"] = raw.get("album", "")
    m = re.match(r"\s*(\d+)", raw.get("track", "") or "")
    out["track"] = int(m.group(1)) if m else 0
    return out


# --------------------------------------------------------------------- API
def _read_mutagen(path):
    f = mutagen.File(str(path), easy=True)
    if f is None:
        return None
    def first(*keys):
        for k in keys:
            v = f.tags.get(k) if f.tags else None
            if v:
                return str(v[0]).strip()
        return ""
    out = dict(EMPTY)
    out["title"] = first("title")
    out["author"] = first("albumartist", "artist", "composer")
    out["album"] = first("album")
    track = first("tracknumber")
    m = re.match(r"\s*(\d+)", track or "")
    out["track"] = int(m.group(1)) if m else 0
    try:
        out["duration"] = float(getattr(f.info, "length", 0) or 0)
    except Exception:
        out["duration"] = 0.0
    return out


def read(path):
    """Best-effort tags for one file. Never raises - a bad file is just blank."""
    path = Path(path)
    if mutagen is not None:
        try:
            got = _read_mutagen(path)
            if got and (got["title"] or got["author"]):
                return got
        except Exception:
            pass  # fall through to the built-in parsers
    try:
        suffix = path.suffix.lower()
        if suffix in (".m4a", ".m4b", ".mp4", ".m4p"):
            return _read_mp4(path)
        if suffix == ".mp3":
            return _read_id3(path)
    except Exception:
        pass
    return dict(EMPTY)


def read_book(files):
    """Tags describing a book made of one or more files.

    Takes the first file's title/author, and prefers the album as the book
    title when the per-file title is just a chapter name.
    """
    files = list(files)
    if not files:
        return dict(EMPTY)
    first = read(files[0])
    out = dict(first)
    if len(files) > 1 and first.get("album"):
        # Multi-file books tag each part with its chapter; the album is the book.
        out["title"] = first["album"]
    elif not out.get("title"):
        out["title"] = first.get("album") or Path(files[0]).stem
    return out


def available():
    """Which tag reader is in play - surfaced by `absh status` and the host."""
    return "mutagen" if mutagen is not None else "builtin"
