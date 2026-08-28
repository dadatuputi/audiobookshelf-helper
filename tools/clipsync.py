#!/usr/bin/env python3
"""
clipsync - copy audiobooks from the library onto a SanDisk Sansa Clip / Fuze.

Why this exists: the Clip does NOT recognise the .m4b extension, but it does play
AAC/M4A - and .m4b IS an MP4/AAC container. So the file needs no transcoding at
all, just a different extension. This copies and renames in one step.

Runs anywhere the library and the player are both mounted (your Mac over SMB,
the server, whatever).

    # see what's there
    ./clipsync.py --list
    ./clipsync.py --list redwall

    # copy onto the player
    ./clipsync.py --to /Volumes/CLIP redwall
    ./clipsync.py --to /Volumes/CLIP "starship troopers" holes

    # preview without copying
    ./clipsync.py --to /Volumes/CLIP --dry-run redwall

Books land in <CLIP>/AUDIOBOOKS/ which SanDisk players treat specially
(resume + bookmarking on most models).
"""
import argparse, os, re, shutil, sys, unicodedata
from pathlib import Path

DEFAULT_LIB = "/Volumes/media/audio/audiobooks"      # SMB mount on macOS
FALLBACKS = ["/data/encrypted/media/audio/audiobooks",
             "/MEDIA/audio/audiobooks",
             os.path.expanduser("~/media/audio/audiobooks")]
AUD = {".m4b", ".mp3", ".m4a", ".wav", ".flac"}
PLAYABLE_RENAME = {".m4b": ".m4a"}        # same container, extension only


def find_library(explicit):
    for c in ([explicit] if explicit else []) + [DEFAULT_LIB] + FALLBACKS:
        if c and Path(c).is_dir():
            return Path(c)
    sys.exit("could not find the library - pass --lib /path/to/audiobooks")


def clean(s):
    """Filesystem-safe, ASCII-ish name - Clip firmware dislikes exotic chars."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    return re.sub(r"\s+", " ", s).strip()


def books(lib):
    """Yield (label, folder, [audio files]) for every book folder."""
    out = []
    for d, subdirs, files in os.walk(lib):
        p = Path(d)
        auds = sorted([p / f for f in files if Path(f).suffix.lower() in AUD])
        if not auds:
            continue
        rel = p.relative_to(lib)
        parts = rel.parts
        title = parts[-1]
        if re.match(r"^(cd|dis[ck])\s*\d+$", title, re.I):     # disc subfolder
            continue
        # keep the series folder in the label so "redwall" matches the whole series
        label = " - ".join(parts)
        out.append((label, p, auds))
    return sorted(out, key=lambda x: x[0].lower())


def gather(p, lib):
    """All audio for a book, including Disc N subfolders, in order."""
    files = sorted([q for q in p.rglob("*") if q.is_file() and q.suffix.lower() in AUD],
                   key=lambda q: (str(q.parent).lower(), q.name.lower()))
    return files


def human(n):
    for u in "B KB MB GB TB".split():
        if n < 1024: return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.0f}PB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("patterns", nargs="*", help="substring(s) to match book names")
    ap.add_argument("--lib")
    ap.add_argument("--to", help="mount point of the player, e.g. /Volumes/CLIP")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--flat", action="store_true",
                    help="single-file books go straight in AUDIOBOOKS/ with no subfolder")
    a = ap.parse_args()

    lib = find_library(a.lib)
    allb = books(lib)
    pats = [p.lower() for p in a.patterns]
    sel = [b for b in allb if not pats or any(p in b[0].lower() for p in pats)]

    if a.list or not a.to:
        print(f"library: {lib}   ({len(allb)} books)\n")
        for label, p, auds in sel:
            size = sum(f.stat().st_size for f in gather(p, lib))
            kinds = ",".join(sorted({f.suffix.lower().lstrip('.') for f in gather(p, lib)}))
            print(f"  {human(size):>7}  {len(gather(p,lib)):>3} file(s) [{kinds}]  {label}")
        if not a.to:
            print("\n(pass --to /Volumes/CLIP to copy)")
        return

    dest_root = Path(a.to)
    if not dest_root.is_dir():
        sys.exit(f"player not mounted at {dest_root}")
    ab = dest_root / "AUDIOBOOKS"

    total = sum(sum(f.stat().st_size for f in gather(p, lib)) for _, p, _ in sel)
    free = shutil.disk_usage(dest_root).free
    print(f"library : {lib}")
    print(f"player  : {dest_root}   free {human(free)}")
    print(f"selected: {len(sel)} book(s), {human(total)}\n")
    if total > free:
        sys.exit(f"not enough room: need {human(total)}, have {human(free)}")

    for label, p, _ in sel:
        files = gather(p, lib)
        name = clean(label)
        single = len(files) == 1
        target_dir = ab if (single and a.flat) else ab / name
        print(f"  {label}  ({len(files)} file(s))")
        for i, f in enumerate(files, 1):
            ext = f.suffix.lower()
            newext = PLAYABLE_RENAME.get(ext, ext)
            if single:
                out = target_dir / f"{name}{newext}"
            else:
                out = target_dir / f"{i:03d} - {clean(f.stem)}{newext}"
            note = "  (m4b->m4a)" if newext != ext else ""
            if a.dry_run:
                print(f"      -> {out.relative_to(dest_root)}{note}")
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists() and out.stat().st_size == f.stat().st_size:
                print(f"      = {out.name} (already there)")
                continue
            shutil.copy2(f, out)
            print(f"      + {out.name}{note}")
    if not a.dry_run:
        print("\ndone - eject the player before unplugging")


if __name__ == "__main__":
    main()
