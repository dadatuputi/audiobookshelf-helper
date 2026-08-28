#!/usr/bin/env python3
"""
Native messaging host for the Audiobookshelf Helper extension.

The extension cannot touch the filesystem outside the Downloads folder, so all
real work happens here: fetch each book (from a mounted share when available,
otherwise over HTTP) and write it to the player, renaming .m4b to .m4a.

Protocol: 4-byte little-endian length prefix + UTF-8 JSON, on stdin/stdout.
"""
import json, os, re, shutil, struct, sys, unicodedata, urllib.request, tempfile
from pathlib import Path

VERSION = "1.0.0"
AUD = {".m4b", ".m4a", ".mp3", ".flac", ".wav", ".ogg", ".opus"}
DISC = re.compile(r"^(cd|dis[ck])\s*\d{1,3}$", re.I)


# ---------------------------------------------------------------- transport
def read_msg():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    n = struct.unpack("<I", raw)[0]
    return json.loads(sys.stdin.buffer.read(n).decode("utf-8"))


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


def local_files(local_root, rel_path):
    """Audio files for a book on a mounted share, in stable order."""
    if not local_root or not rel_path:
        return []
    d = Path(local_root) / rel_path
    if not d.is_dir():
        return []
    fs = [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in AUD]
    return sorted(fs, key=lambda p: (str(p.parent).lower(), p.name.lower()))


def copy_book(item, opts, report):
    dev = Path(opts["devicePath"])
    root = dev / (opts.get("subdir") or "AUDIOBOOKS")
    name = clean(opts.get("folderTemplate", "{author} - {title}")
                 .replace("{author}", item.get("author") or "")
                 .replace("{title}", item.get("title") or "")
                 .replace("{series}", item.get("series") or "")).strip(" -")
    rename = bool(opts.get("renameM4b", True))
    mode = opts.get("sourceMode", "auto")

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
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            ct = (r.headers.get("Content-Type") or "").lower()
            cd = r.headers.get("Content-Disposition") or ""
            m = re.search(r'filename="?([^"]+)"?', cd)
            srcname = m.group(1) if m else (item["title"] + ".bin")
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                shutil.copyfileobj(r, tf)
                tmp = Path(tf.name)
    except Exception as e:
        report["errors"].append(f"{item['title']}: download failed - {e}")
        return

    if "zip" in ct or srcname.lower().endswith(".zip"):
        import zipfile
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(tmp) as z:
                members = [n for n in z.namelist() if Path(n).suffix.lower() in AUD]
                for i, n in enumerate(sorted(members), 1):
                    ext = out_ext(n, rename)
                    dst = target / f"{i:03d} - {clean(Path(n).stem)}{ext}"
                    if dst.exists():
                        report["skipped"] += 1
                        continue
                    with z.open(n) as s, open(dst, "wb") as o:
                        shutil.copyfileobj(s, o)
                    report["copied"] += 1
        finally:
            tmp.unlink(missing_ok=True)
    else:
        ext = out_ext(srcname, rename)
        dst = root / f"{name}{ext}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size == tmp.stat().st_size:
            report["skipped"] += 1
            tmp.unlink(missing_ok=True)
        else:
            shutil.move(str(tmp), dst)
            report["copied"] += 1


# ---------------------------------------------------------------- main
def handle(msg):
    cmd = msg.get("cmd")
    if cmd == "ping":
        return {"ok": True, "version": VERSION, "python": sys.version.split()[0]}
    if cmd != "sync":
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}

    dev = Path(msg.get("devicePath") or "")
    if not dev.is_dir():
        return {"ok": False, "error": f"device not mounted at {dev}"}

    report = {"copied": 0, "skipped": 0, "errors": []}
    for item in msg.get("items", []):
        try:
            copy_book(item, msg, report)
        except Exception as e:
            report["errors"].append(f"{item.get('title','?')}: {e}")
    try:
        report["freeAfter"] = human(shutil.disk_usage(dev).free)
    except Exception:
        pass
    report["ok"] = True
    return report


def main():
    while True:
        m = read_msg()
        if m is None:
            break
        try:
            write_msg(handle(m))
        except Exception as e:
            write_msg({"ok": False, "error": str(e)})


if __name__ == "__main__":
    main()
