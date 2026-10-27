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
import os
import re
from struct import error as struct_error
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import config as config_mod
from . import embed
from . import index as index_mod
from . import mp4meta
from . import tags as tags_mod
from .abs_api import AbsError
from .device import device_root
from .naming import AUDIO_EXT, MAX_NAME, clean, fit, free_name, out_ext, source_ext, target_name


def _noop(_ev):
    pass


# A file being written to the player has this in front of its name until it is
# whole. The listings skip dot-names, so it is never taken for a book, and one
# left by an unplugged player is swept away by the next copy.
PART = ".absh-part-"


class DeviceGone(Exception):
    """The player went away. Not a failure of the book: the run stops there,
    and what was left is reported so it can carry on when the player is back."""


def device_present(device_path):
    return Path(device_path).is_dir()


def _write_whole(dst, fill):
    """Fill `dst` under a hidden name beside it, then rename it into place.

    Copying straight to the real name left half a book there when the player
    was unplugged, and a file that exists is a file that is skipped next
    time - so a book stayed a chapter short for good. A rename on the same
    drive is all or nothing; the hidden file, if any, goes.
    """
    part = dst.with_name(PART + dst.name)
    try:
        with open(part, "wb") as out:
            fill(out)
        os.replace(part, dst)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def _sweep_parts(device_path, root):
    """Remove what an interrupted copy left half-written."""
    if not device_present(device_path):
        return
    try:
        for p in list(root.glob(PART + "*")) + list(root.glob("*/" + PART + "*")):
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


class Report(dict):
    """Accumulates what happened, in a shape both the CLI and the host render."""

    def __init__(self):
        super().__init__(copied=0, skipped=0, updated=0, written=0, uploaded=0,
                         removed=[], freed=0, errors=[], books=0, interrupted=[])

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


def taken_names(device_path, root):
    """Every name in use in the books folder, and which library item holds it.

    A name is a folder's name or a file's stem: the template does not know
    yet whether a book arrives as one file or a folder of them. An index
    record whose book has since been deleted from the device holds nothing.
    """
    taken = {}
    try:
        for p in root.iterdir():
            taken[p.stem if p.is_file() else p.name] = None
    except OSError:
        pass
    for name, rec in index_mod.load(device_path).get("entries", {}).items():
        if rec.get("itemId") and (root / name).exists():
            taken[Path(name).stem if rec.get("kind") == "file" else name] = rec["itemId"]
    return taken


def pull_book(client, item, opts, report, emit=_noop):
    """Put one server book on the device, over the API."""
    root = device_root(opts["devicePath"], opts.get("subdir"))
    name = target_name(item, opts.get("folderTemplate"))
    rename = bool(opts.get("renameM4b", True))
    title = item.get("title") or name

    def step(done, total, label):
        emit({"event": "progress", "id": item.get("id"), "title": title,
              "file": label, "done": done, "total": total})

    full = _full_item(client, item)
    if full is not None:
        return _pull_files(client, item, full, opts, report, step, root, name, rename)

    if not opts.get("force"):
        have = already_on_device(opts, item)
        if have:
            report["skipped"] += 1
            step(1, 1, have)
            return have

    try:
        name = free_name(name, item, taken_names(opts["devicePath"], root))
    except ValueError as e:
        report.fail(title, str(e))
        return None
    got = _download(client, item, root, name, rename, report, step, opts["devicePath"])
    if got is None:
        return None
    written, entry_name, kind, src_ext = got

    index_mod.record(opts["devicePath"], entry_name, item,
                     [{"name": p.name, "size": p.stat().st_size} for p in written if p.exists()],
                     kind, source_ext=src_ext)
    report["books"] += 1
    return entry_name


def _download(client, item, root, name, rename, report, step, device_path):
    """Fetch over the API. One file, or a zip for a multi-file book.

    Raises DeviceGone if the player is not there to write to, rather than
    making its folders again: on a Mac that would put /Volumes/<player> on
    the main disk."""
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

        if not device_present(device_path):
            raise DeviceGone()
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
                    part = fit(clean(Path(member).stem), MAX_NAME - 6 - len(ext))
                    dst = target / f"{i:03d} - {part}{ext}"
                    step(i, len(members), dst.name)
                    # Written whole or not at all, so one that exists is
                    # complete: a book cut off at file 20 carries on at 21.
                    if dst.exists():
                        report["skipped"] += 1
                    else:
                        with z.open(member) as src:
                            _write_whole(dst, lambda out: shutil.copyfileobj(src, out))
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
            # A move across drives is a copy, and a copy cut off midway would
            # leave half a book under the real name.
            with open(tmp, "rb") as src:
                _write_whole(dst, lambda out: shutil.copyfileobj(src, out))
            report["copied"] += 1
        return [dst], dst.name, "file", src_ext

    except DeviceGone:
        raise
    except OSError as e:
        if not device_present(device_path):
            raise DeviceGone() from e
        report.fail(title, f"download failed - {e}")
        return None
    except (AbsError, zipfile.BadZipFile) as e:
        report.fail(title, f"download failed - {e}")
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


