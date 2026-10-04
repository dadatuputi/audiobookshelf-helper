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

What makes it safe to run at all is the signature. A digest GitHub reports is
computed by GitHub over whatever it was given, so anyone able to publish a
release publishes a matching one; it proves the download arrived intact and
nothing about who made it. So a release is installed only if its SHA256SUMS
is signed by a key the *installed* copy already pins (absh/release_keys.py),
the manifest names the tag that was fetched, and the archive's bytes match the
manifest. The private key is the RELEASE_SIGNING_KEY Actions secret, and only
the release workflow signs with it - the maintainer's choice, so that releasing
needs no manual step. So a signature proves a release came out of this
repository's release workflow: an asset swapped in afterwards, or a build from
anywhere else, is refused. It does not hold against someone who can run
workflows here or controls the maintainer's account; they can sign too. There is deliberately no switch to skip that: installing an unsigned
release is downloading a zip and running install.py, and a flag that does the
same thing from here would only be something to talk a person into typing.
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

from . import signing
from . import version as version_mod

REPO = "dadatuputi/audiobookshelf-helper"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
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

    Without one, the newest release this copy should be offered, as
    releases.latest chooses it. Not GitHub's own "latest": that skips
    prereleases by definition, so while every release is one it answers 404.
    """
    if not tag:
        from . import releases       # here, not at the top: it imports this module
        return releases.latest(installed_release())
    try:
        rel = _get(API + f"/repos/{REPO}/releases/tags/{tag}")
    except Exception as e:                          # network, 404, bad JSON
        raise UpdateError(f"could not reach the release feed: {e}")

    assets = [a for a in rel.get("assets", [])
              if a.get("name", "").startswith(ASSET_PREFIX)
              and a["name"].endswith(".zip")]
    if not assets:
        raise UpdateError(
            f"release {rel.get('tag_name', '?')} has no {ASSET_PREFIX}*.zip to install")
    a = assets[0]
    by_name = {x.get("name"): x.get("browser_download_url")
               for x in rel.get("assets", [])}
    return {
        "tag": rel.get("tag_name", "?"),
        "name": a["name"],
        "url": a["browser_download_url"],
        # "sha256:abc..." when GitHub reports one; absent on older releases.
        # Informational only: see the module docstring for why it is not
        # what decides whether to install.
        "digest": (a.get("digest") or "").split(":")[-1] or None,
        "prerelease": bool(rel.get("prerelease")),
        # None on any release published before signing began, and on a new
        # one in the minutes between CI publishing it and the maintainer
        # attaching the signature.
        "manifest_url": by_name.get(signing.MANIFEST_NAME),
        "signature_url": by_name.get(signing.SIGNATURE_NAME),
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
    try:
        trusted_keys(root)
    except UpdateError as e:
        return str(e)
    return None


def trusted_keys(root=None):
    """The signing keys the copy at `root` pins, read before anything changes.

    From the installation's own absh/release_keys.py, the same way
    installed_release reads its version: what decides whether a release is
    genuine has to be the thing already on disk, never anything the release
    brings with it.
    """
    root = root or install_root()
    path = root / "absh" / "release_keys.py"
    try:
        keys = signing.read_pinned(path)
    except signing.SignatureError as e:
        raise UpdateError(
            f"cannot read the signing keys this copy trusts ({e}); reinstall it "
            f"from {RELEASES_PAGE}")
    if not keys:
        raise UpdateError(
            "this copy pins no release-signing key, so it cannot tell a genuine "
            "release from a forged one and will not install any. Download the "
            f"native zip from {RELEASES_PAGE} and run `python3 install.py` from it "
            "instead. (Maintainers: add the RELEASE_SIGNING_KEY secret and run the "
            "\"Pin release-signing key\" workflow, which pins its key in "
            "absh/release_keys.py.)")
    return keys


def verify_release(rel, root=None):
    """Check `rel`'s signature against the installed copy's keys.

    Returns {"sha256": the native archive's signed digest, "key": the id of the
    key that signed it}. Fetches only the manifest and its signature - a few
    hundred bytes - so `absh update --check` can say whether a release would
    be accepted without downloading the archive.
    """
    keys = trusted_keys(root)
    tag = rel["tag"]
    if not rel.get("manifest_url") or not rel.get("signature_url"):
        missing = (signing.SIGNATURE_NAME if rel.get("manifest_url")
                   else f"{signing.MANIFEST_NAME} or {signing.SIGNATURE_NAME}")
        raise UpdateError(
            f"release {tag} is not signed (it has no {missing}), so this copy will "
            "not install it. If it was published in the last few minutes, the "
            "signature may not be attached yet - try again later. Releases from "
            f"before signing began can only be installed by hand from {RELEASES_PAGE}.")
    try:
        manifest = _get(rel["manifest_url"], binary=True)
        signature = _get(rel["signature_url"], binary=True)
    except Exception as e:
        raise UpdateError(f"could not download release {tag}'s signature: {e}")

    try:
        kid = signing.verify(manifest, signature, keys)
        signed_tag, digests = signing.parse_manifest(manifest)
    except signing.SignatureError as e:
        raise UpdateError(
            f"release {tag} failed its signature check: {e}. It will not be "
            "installed. If you trust it anyway, install it by hand from "
            f"{RELEASES_PAGE} - this command will not.")
    if signed_tag != tag:
        # A genuine manifest from one release attached to another: the
        # signature is real, the claim it is making here is not.
        raise UpdateError(
            f"release {tag} carries a signed manifest for {signed_tag}; refusing "
            "a signature that belongs to a different release")
    if rel["name"] not in digests:
        raise UpdateError(
            f"the signed manifest for {tag} does not list {rel['name']}, so "
            "there is nothing signed to install")
    return {"sha256": digests[rel["name"]], "key": kid}


def _extract(zip_path, dest):
    """Unpack, refusing entries that would land outside dest.

    Our own archive is flat and harmless, but the threat this command cannot
    otherwise address is a release that is not ours, and writing outside the
    install directory is the cheapest thing such an archive would try.

    extractall drops the permission bits the archive recorded, so a script
    packed executable comes out not, and a launcher or manifest pointing at
    it can no longer start it. The execute bits are put back - only those:
    the archive does not get to make anything setuid or world-writable.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep):
                raise UpdateError(f"archive entry escapes the install: {info.filename}")
        z.extractall(dest)
        if os.name == "posix":
            for info in z.infolist():
                execs = (info.external_attr >> 16) & 0o111
                target = dest / info.filename
                if execs and not info.is_dir() and target.is_file():
                    target.chmod(target.stat().st_mode | execs)
    missing = [f for f in REQUIRED if not (dest / f).is_file()]
    if missing:
        raise UpdateError(f"archive is missing {', '.join(missing)}")
    return dest


