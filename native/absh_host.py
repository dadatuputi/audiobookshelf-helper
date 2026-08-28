#!/usr/bin/env python3
"""
Native messaging host for the Audiobookshelf Helper extension.

The extension cannot touch the filesystem outside the Downloads folder, so all
real work happens here: fetch each book (from a mounted share when available,
otherwise over HTTP), write it to the player renaming .m4b to .m4a, report what
is already there, and delete books on request.

Protocol: 4-byte little-endian length prefix + UTF-8 JSON, on stdin/stdout.

Commands
    ping    -> {ok, version, python}
    list    -> which of the given books are already on the device, plus orphans
    sync    -> copy books; streams {event:"progress"} messages then {event:"done"}
    remove  -> delete named entries from the device

`sync` streams because a 320-book library takes long enough that a single
blocking reply is useless for a progress UI - and, on Chrome, long enough to
risk the MV3 service worker being torn down while it waits. The extension talks
to this host over a long-lived port (runtime.connectNative) rather than
sendNativeMessage, which only ever reads one reply.
"""
import json, re, shutil, struct, sys, unicodedata, tempfile
import urllib.parse, urllib.request
from pathlib import Path

VERSION = "1.0.0"
AUD = {".m4b", ".m4a", ".mp3", ".flac", ".wav", ".ogg", ".opus"}

# The host will fetch a book over HTTP, but only over HTTP. Without this, a
# caller could hand it file:// or ftp:// and use the host as a file-read oracle.
ALLOWED_SCHEMES = {"http", "https"}


# ---------------------------------------------------------------- transport
def _read_exactly(n):
    """Read exactly n bytes, or return None at a clean end of stream.

    A single .read(n) on a pipe is allowed to return short, which would
    desynchronise the length-prefixed framing for every message after it.
    """
    buf = b""
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_msg():
    raw = _read_exactly(4)
    if raw is None:
        return None
    n = struct.unpack("<I", raw)[0]
    body = _read_exactly(n)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def write_msg(obj):
    b = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(b)))
    sys.stdout.buffer.write(b)
    sys.stdout.buffer.flush()


