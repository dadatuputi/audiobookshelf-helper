"""Moving books between the server and the device.

Everything goes through the Audiobookshelf API. There is no second path that
reaches into the library's files directly: the server is the only thing this
needs to be able to see, which is the whole point of it being self-hosted.

Three operations, none of which raise on a per-book failure - a batch of 200
must not stop because one book's tags are odd. Errors are collected and
returned.

pull   server -> device   (download)
push   device -> server   (upload, undoing the .m4a rename on the way)
remove device            (delete, with hard containment checks)
"""
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import index as index_mod
from . import tags as tags_mod
from .abs_api import AbsError
from .device import device_root
from .naming import AUDIO_EXT, clean, out_ext, source_ext, target_name


def _noop(_ev):
    pass


class Report(dict):
    """Accumulates what happened, in a shape both the CLI and the host render."""

    def __init__(self):
        super().__init__(copied=0, skipped=0, uploaded=0, removed=[],
                         freed=0, errors=[], books=0)

    def fail(self, title, why):
        self["errors"].append(f"{title}: {why}")


# ------------------------------------------------------------------- pull
def already_on_device(opts, item):
    """The entry name this book already occupies, or None.

    Checked before downloading rather than after. Everything comes over the
    API now, so discovering a book is already present by fetching all of it
    first would make `pull --all` cost a full re-download every time.
    """
    root = device_root(opts["devicePath"], opts.get("subdir"))
    for name, rec in index_mod.load(opts["devicePath"]).get("entries", {}).items():
        if rec.get("itemId") and rec["itemId"] == item.get("id") and (root / name).exists():
            return name
    return None


def pull_book(client, item, opts, report, emit=_noop):
    """Put one server book on the device, over the API."""
    root = device_root(opts["devicePath"], opts.get("subdir"))
    name = target_name(item, opts.get("folderTemplate"))
    rename = bool(opts.get("renameM4b", True))
    title = item.get("title") or name

    def step(done, total, label):
        emit({"event": "progress", "id": item.get("id"), "title": title,
              "file": label, "done": done, "total": total})

    if not opts.get("force"):
        have = already_on_device(opts, item)
        if have:
            report["skipped"] += 1
            step(1, 1, have)
            return have

    got = _download(client, item, root, name, rename, report, step)
    if got is None:
        return None
    written, entry_name, kind, src_ext = got

    index_mod.record(opts["devicePath"], entry_name, item,
                     [{"name": p.name, "size": p.stat().st_size} for p in written if p.exists()],
                     kind, source_ext=src_ext)
    report["books"] += 1
    return entry_name


def _download(client, item, root, name, rename, report, step):
    """Fetch over the API. One file, or a zip for a multi-file book."""
    title = item.get("title") or name
    tmp = None
    try:
        step(0, 1, "downloading")
        with client.open_download(item["id"]) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            disp = r.headers.get("Content-Disposition") or ""
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp)
            srcname = m.group(1) if m else (title + ".bin")
            with tempfile.NamedTemporaryFile(delete=False) as fh:
                shutil.copyfileobj(r, fh)
                tmp = Path(fh.name)

        if "zip" in ctype or srcname.lower().endswith(".zip"):
            target = root / name
            target.mkdir(parents=True, exist_ok=True)
            written = []
            src_ext = ""
            with zipfile.ZipFile(tmp) as z:
                members = [n for n in z.namelist() if Path(n).suffix.lower() in AUDIO_EXT]
                if not members:
                    report.fail(title, "download contained no audio files")
                    return None
                for i, member in enumerate(sorted(members), 1):
                    src_ext = src_ext or Path(member).suffix.lower()
                    ext = out_ext(member, rename)
                    # Names are rebuilt from the index, never taken from the
                    # archive, so a crafted zip cannot escape the target dir.
                    dst = target / f"{i:03d} - {clean(Path(member).stem)}{ext}"
                    step(i, len(members), dst.name)
                    if dst.exists():
                        report["skipped"] += 1
                    else:
                        with z.open(member) as src, open(dst, "wb") as out:
                            shutil.copyfileobj(src, out)
                        report["copied"] += 1
                    written.append(dst)
            return written, name, "dir", src_ext

        src_ext = Path(srcname).suffix.lower()
        ext = out_ext(srcname, rename)
        dst = root / f"{name}{ext}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        step(1, 1, dst.name)
        if dst.exists() and dst.stat().st_size == tmp.stat().st_size:
            report["skipped"] += 1
        else:
            shutil.move(str(tmp), dst)
            tmp = None
            report["copied"] += 1
        return [dst], dst.name, "file", src_ext

    except (AbsError, OSError, zipfile.BadZipFile) as e:
        report.fail(title, f"download failed - {e}")
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def pull(client, items, opts, emit=_noop):
    report = Report()
    for n, item in enumerate(items, 1):
        emit({"event": "item", "id": item.get("id"), "title": item.get("title"),
              "index": n, "count": len(items), "op": "pull"})
        try:
            pull_book(client, item, opts, report, emit)
        except Exception as e:                      # one bad book, not the batch
            report.fail(item.get("title", "?"), str(e))
    return report


