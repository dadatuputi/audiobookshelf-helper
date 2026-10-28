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
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from . import config as config_mod
from . import embed
from . import index as index_mod
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
        super().__init__(copied=0, skipped=0, updated=0, written=0, wrote={}, failed={}, noted={},
                         uploaded=0, removed=[], freed=0, errors=[], notes=[], books=0, interrupted=[])
        # The book being copied: what is said about it is kept by its id too,
        # since two editions can share a title and "<title>: why" cannot say
        # which of them it means.
        self.book = None

    def _by_book(self, key, said):
        if self.book is not None:
            was = self[key].get(self.book)
            self[key][self.book] = f"{was}; {said}" if was else said

    def fail(self, title, why):
        self["errors"].append(f"{title}: {why}")
        self._by_book("failed", str(why))

    def note(self, title, what):
        """Worth saying, but the book is fine: not an error."""
        self["notes"].append(f"{title}: {what}")
        self._by_book("noted", str(what))


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
        try:
            return _pull_files(client, item, full, opts, report, step, root, name, rename)
        except StaleFiles as stale:
            # A file Audiobookshelf lists for the book but cannot find: its
            # list is out of date. Have it read the book's folder again, then
            # copy what it lists now.
            try:
                client.scan_item(item["id"])
            except AbsError as e:
                report.fail(title, f"download failed - {stale.error}; Audiobookshelf's list of "
                                   f"this book's files looks out of date, and it would not rescan "
                                   f"it ({e}): open the book in Audiobookshelf and choose Re-Scan")
                return None
            full = _full_item(client, item) or full
            return _pull_files(client, item, full, opts, report, step, root, name, rename,
                               embedded=stale.embedded, rescanned=True)

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
    got = _download(client, item, root, name, rename, report, step, opts["devicePath"],
                    force=bool(opts.get("force")))
    if got is None:
        return None
    written, entry_name, kind, src_ext = got

    index_mod.record(opts["devicePath"], entry_name, item,
                     [{"name": p.name, "size": p.stat().st_size} for p in written if p.exists()],
                     kind, source_ext=src_ext)
    report["books"] += 1
    return entry_name


def _download(client, item, root, name, rename, report, step, device_path, force=False):
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
                    if dst.exists() and force:
                        # Forced: only the sectors that differ are written.
                        with z.open(member) as src:
                            n = _diff_write(dst, src)
                        report["written"] += n
                        report["copied" if n else "skipped"] += 1
                    elif dst.exists():
                        report["skipped"] += 1
                    else:
                        with z.open(member) as src:
                            _write_whole(dst, lambda out: shutil.copyfileobj(src, out))
                        report["written"] += dst.stat().st_size
                        report["copied"] += 1
                    written.append(dst)
            return written, name, "dir", src_ext

        src_ext = Path(srcname).suffix.lower()
        ext = out_ext(srcname, rename)
        dst = root / f"{name}{ext}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        step(1, 1, dst.name)
        if dst.exists() and force:
            # Forced: compared with what is there, and only what differs is
            # written - a same-size file with the wrong bytes is put right.
            with open(tmp, "rb") as src:
                n = _diff_write(dst, src)
            report["written"] += n
            report["copied" if n else "skipped"] += 1
        elif dst.exists() and dst.stat().st_size == tmp.stat().st_size:
            report["skipped"] += 1
        else:
            # A move across drives is a copy, and a copy cut off midway would
            # leave half a book under the real name.
            with open(tmp, "rb") as src:
                _write_whole(dst, lambda out: shutil.copyfileobj(src, out))
            report["written"] += dst.stat().st_size
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
# inode, size and mtime for that file. A file the server has not changed is
# not downloaded at all. One that did change, or a forced copy, is downloaded
# and compared with the player's copy block by block, and only the sectors
# that differ are written.
#
# Tags are Audiobookshelf's to write: asked to, it runs its own Embed
# Metadata on the book, which rewrites the files in the library, and the
# result is copied as above. The index keeps a fingerprint of the details
# that were embedded, so a book is only embedded again when they change.

