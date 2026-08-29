"""The sidecar index kept on the device.

Matching a folder on a USB stick back to a library item is the hard problem in
this tool. Folder names are lossy (a template turns "The Hobbit" into
"Tolkien - The Hobbit"), tags are inconsistent, and the player may have
scribbled its own files everywhere.

So whenever this tool puts a book on a device, it writes down what it was:
item id, name, size, files. That makes the common case exact and instant.
Books it did *not* put there - side-loaded by hand - fall back to tags, which
is what device.py does.

The file lives at <device>/.absh/index.json. Losing it is not fatal; the worst
case is that everything falls back to tag matching.
"""
import json
import os
import tempfile
import time
from pathlib import Path

INDEX_DIR = ".absh"
INDEX_NAME = "index.json"
VERSION = 1


def index_path(device_path):
    return Path(device_path) / INDEX_DIR / INDEX_NAME


def load(device_path):
    """Read the index, tolerating anything - a device is removable media."""
    p = index_path(device_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": VERSION, "entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"version": VERSION, "entries": {}}
    data.setdefault("version", VERSION)
    return data


def save(device_path, data):
    """Write atomically: a half-written index on a yanked USB stick is worse
    than no index, because it would silently orphan every book on it."""
    p = index_path(device_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": VERSION, "updatedAt": _now(), "entries": data.get("entries", {})}
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".index-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except OSError:
        # FAT32 on a cheap player can refuse things; never let bookkeeping
        # break an otherwise successful copy.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record(device_path, name, item, files, kind, source_ext=""):
    """Note that `name` on the device is library item `item`."""
    data = load(device_path)
    data.setdefault("entries", {})[name] = {
        "itemId": item.get("id"),
        "title": item.get("title") or "",
        "author": item.get("author") or "",
        "series": item.get("series") or "",
        "kind": kind,
        "sourceExt": source_ext,
        "bytes": sum(f.get("size", 0) for f in files),
        "files": files,
        "syncedAt": _now(),
    }
    save(device_path, data)
    return data


def forget(device_path, names):
    """Drop entries - called after a successful delete."""
    data = load(device_path)
    entries = data.setdefault("entries", {})
    dropped = [n for n in names if entries.pop(n, None) is not None]
    if dropped:
        save(device_path, data)
    return dropped


def prune(device_path, present_names):
    """Remove records for anything no longer on the device.

    Someone deleting a folder in Finder is normal; the index should not keep
    claiming the book is there.
    """
    data = load(device_path)
    entries = data.setdefault("entries", {})
    stale = [n for n in entries if n not in present_names]
    for n in stale:
        entries.pop(n, None)
    if stale:
        save(device_path, data)
    return stale