def _frames(out):
    """Every length-prefixed JSON reply in a host's stdout, in order."""
    replies = []
    while len(out) >= 4:
        n = struct.unpack("<I", out[:4])[0]
        if len(out) < 4 + n:
            break
        replies.append(json.loads(out[4:4 + n].decode("utf-8")))
        out = out[4 + n:]
    return replies


def _self_check(root, timeout=30):
    """Start the new host and make it do real work, over its own protocol.

    A swap that leaves a helper which cannot start is worse than no update:
    the browser reports only that the port disconnected, and the UI that would
    explain it is the thing that stopped working. So the new code has to prove
    itself before the old code is discarded.

    Answering a ping proves it starts. Listing devices proves it can do the
    part that matters - dispatch a real command, load settings, and look at a
    volume - which a build that imports cleanly and then breaks on first use
    would not survive. It is pointed at a stand-in device made here, with a
    config path that does not exist, rather than at what is plugged in: the
    check must read nothing of the user's, must give the same answer whether
    or not a player is connected, and must not walk a large disk that happens
    to be mounted and time out on it, which would roll back a good build.
    """
    work = Path(tempfile.mkdtemp(prefix="absh-selfcheck-"))
    try:
        device = work / "PLAYER"
        (device / "AUDIOBOOKS" / "A Book").mkdir(parents=True)
        env = dict(os.environ,
                   ABSH_DEVICE_ROOTS=str(device),
                   ABSH_CONFIG=str(work / "no-config.json"))
        frames = b""
        for msg in ({"cmd": "ping"}, {"cmd": "devices", "subdir": "AUDIOBOOKS"}):
            body = json.dumps(msg).encode("utf-8")
            frames += struct.pack("<I", len(body)) + body
        try:
            p = subprocess.run(
                [sys.executable, str(root / "absh_host.py")],
                input=frames, capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return "the new helper did not answer"
        try:
            replies = _frames(p.stdout)
        except Exception as e:
            return f"the new helper's reply was unreadable: {e}"
        if not replies:
            return f"the new helper wrote nothing (exit {p.returncode})"
        if not replies[0].get("ok"):
            return f"the new helper refused a ping: {replies[0].get('error')}"
        if len(replies) < 2:
            return f"the new helper answered a ping and then stopped (exit {p.returncode})"
        listed = replies[1]
        if not listed.get("ok"):
            return f"the new helper cannot list devices: {listed.get('error')}"
        seen = [d for d in listed.get("devices") or []
                if isinstance(d, dict) and Path(str(d.get("path"))) == device]
        if not seen or not seen[0].get("hasSubdir"):
            return "the new helper did not see a test device it was pointed at"
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def apply(tag=None, root=None, on_step=lambda _m: None):
    """Download, verify, swap, and prove it starts. Returns a summary dict.

    Verification happens in full before the install is touched: the
    signature over the manifest by a key this copy already pins, the tag the
    manifest names, and the archive's bytes against the manifest. A failure
    at any of those leaves the installation exactly as it was.
    """
    root = root or install_root()
    why = refuse_reason(root)
    if why:
        raise UpdateError(why)

    from . import releases       # here, not at the top: it imports this module
    current = installed_release(root)
    rel = find_release(tag) if tag else releases.latest(current)
    order = releases.compare(rel["tag"], current)
    if not tag and (order == 0 or rel["tag"].lstrip("v") == current):
        return {"updated": False, "current": current, "latest": rel["tag"],
                "reason": "already on the latest release"}
    if not tag and order == -1:
        # Signed releases stay validly signed forever, so an old one being
        # presented as the latest is a downgrade, whoever arranged it. Going
        # back stays possible, but only when someone asks for it by name.
        return {"updated": False, "current": current, "latest": rel["tag"],
                "reason": f"newer than the latest release ({rel['tag']}); "
                          "name it with --tag to go back to it deliberately"}

    signed = verify_release(rel, root)
    on_step(f"signed by trusted key {signed['key']} for {rel['tag']}")

    on_step(f"downloading {rel['name']}")
    try:
        blob = _get(rel["url"], binary=True, timeout=120)
    except Exception as e:
        raise UpdateError(f"could not download {rel['name']}: {e}")

    got = hashlib.sha256(blob).hexdigest()
    if got != signed["sha256"]:
        raise UpdateError(
            f"downloaded bytes do not match the digest in the signed manifest "
            f"({got[:12]}… vs {signed['sha256'][:12]}…) - this is not the "
            "archive that was signed, so it will not be installed")
    on_step("archive matches the signed manifest")

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
        _put(new, root)

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


def _put(src, root):
    """Copy every file under src over the installation at root.

    A file being replaced keeps any execute bit it had: install.py marks the
    host script executable, and a release archived somewhere that records no
    mode must not quietly take that away.

    Then the bytecode cache goes, wherever a file landed. Python reuses a .pyc
    when the source's mtime, to the second, and size match what it recorded -
    and a release's version.py is the same size as the one it replaces. A swap
    inside that second would leave the old code running from the cache, and
    the self-check that follows the swap would be vouching for it rather than
    for what was installed - and after a rollback, the cache could still hold
    the build that failed. A hash-checked or unchecked .pyc is worse still: nothing about
    the new source would invalidate it. Deleting it costs one recompile.
    """
    touched = set()
    for rel_path in _files_in(src):
        dest = root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        keep = dest.stat().st_mode & 0o111 if dest.is_file() else 0
        shutil.copy2(src / rel_path, dest)
        if keep and os.name == "posix":
            dest.chmod(dest.stat().st_mode | keep)
        touched.add(dest.parent)
    for d in touched:
        shutil.rmtree(d / "__pycache__", ignore_errors=True)


def _restore(backup, root):
    _put(backup, root)