BLOCK = 1 << 20      # compared at a time
SECTOR = 4096        # written at a time, when it differs
EMBED_POLL = 1.0     # seconds between asking whether an embed is done
EMBED_TIMEOUT = 1800 # a long book on a slow disk; ffmpeg only copies streams


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


def _blocks(src, size=BLOCK):
    src.seek(0)
    while True:
        chunk = src.read(size)
        if not chunk:
            return
        yield chunk


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


def _diff_write(dst, src):
    """Make `dst` equal to `src`, writing only the sectors that differ."""
    written = 0
    pos = 0
    with open(dst, "r+b") as out:
        for block in _blocks(src):
            out.seek(pos)
            written += _put_changed(out, pos, block, out.read(len(block)))
            pos += len(block)
        out.seek(0, 2)
        if out.tell() != pos:
            out.truncate(pos)
    return written


class EmbedRefused(Exception):
    """Audiobookshelf would not embed: the key is not an admin's, or the
    embed failed. The book is copied as it is, and the report says why."""


def _embed_on_server(client, item_id, step):
    """Run Audiobookshelf's Embed Metadata on the book and wait for it.

    It answers at once and works as a task; a finished task drops off the
    list, so done is when the book is neither queued nor running."""
    try:
        client.embed_metadata(item_id)
    except AbsError as e:
        if e.status in (401, 403):
            raise EmbedRefused("embedding tags needs an API key that belongs to an admin in Audiobookshelf")
        if e.status != 400:          # 400: already queued or running - wait for it
            raise EmbedRefused(f"Audiobookshelf would not embed: {e}")
    deadline = time.monotonic() + EMBED_TIMEOUT
    while True:
        step(0, 1, "Audiobookshelf is writing tags")
        if not client.embedding(item_id):
            return
        if time.monotonic() > deadline:
            raise EmbedRefused("Audiobookshelf took too long to write tags")
        time.sleep(EMBED_POLL)


def _layout(entry_name, files, root, rename, kind, have_names=None):
    """Where each server file goes on the player.

    `have_names` are the names the book's files already have on the player,
    in order. When there is one for each server file they are kept, so a
    book renamed on the player is written over, not copied a second time."""
    if kind == "file":
        f = files[0]
        fname = have_names[0] if have_names else f"{entry_name}{out_ext(f['filename'], rename)}"
        return [root / fname]
    target = root / entry_name
    if have_names and len(have_names) == len(files):
        return [target / n for n in have_names]
    out = []
    for i, f in enumerate(files, 1):
        ext = out_ext(f["filename"], rename)
        part = fit(clean(Path(f["relPath"]).stem), MAX_NAME - 6 - len(ext))
        out.append(target / f"{i:03d} - {part}{ext}")
    return out


def _names_there(root, have, rec):
    """The names a book's files have on the player now, in order: from its
    record, or else the audio files in its folder."""
    # From the player's own index, so only plain names: none that leads out.
    names = [n for n in (r.get("name") for r in rec.get("files") or [])
             if isinstance(n, str) and n not in ("", ".", "..") and "/" not in n and "\\" not in n]
    if rec.get("kind") == "file":
        return [have]
    folder = root / have
    if names and all((folder / n).is_file() for n in names):
        return names
    try:
        return sorted(p.name for p in folder.iterdir()
                      if p.is_file() and p.suffix.lower() in AUDIO_EXT)
    except OSError:
        return []


def _download_file(client, item_id, f, step_label, step, n, total):
    step(n - 1, total, step_label)
    with client.open_file(item_id, f["ino"]) as r, \
            tempfile.NamedTemporaryFile(delete=False) as fh:
        shutil.copyfileobj(r, fh)
        return Path(fh.name)


class StaleFiles(Exception):
    """A file of the book was a 404: Audiobookshelf's list of its files no
    longer matches the disk."""

    def __init__(self, error, embedded):
        super().__init__(str(error))
        self.error = error
        self.embedded = embedded


