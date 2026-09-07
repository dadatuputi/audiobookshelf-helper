"""Replace this installation with the latest release, deliberately.

This is `absh update`, run by a person, not a background check. The helper is
the privileged half of this tool - it runs outside the browser sandbox with
your rights and writes to your filesystem - so it does not quietly fetch and
execute new code on its own schedule. You ask; it tells you what it found;
it swaps and proves the result starts before keeping it.

What makes an in-place swap safe here is the shape of the install: the
browser's manifest points at a launcher, and that launcher execs an absolute
interpreter against a fixed absh_host.py path. Replacing the files under that
path leaves both untouched, so nothing has to be re-registered.

What this does NOT do is verify who published the release. The digest GitHub
reports is computed by GitHub from the bytes it was given, so it catches a
corrupted download and nothing else: whoever can publish a release publishes
its digest too. Signing with a key that does not live on GitHub is the fix,
and until that exists this is a convenience over downloading the zip by hand,
not a trust boundary.
"""
import hashlib
import json
import re
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import version as version_mod

REPO = "dadatuputi/audiobookshelf-helper"
API = os.environ.get("ABSH_UPDATE_API", "https://api.github.com")
ASSET_PREFIX = "audiobookshelf-helper-native-"

# What a usable archive has to contain. Checked before anything is replaced,
# so a release that packaged the wrong thing fails while the install is still
# whole.
REQUIRED = ("absh_host.py", "absh/__init__.py", "absh/host.py", "absh/version.py")


class UpdateError(Exception):
    """Anything that should stop the update with a sentence the user can act on."""


def _get(url, binary=False, timeout=30):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"absh/{version_mod.release()}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw if binary else json.loads(raw.decode("utf-8"))


def find_release(tag=None):
    """The release to install, and the native asset inside it.

    A tag can be named, which is how you go back to a known-good build - and
    the only way to exercise this at all before a newer release exists.
    """
    where = f"/repos/{REPO}/releases/tags/{tag}" if tag else f"/repos/{REPO}/releases/latest"
    try:
        rel = _get(API + where)
    except Exception as e:                          # network, 404, bad JSON
        raise UpdateError(f"could not reach the release feed: {e}")

    assets = [a for a in rel.get("assets", [])
              if a.get("name", "").startswith(ASSET_PREFIX)
              and a["name"].endswith(".zip")]
    if not assets:
        raise UpdateError(
            f"release {rel.get('tag_name', '?')} has no {ASSET_PREFIX}*.zip to install")
    a = assets[0]
    return {
        "tag": rel.get("tag_name", "?"),
        "name": a["name"],
        "url": a["browser_download_url"],
        # "sha256:abc..." when GitHub reports one; absent on older releases.
        "digest": (a.get("digest") or "").split(":")[-1] or None,
        "prerelease": bool(rel.get("prerelease")),
    }


def install_root():
    """The directory holding absh_host.py and the absh package."""
    return Path(__file__).resolve().parent.parent


def installed_release(root=None):
    """The release string of the copy at `root`, read from its own files.

    Read from disk rather than taken from this module's import, because the
    thing being updated is the installation, not whatever happens to be on
    sys.path. In normal use they are the same file; keeping them distinct is
    what lets an update be exercised against a copy that is not the one
    running the test.
    """
    root = root or install_root()
    try:
        text = (root / "absh" / "version.py").read_text()
    except OSError:
        return "unknown"
    m = re.search(r'^RELEASE\s*=\s*"([^"]*)"', text, re.M)
    return m.group(1) if m else "unknown"


def refuse_reason(root=None):
    """Why this copy must not be updated in place, or None if it may be.

    A checkout is the important one: `absh update` in a working tree would
    overwrite the source someone is editing with a release tarball, and the
    version stamp says "dev" precisely so that is detectable.
    """
    root = root or install_root()
    if (root / ".git").exists():
        return (f"{root} is a git checkout - update it with git, not this. "
                "This command is for an installed copy of a release.")
    if installed_release(root) in ("dev", "unknown"):
        return ("this copy reports itself as \"dev\", so there is no version to "
                "update from; install a release first")
    if not os.access(root, os.W_OK):
        return f"{root} is not writable by you"
    return None