# ---------------------------------------------------------------- helpers
def clean(s):
    """ASCII-ish, filesystem-safe. Clip firmware dislikes exotic characters."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    return re.sub(r"\s+", " ", s).strip() or "Untitled"


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.0f}PB"


def out_ext(p, rename_m4b):
    e = Path(p).suffix.lower()
    return ".m4a" if (rename_m4b and e == ".m4b") else e


def safe_subdir(s):
    """Reduce the configured subdir to a relative path that cannot escape.

    The subdir comes from extension settings, so it is user input rather than
    attacker input - but "../.." there would silently write outside the device
    root, which is worth making impossible rather than merely unlikely.
    """
    parts = []
    for raw in re.split(r"[\\/]+", str(s or "")):
        p = clean(raw) if raw.strip(". ") else ""
        if not raw or raw in (".", "..") or not p:
            continue
        parts.append(p)
    return Path(*parts) if parts else Path("AUDIOBOOKS")


def device_root(opts):
    """<devicePath>/<subdir>, with the subdir neutralised."""
    return Path(opts["devicePath"]) / safe_subdir(opts.get("subdir"))


def target_name(item, opts):
    """The on-device folder/file stem for a book, from the folder template.

    Naming lives here rather than in the extension so that sync, list and
    remove can never disagree about what a book is called on the device.
    """
    return clean(opts.get("folderTemplate", "{author} - {title}")
                 .replace("{author}", item.get("author") or "")
                 .replace("{title}", item.get("title") or "")
                 .replace("{series}", item.get("series") or "")).strip(" -") or "Untitled"


def _dir_stats(d):
    total, count = 0, 0
    for p in d.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            count += 1
    return total, count


def entry_for(root, name):
    """Describe what is on the device under `name`, or None if nothing is.

    A book lands either as one file (single-file book, extension varies) or as
    a folder of numbered parts, so both shapes have to be recognised.
    """
    d = root / name
    if d.is_dir():
        size, count = _dir_stats(d)
        return {"name": name, "kind": "dir", "bytes": size, "files": count}
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if p.is_file() and p.stem == name and p.suffix.lower() in AUD:
                return {"name": p.name, "kind": "file",
                        "bytes": p.stat().st_size, "files": 1}
    return None


def local_files(local_root, rel_path):
    """Audio files for a book on a mounted share, in stable order."""
    if not local_root or not rel_path:
        return []
    d = Path(local_root) / rel_path
    if not d.is_dir():
        return []
    fs = [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in AUD]
    # Sorting by parent directory first is what flattens "CD 1"/"CD 2" folders
    # into the right order - the numbering applied downstream then follows the
    # disc order rather than interleaving the discs.
    return sorted(fs, key=lambda p: (str(p.parent).lower(), p.name.lower()))


def copy_book(item, opts, report, emit=None):
    """Copy one book to the device. `emit` receives per-file progress."""
    root = device_root(opts)
    name = target_name(item, opts)
    rename = bool(opts.get("renameM4b", True))
    mode = opts.get("sourceMode", "auto")

    def step(done, total, label):
        if emit:
            emit({"event": "progress", "id": item.get("id"),
                  "title": item.get("title"), "file": label,
                  "done": done, "total": total})

    files = []
    if mode in ("auto", "local"):
        files = local_files(opts.get("localRoot"), item.get("relPath"))
    if not files and mode == "local":
        report["errors"].append(f"{item['title']}: not found on local share")
        return
    if files:
        single = len(files) == 1
        for i, f in enumerate(files, 1):
            ext = out_ext(f, rename)
            dst = (root / f"{name}{ext}") if single else (root / name / f"{i:03d} - {clean(f.stem)}{ext}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            step(i, len(files), dst.name)
            if dst.exists() and dst.stat().st_size == f.stat().st_size:
                report["skipped"] += 1
                continue
            shutil.copy2(f, dst)
            report["copied"] += 1
        return

    # HTTP fallback: ABS returns a single file, or a zip for multi-file books.
    url = item.get("url")
    if not url:
        report["errors"].append(f"{item['title']}: no download url")
        return
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        report["errors"].append(f"{item['title']}: refusing {scheme or 'schemeless'} url")
        return

    tmp = None
    try:
        step(0, 1, "downloading")
        with urllib.request.urlopen(url, timeout=120) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            cd = r.headers.get("Content-Disposition") or ""
            m = re.search(r'filename="?([^"]+)"?', cd)
            srcname = m.group(1) if m else (item["title"] + ".bin")
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                shutil.copyfileobj(r, tf)
                tmp = Path(tf.name)

        if "zip" in ct or srcname.lower().endswith(".zip"):
            import zipfile
            target = root / name
            target.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp) as z:
                members = [n for n in z.namelist() if Path(n).suffix.lower() in AUD]
                for i, n in enumerate(sorted(members), 1):
                    ext = out_ext(n, rename)
                    # Names are rebuilt from the index, never taken from the
                    # archive, so a crafted zip cannot escape the target dir.
                    dst = target / f"{i:03d} - {clean(Path(n).stem)}{ext}"
                    step(i, len(members), dst.name)
                    if dst.exists():
                        report["skipped"] += 1
                        continue
                    with z.open(n) as s, open(dst, "wb") as o:
                        shutil.copyfileobj(s, o)
                    report["copied"] += 1
        else:
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
    except Exception as e:
        report["errors"].append(f"{item['title']}: download failed - {e}")
    finally:
        # Covers the early-exception paths too, which previously leaked a
        # full-sized temp file per failed download.
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------- commands
def _require_device(msg):
    dev = Path(msg.get("devicePath") or "")
    if not dev.is_dir():
        raise ValueError(f"device not mounted at {dev}")
    return dev


def list_device(msg):
    """Report which of the supplied books are on the device, and what else is.

    The extension sends the books it knows about; matching happens here so the
    naming rules only exist in one place.
    """
    _require_device(msg)
    root = device_root(msg)
    on_device, seen = [], set()
    for item in msg.get("items", []):
        name = target_name(item, msg)
        e = entry_for(root, name)
        if e:
            seen.add(e["name"])
            on_device.append({**e, "id": item.get("id"), "title": item.get("title")})

    orphans = []
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if p.name in seen:
                continue
            if p.is_dir():
                size, count = _dir_stats(p)
                orphans.append({"name": p.name, "kind": "dir", "bytes": size, "files": count})
            elif p.suffix.lower() in AUD:
                orphans.append({"name": p.name, "kind": "file",
                                "bytes": p.stat().st_size, "files": 1})

    out = {"ok": True, "onDevice": on_device, "orphans": orphans}
    try:
        u = shutil.disk_usage(Path(msg["devicePath"]))
        out["free"] = human(u.free)
        out["freeBytes"] = u.free
    except OSError:
        pass
    return out


def remove_entries(msg):
    """Delete named entries from the device.

    This is the only destructive command, so every name is checked to be a
    single path component that resolves to a real child of the device root -
    no traversal, no symlink following, nothing outside the subdir.
    """
    _require_device(msg)
    root = device_root(msg)
    root_r = root.resolve()
    removed, freed, errors = [], 0, []

    for name in msg.get("names", []):
        name = str(name)
        if not name or name in (".", "..") or Path(name).name != name:
            errors.append(f"{name}: refusing a name that is not a single entry")
            continue
        p = root / name
        if p.is_symlink():
            errors.append(f"{name}: refusing a symlink")
            continue
        if not p.exists():
            errors.append(f"{name}: not on the device")
            continue
        try:
            resolved = p.resolve()
            if resolved.parent != root_r:
                errors.append(f"{name}: resolves outside the device folder")
                continue
            if p.is_dir():
                size, _ = _dir_stats(p)
                shutil.rmtree(p)
            else:
                size = p.stat().st_size
                p.unlink()
            freed += size
            removed.append(name)
        except OSError as e:
            errors.append(f"{name}: {e}")

    out = {"ok": True, "removed": removed, "freed": human(freed),
           "freedBytes": freed, "errors": errors}
    try:
        u = shutil.disk_usage(Path(msg["devicePath"]))
        out["free"] = human(u.free)
        out["freeBytes"] = u.free
    except OSError:
        pass
    return out


def sync(msg, emit=None):
    _require_device(msg)
    items = msg.get("items", [])
    report = {"copied": 0, "skipped": 0, "errors": []}
    for n, item in enumerate(items, 1):
        if emit:
            emit({"event": "item", "id": item.get("id"),
                  "title": item.get("title"), "index": n, "count": len(items)})
        try:
            copy_book(item, msg, report, emit)
        except Exception as e:
            report["errors"].append(f"{item.get('title','?')}: {e}")
    try:
        report["freeAfter"] = human(shutil.disk_usage(Path(msg["devicePath"])).free)
    except OSError:
        pass
    report["ok"] = True
    return report


# ---------------------------------------------------------------- main
def handle(msg, emit=None):
    cmd = msg.get("cmd")
    try:
        if cmd == "ping":
            return {"ok": True, "version": VERSION, "python": sys.version.split()[0]}
        if cmd == "list":
            return list_device(msg)
        if cmd == "remove":
            return remove_entries(msg)
        if cmd == "sync":
            return sync(msg, emit)
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def main():
    while True:
        try:
            m = read_msg()
        except (struct.error, ValueError, UnicodeDecodeError) as e:
            write_msg({"ok": False, "error": f"malformed message: {e}"})
            break
        if m is None:
            break

        # Tag every reply with the request id so a port carrying several
        # in-flight requests can tell the answers apart.
        rid = m.get("rid")

        def send(ev):
            write_msg({**ev, "rid": rid} if rid is not None else ev)

        # Opt-in: a caller that sends one message and reads one reply must not
        # find progress events queued ahead of its answer.
        emit = send if m.get("progress") else None

        try:
            out = handle(m, emit)
        except Exception as e:
            out = {"ok": False, "error": str(e)}
        send({**out, "event": "done"})


if __name__ == "__main__":
    main()