def _pull_files(client, item, full, opts, report, step, root, name, rename,
                embedded=False, rescanned=False):
    title = item.get("title") or name
    device_path = opts["devicePath"]
    force = bool(opts.get("force"))
    files = embed.audio_files(full)

    have = already_on_device(opts, item)
    rec = index_mod.load(device_path).get("entries", {}).get(have) if have else None
    kind_now = "file" if len(files) == 1 else "dir"
    if have and rec and rec.get("kind") == "dir" and kind_now == "file":
        # A one-file book in a folder of its own: how Audiobookshelf's
        # download hands over a book whose folder holds more than its audio,
        # so older versions of this tool copied it that way. It stays so.
        kind_now = "dir"
    if have and rec and rec.get("kind") != kind_now:
        report.fail(title, f"the book is now {len(files)} file(s) on the server but not on "
                           f"the player; remove it from the player and copy it again")
        return None

    # Tags: embedded on the server when its details differ from what was
    # embedded last time this book was copied. Its files then change on the
    # server, perhaps before Audiobookshelf notices, so each is fetched again.
    old = {r.get("name"): r for r in (rec or {}).get("files") or []}
    digest = embed.digest(full)
    had = {r.get("tags") for r in old.values()} if old else {None}
    refetch = False
    if embedded:
        refetch = True              # embedded before the rescan, not again
    elif config_mod.writes_tags(opts) and had != {digest}:
        try:
            _embed_on_server(client, item["id"], step)
        except (EmbedRefused, AbsError) as e:
            report.note(title, f"copied without tags - {e}")
            digest = None
        else:
            refetch = embedded = True
            full = _full_item(client, item) or full
            files = embed.audio_files(full)
    elif had != {digest}:
        digest = None               # not embedded, or embedded before a change

    if have:
        entry_name = have
    else:
        try:
            entry_name = free_name(name, item, taken_names(device_path, root))
        except ValueError as e:
            report.fail(title, str(e))
            return None
    kind = kind_now
    dsts = _layout(entry_name, files, root, rename, kind,
                   _names_there(root, have, rec or {"kind": kind}) if have else None)
    if kind == "file":
        entry_name = dsts[0].name

    out_files = []
    src_ext = ""
    changed = False
    tmp = None
    try:
        for n, (f, dst) in enumerate(zip(files, dsts), 1):
            src_ext = src_ext or Path(f["filename"]).suffix.lower()
            r = old.get(dst.name)
            same_src = bool(r) and r.get("ino") == f["ino"] and r.get("srcSize") == f["size"] \
                and r.get("srcMtime") == f["mtimeMs"]
            here = dst.is_file()
            intact = here and r is not None and dst.stat().st_size == r.get("size")
            # A book this tool copied before it kept per-file records: same
            # size as the server's file means it is the server's file.
            legacy = here and r is not None and "ino" not in r and dst.stat().st_size == f["size"]
            record = {"name": dst.name, "ino": f["ino"], "srcSize": f["size"],
                      "srcMtime": f["mtimeMs"], "tags": digest}

            if not force and not refetch and ((same_src and intact) or legacy):
                record["size"] = dst.stat().st_size
                out_files.append(record)
                continue

            tmp = _download_file(client, item["id"], f, dst.name, step, n, len(files))
            # Gone while it downloaded: stop, rather than make the player's
            # folders again on this disk below.
            if not device_present(device_path):
                raise DeviceGone()
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "rb") as src:
                if here:
                    report["written"] += _diff_write(dst, src)
                    report["updated"] += 1
                else:
                    _write_whole(dst, lambda out: [out.write(b) for b in _blocks(src)])
                    report["written"] += tmp.stat().st_size
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
        if e.status == 404 and not rescanned:
            raise StaleFiles(e, embedded) from e
        report.fail(title, f"download failed - {e}" + (
            " (still, after Audiobookshelf rescanned the book: check its files in "
            "Audiobookshelf)" if e.status == 404 else ""))
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
        before = report["written"]
        report.book = item.get("id")
        try:
            pull_book(client, item, opts, report, emit)
            # What this book put on the player, so a forced copy can say how
            # many books it changed and not only how many it checked.
            report["wrote"][item.get("id")] = report["written"] - before
        except DeviceGone:
            report["interrupted"] = [i.get("id") for i in items[n - 1:]]
            break
        except Exception as e:                      # one bad book, not the batch
            report.fail(item.get("title", "?"), str(e))
        report.book = None
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