def _extract(zip_path, dest):
    """Unpack, refusing entries that would land outside dest.

    Our own archive is flat and harmless, but the threat this command cannot
    otherwise address is a release that is not ours, and writing outside the
    install directory is the cheapest thing such an archive would try.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep):
                raise UpdateError(f"archive entry escapes the install: {info.filename}")
        z.extractall(dest)
    missing = [f for f in REQUIRED if not (dest / f).is_file()]
    if missing:
        raise UpdateError(f"archive is missing {', '.join(missing)}")
    return dest


def _self_check(root, timeout=30):
    """Start the new host and make it answer, over its own protocol.

    A swap that leaves a helper which cannot start is worse than no update:
    the browser reports only that the port disconnected, and the UI that would
    explain it is the thing that stopped working. So the new code has to prove
    itself before the old code is discarded.
    """
    msg = json.dumps({"cmd": "ping"}).encode("utf-8")
    try:
        p = subprocess.run(
            [sys.executable, str(root / "absh_host.py")],
            input=struct.pack("<I", len(msg)) + msg,
            capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "the new helper did not answer a ping"
    out = p.stdout
    if len(out) < 4:
        return f"the new helper wrote nothing (exit {p.returncode})"
    n = struct.unpack("<I", out[:4])[0]
    try:
        reply = json.loads(out[4:4 + n].decode("utf-8"))
    except Exception as e:
        return f"the new helper's reply was unreadable: {e}"
    if not reply.get("ok"):
        return f"the new helper refused a ping: {reply.get('error')}"
    return None


def apply(tag=None, root=None, on_step=lambda _m: None):
    """Download, verify, swap, and prove it starts. Returns a summary dict."""
    root = root or install_root()
    why = refuse_reason(root)
    if why:
        raise UpdateError(why)

    current = installed_release(root)
    rel = find_release(tag)
    if rel["tag"].lstrip("v") == current and not tag:
        return {"updated": False, "current": current, "latest": rel["tag"],
                "reason": "already on the latest release"}

    on_step(f"downloading {rel['name']}")
    try:
        blob = _get(rel["url"], binary=True, timeout=120)
    except Exception as e:
        raise UpdateError(f"could not download {rel['name']}: {e}")

    if rel["digest"]:
        got = hashlib.sha256(blob).hexdigest()
        if got != rel["digest"]:
            raise UpdateError(
                f"downloaded bytes do not match the published digest "
                f"({got[:12]}… vs {rel['digest'][:12]}…)")
        on_step("digest matches what the release publishes")

    work = Path(tempfile.mkdtemp(prefix="absh-update-"))
    backup = work / "backup"
    try:
        zip_path = work / rel["name"]
        zip_path.write_bytes(blob)
        new = _extract(zip_path, work / "new")

        # Keep what we are about to overwrite, so a helper that will not start
        # can be put back rather than left broken.
        backup.mkdir()
        for rel_path in _files_in(new):
            existing = root / rel_path
            if existing.is_file():
                (backup / rel_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(existing, backup / rel_path)

        on_step(f"replacing {current} with {rel['tag']}")
        for rel_path in _files_in(new):
            dest = root / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(new / rel_path, dest)

        problem = _self_check(root)
        if problem:
            _restore(backup, root)
            raise UpdateError(f"{problem} - put the previous version back")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return {"updated": True, "current": current, "latest": rel["tag"],
            "prerelease": rel["prerelease"]}


def _files_in(base):
    return sorted(p.relative_to(base) for p in base.rglob("*") if p.is_file())


def _restore(backup, root):
    for rel_path in _files_in(backup):
        dest = root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / rel_path, dest)
