"""What is actually on the player, and how it lines up with the server.

Produces the three-way view everything else is built on:

    both        on the server and on the device
    server_only on the server, not on the device   -> can be pulled
    device_only on the device, not on the server   -> can be pushed

Identity comes from the sidecar index first (exact, for anything this tool put
there), then from the file's own tags, then from the folder name. A book only
counts as device_only once all three have failed to find it on the server -
otherwise a tagging quirk would offer to upload a book the server already has.
"""
import shutil
from pathlib import Path

from . import index as index_mod
from . import tags as tags_mod
from .naming import AUDIO_EXT, norm_key, safe_subdir, target_name

INDEX_DIRNAME = ".absh"


def device_root(device_path, subdir):
    return Path(device_path) / safe_subdir(subdir)


def _dir_stats(d):
    total = count = 0
    files = []
    for p in sorted(d.rglob("*")):
        if p.is_file():
            size = p.stat().st_size
            total += size
            count += 1
            if p.suffix.lower() in AUDIO_EXT:
                files.append(p)
    return total, count, files


def scan(device_path, subdir, folder_template="{author} - {title}", read_tags=True):
    """Every book-shaped thing under <device>/<subdir>.

    A book is either one audio file or one folder of them. Anything else -
    the player's own database files, a stray cover - is ignored rather than
    reported, because the shelf is for books.
    """
    root = device_root(device_path, subdir)
    if not root.is_dir():
        return []

    idx = index_mod.load(device_path).get("entries", {})
    out = []
    for p in sorted(root.iterdir()):
        if p.name.startswith(".") or p.name == INDEX_DIRNAME:
            continue
        if p.is_dir():
            size, count, files = _dir_stats(p)
            if not files:
                continue
            entry = {"name": p.name, "kind": "dir", "bytes": size,
                     "files": count, "paths": [str(f) for f in files]}
        elif p.is_file() and p.suffix.lower() in AUDIO_EXT:
            entry = {"name": p.name, "kind": "file", "bytes": p.stat().st_size,
                     "files": 1, "paths": [str(p)]}
        else:
            continue

        rec = idx.get(p.name)
        if rec:
            entry.update({"itemId": rec.get("itemId"), "title": rec.get("title") or p.stem,
                          "author": rec.get("author") or "", "series": rec.get("series") or "",
                          "source": "index"})
        elif read_tags:
            t = tags_mod.read_book([Path(x) for x in entry["paths"]])
            # For a folder, the per-file title is usually a chapter name. The
            # album is the book; failing that the folder is a better guess
            # than "Part 1".
            title = (t.get("album") or p.name) if entry["kind"] == "dir" else t.get("title")
            entry.update({"itemId": None, "title": title or p.stem,
                          "author": t.get("author") or "", "series": "",
                          "source": "tags" if (t.get("title") or t.get("author")) else "name"})
        else:
            entry.update({"itemId": None, "title": p.stem, "author": "",
                          "series": "", "source": "name"})
        out.append(entry)

    # Anything the index still claims but the filesystem no longer has was
    # deleted outside this tool; stop reporting it.
    index_mod.prune(device_path, {e["name"] for e in out})
    return out


def free_space(device_path):
    try:
        u = shutil.disk_usage(str(device_path))
        return {"free": u.free, "total": u.total, "used": u.used}
    except OSError:
        return {}


def diff(server_items, device_entries, folder_template="{author} - {title}"):
    """Line the two sides up.

    Matching runs id -> expected-device-name -> normalised title/author, in
    that order, each one only for what the previous did not resolve.
    """
    by_id = {i["id"]: i for i in server_items if i.get("id")}
    by_name = {}
    by_key = {}
    for i in server_items:
        by_name.setdefault(target_name(i, folder_template), i)
        by_key.setdefault(norm_key(i.get("title"), i.get("author")), i)

    both, device_only = [], []
    matched_ids = set()

    for e in device_entries:
        item = None
        how = None
        if e.get("itemId") and e["itemId"] in by_id:
            item, how = by_id[e["itemId"]], "id"
        if item is None:
            stem = Path(e["name"]).stem if e["kind"] == "file" else e["name"]
            if stem in by_name:
                item, how = by_name[stem], "name"
        if item is None and (e.get("title") or e.get("author")):
            k = norm_key(e.get("title"), e.get("author"))
            if k in by_key:
                item, how = by_key[k], "tags"
        if item is not None:
            matched_ids.add(item["id"])
            both.append({**e, "item": item, "itemId": item["id"], "matchedBy": how})
        else:
            device_only.append(e)

    server_only = [i for i in server_items if i["id"] not in matched_ids]
    return {"both": both, "serverOnly": server_only, "deviceOnly": device_only}


def status(device_path, subdir, server_items, folder_template="{author} - {title}",
           read_tags=True):
    """The whole picture, ready to render."""
    entries = scan(device_path, subdir, folder_template, read_tags=read_tags)
    d = diff(server_items, entries, folder_template)
    d["free"] = free_space(device_path)
    d["onDeviceBytes"] = sum(e["bytes"] for e in entries)
    return d