# ------------------------------------------------- file by file, by bytes
# With the full item from the server, a book is copied file by file and each
# file is compared with what the index says is on the player: the server's
# inode, size and mtime for that file, and the tags last written into it.
# Only what differs moves. A file the server has not changed is not
# downloaded at all; if only the tags differ, just the tag bytes are written
# on the player. A file that did change, or a forced copy, is downloaded and
# compared with the player's copy block by block, and only blocks that differ
# are written.

BLOCK = 1 << 20      # compared at a time
SECTOR = 4096        # written at a time, when it differs


def _full_item(client, item):
    """The item with its files, or None when the server (or a test double)
    cannot say - then the whole-book download is used, as before."""
    get = getattr(client, "item", None)
    if get is None or getattr(client, "open_file", None) is None:
        return None
    try:
        full = get(item["id"])
    except AbsError:
        return None
    return full if isinstance(full, dict) and embed.audio_files(full) else None


def _blocks(src, segs, size=BLOCK):
    """The bytes `segs` describe (see mp4meta.rebuild), in blocks of `size`."""
    buf = bytearray()
    for seg in segs:
        if seg[0] == "bytes":
            buf += seg[1]
        else:
            src.seek(seg[1])
            left = seg[2] - seg[1]
            while left:
                chunk = src.read(min(left, size))
                if not chunk:
                    raise OSError("source file shorter than expected")
                buf += chunk
                left -= len(chunk)
                while len(buf) >= size:
                    yield bytes(buf[:size])
                    del buf[:size]
        while len(buf) >= size:
            yield bytes(buf[:size])
            del buf[:size]
    if buf:
        yield bytes(buf)


def _put_changed(out, pos, data, old):
    """Write the sectors of `data` at `pos` that differ from `old`."""
    written = 0
    for i in range(0, len(data), SECTOR):
        new = data[i:i + SECTOR]
        if old[i:i + SECTOR] != new:
            out.seek(pos + i)
            out.write(new)
            written += len(new)
    return written


def _diff_write(dst, src, segs):
    """Make `dst` equal to what `segs` describe, writing only what differs."""
    written = 0
    pos = 0
    with open(dst, "r+b") as out:
        for block in _blocks(src, segs):
            out.seek(pos)
            written += _put_changed(out, pos, block, out.read(len(block)))
            pos += len(block)
        out.seek(0, 2)
        if out.tell() != pos:
            out.truncate(pos)
    return written


def _apply_writes(dst, writes, truncate):
    """mp4meta.retag()'s writes, in order, each only where it differs."""
    written = 0
    with open(dst, "r+b") as out:
        for pos, data in writes:
            out.seek(pos)
            written += _put_changed(out, pos, data, out.read(len(data)))
        if truncate is not None:
            out.truncate(truncate)
    return written


def _tagged(path, tags):
    """rebuild() segments for `path`, or the whole file untouched when it is
    not an MP4 this can tag (or no tags are wanted)."""
    size = path.stat().st_size
    if tags:
        try:
            with open(path, "rb") as f:
                return mp4meta.rebuild(f, tags), True
        except (mp4meta.NotMp4, OSError, struct_error):
            pass
    return [("src", 0, size)], False


def _layout(entry_name, files, name, root, rename, have):
    """Where each server file goes on the player."""
    if len(files) == 1:
        f = files[0]
        ext = out_ext(f["filename"], rename)
        fname = have if have else f"{name}{ext}"
        return "file", [root / fname]
    target = root / entry_name
    out = []
    for i, f in enumerate(files, 1):
        ext = out_ext(f["filename"], rename)
        part = fit(clean(Path(f["relPath"]).stem), MAX_NAME - 6 - len(ext))
        out.append(target / f"{i:03d} - {part}{ext}")
    return "dir", out


def _download_file(client, item_id, f, step_label, step, n, total):
    step(n - 1, total, step_label)
    with client.open_file(item_id, f["ino"]) as r, \
            tempfile.NamedTemporaryFile(delete=False) as fh:
        shutil.copyfileobj(r, fh)
        return Path(fh.name)