# ------------------------------------------------------------------- push
def push_entry(client, entry, opts, report, emit=_noop):
    """Upload a device-only book to Audiobookshelf."""
    title = entry.get("title") or Path(entry["name"]).stem
    author = entry.get("author") or ""
    library_id = opts.get("libraryId")
    folder_id = opts.get("folderId")
    if not library_id or not folder_id:
        report.fail(title, "no target library/folder chosen for upload")
        return None

    paths = [Path(p) for p in entry.get("paths", [])]
    paths = [p for p in paths if p.is_file()]
    if not paths:
        report.fail(title, "no audio files found on the device")
        return None

    # Undo the rename on the way back: the file was .m4b on the server and we
    # only called it .m4a so the player would touch it.
    files = []
    for i, p in enumerate(paths, 1):
        upload_name = p.name
        if opts.get("restoreM4b", True):
            ext = source_ext(p.name)
            if ext != p.suffix.lower():
                upload_name = p.stem + ext
        emit({"event": "progress", "title": title, "file": upload_name,
              "done": i, "total": len(paths)})
        files.append((upload_name, p))

    try:
        client.upload(library_id, folder_id, title, author, files,
                      series=entry.get("series") or None)
    except AbsError as e:
        report.fail(title, f"upload failed - {e}")
        return None

    report["uploaded"] += 1
    report["books"] += 1
    return title


def push(client, entries, opts, emit=_noop):
    report = Report()
    for n, entry in enumerate(entries, 1):
        emit({"event": "item", "title": entry.get("title"), "index": n,
              "count": len(entries), "op": "push"})
        try:
            push_entry(client, entry, opts, report, emit)
        except Exception as e:
            report.fail(entry.get("title", "?"), str(e))
    return report


# ----------------------------------------------------------------- remove
def remove(names, opts, emit=_noop):
    """Delete entries from the device.

    The only destructive path, so every name is checked to be a single path
    component resolving to a real child of the device folder - no traversal,
    no symlink following, nothing outside the subdir.
    """
    root = device_root(opts["devicePath"], opts.get("subdir"))
    if not Path(opts["devicePath"]).is_dir():
        raise ValueError(f"device not mounted at {opts['devicePath']}")
    root_r = root.resolve()
    report = Report()

    for name in names:
        name = str(name)
        if not name or name in (".", "..") or Path(name).name != name:
            report.fail(name, "refusing a name that is not a single entry")
            continue
        p = root / name
        if p.is_symlink():
            report.fail(name, "refusing a symlink")
            continue
        if not p.exists():
            report.fail(name, "not on the device")
            continue
        try:
            if p.resolve().parent != root_r:
                report.fail(name, "resolves outside the device folder")
                continue
            emit({"event": "item", "title": name, "op": "remove"})
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p)
            else:
                size = p.stat().st_size
                p.unlink()
            report["freed"] += size
            report["removed"].append(name)
        except OSError as e:
            report.fail(name, str(e))

    if report["removed"]:
        index_mod.forget(opts["devicePath"], report["removed"])
    return report
