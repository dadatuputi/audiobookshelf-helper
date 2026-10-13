"""Finding the player, so you do not have to type its path from memory.

There is no reliable, dependency-free way to ask "which of these is a USB audio
player", so this does not pretend to. It lists the removable-looking volumes the
OS has mounted, scores them on how much they look like a player, and lets you
pick. The score is a hint for ordering, never a decision: `absh devices` shows
the list and you choose.

Set ABSH_DEVICE_ROOTS to search somewhere else entirely - see _env_roots.

Deliberately stdlib-only, like everything the native host can reach.
"""
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
from pathlib import Path

from .naming import AUDIO_EXT, safe_subdir

# Most USB players are a few GB. Anything enormous is far more likely to be a
# backup disk than a player, so it sorts lower - but it is still listed, because
# someone will use a 256GB stick and be right to.
PLAYER_MAX_BYTES = 128 * 1024 ** 3


# A player is never a network share, and a share is the slowest thing there
# is to look through: one under /Volumes held the whole device list up. These
# are left out by name, before anything inside them is touched.
NETWORK_FS = {
    "smbfs", "cifs", "smb3", "nfs", "nfs4", "afpfs", "webdav", "davfs", "ftp",
    "fuse.sshfs", "fuse.rclone", "fuse.davfs2", "fuse.glusterfs", "glusterfs",
    "ceph", "afs", "ncpfs", "9p",
}


def parse_mac_mounts(text):
    """{mount point: filesystem type} from the output of macOS `mount`.

    Each line reads `//user@nas/share on /Volumes/share (smbfs, nodev, ...)`.
    """
    out = {}
    for line in text.splitlines():
        head, sep, rest = line.rpartition(" (")
        if not sep or " on " not in head:
            continue
        point = head.split(" on ", 1)[1]
        out[point] = rest.split(",", 1)[0].rstrip(")").strip()
    return out


def parse_mountinfo(text):
    """{mount point: filesystem type} from Linux /proc/self/mountinfo."""
    out = {}
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        # Spaces and the like are written as octal escapes: \040 is a space.
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        out[point] = right.split()[0]
    return out


def network_mounts(system=None):
    """Mount points that are network shares. Empty if it cannot tell."""
    system = system or sys.platform
    try:
        if system == "darwin":
            text = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                                  timeout=3).stdout
            table = parse_mac_mounts(text)
        elif system.startswith("linux"):
            with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
                table = parse_mountinfo(f.read())
        else:
            return set()
    except (OSError, subprocess.SubprocessError):
        return set()
    return {point for point, fs in table.items() if fs.lower() in NETWORK_FS}


def _mac_roots(base="/Volumes", network=None):
    vols = Path(base)
    if not vols.is_dir():
        return []
    network = network_mounts("darwin") if network is None else network
    out = []
    for p in sorted(vols.iterdir()):
        if str(p) in network:
            continue
        try:
            # The boot volume is mounted here too, as a symlink to /.
            if p.is_symlink() or p.resolve() == Path("/"):
                continue
            if p.is_dir():
                out.append(p)
        except OSError:
            continue
    return out


def _linux_roots(network=None):
    network = network_mounts("linux") if network is None else network
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
                if str(p) in network:
                    continue
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
        # The type first: it does not touch the drive, and asking whether a
        # disconnected network drive exists can block for many seconds.
        if kernel32 is not None:
            try:
                if kernel32.GetDriveTypeW(str(root)) != DRIVE_REMOVABLE:
                    continue
            except Exception:               # pragma: no cover
                pass
        if not root.exists():
            continue
        out.append(root)
    return out


def _env_roots():
    """ABSH_DEVICE_ROOTS overrides the search entirely: it names the volumes
    themselves, not directories to scan for them.

    The per-OS lists below cover where desktops normally automount, which is
    not everywhere: a player mounted by hand at ~/mnt/player, or by an fstab
    entry somewhere idiosyncratic, is invisible to `absh devices` and to the
    extension's Detect button. Rather than guess wider and offer half the
    filesystem as candidates, let the person who knows say where to look.

    Separator is os.pathsep, like PATH: ":" everywhere but Windows.

    `absh update` also uses this to point a freshly installed build at a
    stand-in device before keeping it, so the name is part of what an older
    copy expects of a newer one: renaming it would fail every update's
    self-check and roll it back.
    """
    raw = os.environ.get("ABSH_DEVICE_ROOTS")
    if not raw:
        return None
    out = []
    for part in raw.split(os.pathsep):
        p = Path(part.strip()).expanduser()
        if part.strip() and p.is_dir() and p not in out:
            out.append(p)
    return out


def roots(system=None):
    """Mounted volumes that could plausibly be a removable player."""
    override = _env_roots()
    if override is not None:            # [] means "you named roots, none exist"
        return override
    system = system or sys.platform
    if system == "darwin":
        return _mac_roots()
    if system.startswith("win"):
        return _windows_roots()
    return _linux_roots()


# How much of a volume to look through for audio before giving up. A player
# shows audio near the top; a backup disk or a network share does not, and
# walking all of one took minutes and held up every other request.
WALK_ENTRIES = 2000
WALK_DEPTH = 3


def _audio_count(path, limit=40, entries=WALK_ENTRIES, depth=WALK_DEPTH):
    """How many audio files sit near the top of here. A hint, so it stops
    early: at `limit` found, or `entries` looked at, or `depth` folders down."""
    n = seen = 0
    stack = [(Path(path), 0)]
    while stack:
        d, level = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > entries:
                        return n
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if level + 1 < depth and not e.name.startswith("."):
                                stack.append((Path(e.path), level + 1))
                        elif Path(e.name).suffix.lower() in AUDIO_EXT:
                            n += 1
                            if n >= limit:
                                return n
                    except OSError:
                        continue
        except OSError:
            continue
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


# A volume that has not answered by then is listed as not answering, rather
# than holding up the whole list: a network share whose server has gone away
# can block a call into it for minutes.
DESCRIBE_SECONDS = 4.0


def candidates(subdir="AUDIOBOOKS", system=None, budget=DESCRIBE_SECONDS):
    """Every plausible device, most player-like first.

    Each volume is looked at on a thread of its own, and all of them get
    `budget` seconds in total. A slow one does not delay the rest, and one
    that has not answered comes back with "slow" set and nothing measured.
    """
    paths = roots(system)
    results = [None] * len(paths)

    def look(i, p):
        results[i] = describe(p, subdir)

    threads = [threading.Thread(target=look, args=(i, p), daemon=True,
                                name=f"absh-describe-{i}") for i, p in enumerate(paths)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + budget
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    found = [r if r is not None else
             {"path": str(p), "name": p.name or str(p), "total": 0, "free": 0,
              "hasSubdir": False, "books": 0, "score": -1, "slow": True}
             for p, r in zip(paths, results)]
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