def _pull_files(client, item, full, opts, report, step, root, name, rename):
    title = item.get("title") or name
    device_path = opts["devicePath"]
    force = bool(opts.get("force"))
    files = embed.audio_files(full)
    want_tags = config_mod.writes_tags(opts)

    have = already_on_device(opts, item)
    rec = index_mod.load(device_path).get("entries", {}).get(have) if have else None
    kind_now = "file" if len(files) == 1 else "dir"
    if have and rec and rec.get("kind") != kind_now:
        report.fail(title, f"the book is now {len(files)} file(s) on the server but not on "
                           f"the player; remove it from the player and copy it again")
        return None
    if have:
        entry_name = have
    else:
        try:
            entry_name = free_name(name, item, taken_names(device_path, root))
        except ValueError as e:
            report.fail(title, str(e))
            return None
    kind, dsts = _layout(entry_name, files, entry_name, root, rename,
                         have if kind_now == "file" else None)
    if kind == "file":
        entry_name = dsts[0].name
    old = {r.get("name"): r for r in (rec or {}).get("files") or []}

    out_files = []
    src_ext = ""
    changed = False
    tmp = None
    try:
        for n, (f, dst) in enumerate(zip(files, dsts), 1):
            src_ext = src_ext or Path(f["filename"]).suffix.lower()
            tags = embed.mp4_tags(full, f.get("index") or n) if want_tags else None
            digest = mp4meta.tags_digest(tags) if tags else None
            r = old.get(dst.name)
            same_src = bool(r) and r.get("ino") == f["ino"] and r.get("srcSize") == f["size"] \
                and r.get("srcMtime") == f["mtimeMs"]
            here = dst.is_file()
            intact = here and r is not None and dst.stat().st_size == r.get("size")
            # A book this tool copied before it kept per-file records: same
            # size as the server's file means it is the server's file.
            legacy = here and r is not None and "ino" not in r and dst.stat().st_size == f["size"]
            record = {"name": dst.name, "ino": f["ino"], "srcSize": f["size"],
                      "srcMtime": f["mtimeMs"]}

            if not force and ((same_src and intact) or legacy):
                had = None if legacy else r.get("tags")
                if digest and had != digest:
                    step(n - 1, len(files), dst.name)
                    if not device_present(device_path):
                        raise DeviceGone()
                    try:
                        with open(dst, "rb") as fh:
                            writes, cut = mp4meta.retag(fh, tags)
                    except (mp4meta.NotMp4, struct_error):
                        writes, digest = None, None
                    if writes:
                        report["written"] += _apply_writes(dst, writes, cut)
                        report["updated"] += 1
                        changed = True
                    record["tags"] = digest
                else:
                    record["tags"] = had
                record["size"] = dst.stat().st_size
                out_files.append(record)
                continue

            tmp = _download_file(client, item["id"], f, dst.name, step, n, len(files))
            # Gone while it downloaded: stop, rather than make the player's
            # folders again on this disk below.
            if not device_present(device_path):
                raise DeviceGone()
            segs, tagged = _tagged(tmp, tags)
            record["tags"] = digest if tagged else None
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "rb") as src:
                if here:
                    report["written"] += _diff_write(dst, src, segs)
                    report["updated"] += 1
                else:
                    _write_whole(dst, lambda out: [out.write(b) for b in _blocks(src, segs)])
                    report["written"] += mp4meta.segments_length(segs)
                    report["copied"] += 1
            changed = True
            tmp.unlink()
            tmp = None
            record["size"] = dst.stat().st_size
            out_files.append(record)
    except DeviceGone:
        raise
    except OSError as e:
        if not device_present(device_path):
            raise DeviceGone() from e
        report.fail(title, f"copy failed - {e}")
        return None
    except AbsError as e:
        report.fail(title, f"download failed - {e}")
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass

    step(len(files), len(files), entry_name)
    if changed or not rec or [x.get("name") for x in rec.get("files") or []] != \
            [x["name"] for x in out_files] or any("ino" not in x for x in rec.get("files") or []) \
            or [x.get("tags") for x in rec.get("files") or []] != [x["tags"] for x in out_files]:
        index_mod.record(device_path, entry_name, item, out_files, kind, source_ext=src_ext)
    if changed or not have:
        report["books"] += 1
    else:
        report["skipped"] += 1
    return entry_name


def pull(client, items, opts, emit=_noop):
    """Copy `items` to the player. If it goes away partway, stop and list
    what was not copied in report["interrupted"], rather than failing each
    remaining book in turn - the caller carries on when it is back."""
    report = Report()
    _sweep_parts(opts["devicePath"], device_root(opts["devicePath"], opts.get("subdir")))
    for n, item in enumerate(items, 1):
        if not device_present(opts["devicePath"]):
            report["interrupted"] = [i.get("id") for i in items[n - 1:]]
            break
        emit({"event": "item", "id": item.get("id"), "title": item.get("title"),
              "index": n, "count": len(items), "op": "pull"})
        try:
            pull_book(client, item, opts, report, emit)
        except DeviceGone:
            report["interrupted"] = [i.get("id") for i in items[n - 1:]]
            break
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
        emit({"event": "item", "title": entry.get("title"), "name": entry.get("name"),
              "index": n, "count": len(entries), "op": "push"})
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
