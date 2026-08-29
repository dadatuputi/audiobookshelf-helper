"""Finding the player, so you do not have to type its path from memory.

There is no reliable, dependency-free way to ask "which of these is a USB audio
player", so this does not pretend to. It lists the removable-looking volumes the
OS has mounted, scores them on how much they look like a player, and lets you
pick. The score is a hint for ordering, never a decision: `absh devices` shows
the list and you choose.

Deliberately stdlib-only, like everything the native host can reach.
"""
import os
import shutil
import string
import sys
from pathlib import Path

from .naming import AUDIO_EXT, safe_subdir

# Most USB players are a few GB. Anything enormous is far more likely to be a
# backup disk than a player, so it sorts lower - but it is still listed, because
# someone will use a 256GB stick and be right to.
PLAYER_MAX_BYTES = 128 * 1024 ** 3


def _mac_roots(base="/Volumes"):
    vols = Path(base)
    if not vols.is_dir():
        return []
    out = []
    for p in sorted(vols.iterdir()):
        try:
            # The boot volume is mounted here too, as a symlink to /.
            if p.is_symlink() or p.resolve() == Path("/"):
                continue
            if p.is_dir():
                out.append(p)
        except OSError:
            continue
    return out


def _linux_roots():
    out = []
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    bases = [Path("/media"), Path("/run/media"), Path("/mnt")]
    if user:
        bases[:0] = [Path("/media") / user, Path("/run/media") / user]
    for base in bases:
        if not base.is_dir():
            continue
        try:
            for p in sorted(base.iterdir()):
                if p.is_dir() and not p.is_symlink() and p not in out:
                    out.append(p)
        except OSError:
            continue
    return out


def _windows_roots():
    out = []
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        DRIVE_REMOVABLE = 2
    except Exception:                       # pragma: no cover - not on Windows
        kernel32 = None
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        if kernel32 is not None:
            try:
                if kernel32.GetDriveTypeW(str(root)) != DRIVE_REMOVABLE:
                    continue
            except Exception:               # pragma: no cover
                pass
        out.append(root)
    return out


def roots(system=None):
    """Mounted volumes that could plausibly be a removable player."""
    system = system or sys.platform
    if system == "darwin":
        return _mac_roots()
    if system.startswith("win"):
        return _windows_roots()
    return _linux_roots()


def _audio_count(path, limit=40):
    """How many audio files sit under here - stop early, this is a hint."""
    n = 0
    try:
        for p in path.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXT:
                n += 1
                if n >= limit:
                    break
    except OSError:
        pass
    return n


def describe(path, subdir="AUDIOBOOKS"):
    """What we can tell about one candidate, and how player-like it looks."""
    path = Path(path)
    info = {"path": str(path), "name": path.name or str(path),
            "total": 0, "free": 0, "hasSubdir": False, "books": 0, "score": 0}
    try:
        u = shutil.disk_usage(str(path))
        info["total"], info["free"] = u.total, u.free
    except OSError:
        return info

    target = path / safe_subdir(subdir)
    info["hasSubdir"] = target.is_dir()
    if info["hasSubdir"]:
        info["books"] = sum(1 for _ in target.iterdir()) if target.is_dir() else 0

    score = 0
    if info["hasSubdir"]:
        score += 100                        # the strongest signal by far
    if (path / ".absh").is_dir():
        score += 60                         # we have synced to this one before
    if 0 < info["total"] <= PLAYER_MAX_BYTES:
        score += 20
    if _audio_count(path, limit=1):
        score += 10
    info["score"] = score
    return info


def candidates(subdir="AUDIOBOOKS", system=None):
    """Every plausible device, most player-like first."""
    found = [describe(p, subdir) for p in roots(system)]
    return sorted(found, key=lambda d: (-d["score"], d["name"].lower()))


def resolve(value, subdir="AUDIOBOOKS", system=None):
    """Turn what someone typed into a device path.

    An existing path is taken as-is. Otherwise it is treated as a volume name,
    so `--device PLAYER` works without anyone remembering whether this OS puts
    it under /Volumes, /media/you or /run/media/you.
    """
    if not value:
        return None
    p = Path(value).expanduser()
    if p.is_dir():
        return str(p)
    wanted = str(value).strip().strip("/\\").lower()
    for d in candidates(subdir, system):
        if Path(d["path"]).name.lower() == wanted:
            return d["path"]
    return None
