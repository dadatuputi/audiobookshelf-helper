#!/usr/bin/env python3
"""
Sign a published release with a key GitHub never holds.

    python3 tools/sign_release.py keygen                  # once, ever
    python3 tools/sign_release.py sign v1.0.0-alpha.4     # each release, after CI
    python3 tools/sign_release.py verify v1.0.0-alpha.4   # what `absh update` will see

`absh update` installs a release only if its SHA256SUMS is signed by a key the
installed copy pins in absh/release_keys.py. That is only worth anything if
publishing a release is not enough to produce the signature, so the key is
not an Actions secret - any workflow that runs can read those, and a malicious
one would sign its own release. CI builds and publishes; the maintainer signs
here, on their own machine, and uploads one small file.

Signing is the maintainer saying "this is the release", so `sign` checks
before it signs rather than stamping whatever CI uploaded:

  - SHA256SUMS names the tag being signed;
  - every archive on the release is listed in it, and matches its digest;
  - the native archive - the code `absh update` installs and runs with the
    user's rights - is the tagged source, file for file and byte for byte,
    with only version.py's release stamp different.

The extension archives are checked against their digests and not rebuilt:
they run inside the browser's sandbox and reach users through the stores or
by hand, not through `absh update`.

Nothing here needs GitHub credentials. Reading a public release does not, and
the upload is left to you as one printed `gh release upload` command.
"""
import argparse
import datetime
import getpass
import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from absh import ed25519, signing  # noqa: E402
from absh import update as update_mod  # noqa: E402


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RV = _load("relver", "release_version.py")
PK = _load("packager", "package.py")

DEFAULT_KEY = Path.home() / ".config" / "absh-release" / "signing.key"
KEY_HEADER = ("# audiobookshelf-helper release-signing key. PRIVATE: keep it "
              "offline, never commit it, never give it to CI.")
KEYS_FILE = "absh/release_keys.py"


class SignError(Exception):
    """Stop, with a sentence saying what is wrong."""


# ------------------------------------------------------------------ keys
def _git_tree_holding(path):
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


def keygen(out, label):
    """Write a new private key to `out`; return the line to pin."""
    out = Path(out).expanduser().resolve()
    tree = _git_tree_holding(out.parent)
    if tree is not None:
        raise SignError(
            f"{out} is inside the git working tree at {tree}. The private key "
            "must never be committable - put it somewhere else (the default "
            f"is {DEFAULT_KEY}).")
    if out.exists():
        raise SignError(f"{out} already exists; refusing to overwrite a key")
    secret = os.urandom(32)
    public = ed25519.public_key(secret)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Created 0600 rather than chmodded afterwards, so it is never readable by
    # anyone else even briefly. Windows ignores all but the read-only bit;
    # there the file inherits the folder's permissions.
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{KEY_HEADER}\n"
                f"private {signing.format_key(secret)}\n"
                f"public {signing.format_key(public)}\n")
    return f'    "{signing.format_key(public)}",  # {label}'


def load_key(path):
    """The 32-byte private key in a file keygen wrote."""
    path = Path(path).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise SignError(f"cannot read the signing key at {path}: {e}")
    fields = dict(ln.split(" ", 1) for ln in lines
                  if ln and not ln.startswith("#") and " " in ln)
    try:
        secret = signing.parse_key(fields.get("private", ""))
    except signing.SignatureError:
        raise SignError(f"{path} is not a key written by `sign_release.py keygen`")
    if "public" in fields:
        if signing.parse_key(fields["public"]) != ed25519.public_key(secret):
            raise SignError(f"{path}: the public line does not match the private key")
    return secret


# ------------------------------------------------------------ artefacts
class Published:
    """A release as GitHub serves it to anyone: no credentials involved."""

    def __init__(self, tag):
        try:
            rel = update_mod._get(
                f"{update_mod.API}/repos/{update_mod.REPO}/releases/tags/{tag}")
        except Exception as e:
            raise SignError(f"could not read release {tag}: {e}")
        self.urls = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}

    def zips(self):
        return sorted(n for n in self.urls if n.endswith(".zip"))

    def read(self, name):
        if name not in self.urls:
            raise SignError(f"the release has no {name} - has CI finished publishing it?")
        try:
            return update_mod._get(self.urls[name], binary=True, timeout=120)
        except Exception as e:
            raise SignError(f"could not download {name}: {e}")


class Folder:
    """The same artefacts from a directory, e.g. a downloaded release."""

    def __init__(self, path):
        self.path = Path(path)

    def zips(self):
        return sorted(p.name for p in self.path.glob("*.zip"))

    def read(self, name):
        try:
            return (self.path / name).read_bytes()
        except OSError as e:
            raise SignError(f"cannot read {name} from {self.path}: {e}")


class GitRef:
    """Files as they are at a tag in this repository, not as checked out."""

    def __init__(self, ref, repo=ROOT):
        self.ref, self.repo = ref, repo

    def _git(self, *args):
        try:
            p = subprocess.run(["git", "-C", str(self.repo), *args],
                               capture_output=True, check=True)
        except FileNotFoundError:
            raise SignError("git is needed to compare the release against its tag")
        except subprocess.CalledProcessError as e:
            raise SignError(
                f"git could not read {self.ref} ({e.stderr.decode(errors='replace').strip()}); "
                "is the tag fetched? `git fetch --tags`")
        return p.stdout

    def ls(self, directory):
        out = self._git("ls-tree", "--name-only", self.ref, directory.rstrip("/") + "/")
        return out.decode("utf-8").splitlines()

    def read(self, path):
        return self._git("show", f"{self.ref}:{path}")


def native_problems(blob, semver, source):
    """How a native archive differs from the source it claims to be; [] if not."""
    expected = {Path(f).name: source.read(f) for f in PK.NATIVE_FILES}
    for path in source.ls(PK.NATIVE_PACKAGE):
        if path.endswith(".py") and path.count("/") == 1:
            data = source.read(path)
            if path == f"{PK.NATIVE_PACKAGE}/version.py":
                data = PK.stamped_version(data.decode("utf-8"), semver).encode("utf-8")
            expected[path] = data
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            got = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
    except zipfile.BadZipFile:
        return ["it is not a zip archive"]
    problems = []
    for name in sorted(set(expected) | set(got)):
        if name not in got:
            problems.append(f"{name} is missing")
        elif name not in expected:
            problems.append(f"{name} is not in the tagged source")
        elif got[name] != expected[name]:
            problems.append(f"{name} differs from the tagged source")
    return problems


def sign_release(tag, secrets, artefacts, source):
    """Check the release thoroughly, then return (manifest, signature text)."""
    try:
        semver = RV.parse_tag(tag)["semver"]
    except ValueError as e:
        raise SignError(str(e))
    manifest = artefacts.read(signing.MANIFEST_NAME)
    try:
        listed_tag, digests = signing.parse_manifest(manifest)
    except signing.SignatureError as e:
        raise SignError(str(e))
    if listed_tag != tag:
        raise SignError(f"{signing.MANIFEST_NAME} is for {listed_tag}, not {tag}")

    unlisted = [n for n in artefacts.zips() if n not in digests]
    if unlisted:
        raise SignError(f"the release carries archives its manifest does not list: "
                        f"{', '.join(unlisted)}")
    blobs = {}
    for name, want in sorted(digests.items()):
        blob = artefacts.read(name)
        if hashlib.sha256(blob).hexdigest() != want:
            raise SignError(f"{name} does not match its line in {signing.MANIFEST_NAME}")
        blobs[name] = blob

    native = [n for n in digests if n.startswith(update_mod.ASSET_PREFIX)]
    if len(native) != 1:
        raise SignError(f"expected one {update_mod.ASSET_PREFIX}*.zip in the manifest, "
                        f"found {len(native)}")
    problems = native_problems(blobs[native[0]], semver, source)
    if problems:
        raise SignError(f"{native[0]} is not the source at {tag}: " + "; ".join(problems))

    return manifest, signing.sign_manifest(secrets, manifest)


# ------------------------------------------------------------------ cli
def cmd_keygen(a):
    try:
        who = getpass.getuser()
    except Exception:
        who = "maintainer"
    label = a.label or f"{who}, {datetime.date.today().isoformat()}"
    line = keygen(a.out, label)
    print(f"Private key written to {Path(a.out).expanduser()}\n"
          "  Back it up somewhere offline. It is the only thing that can ship a\n"
          "  helper update; lose it and every installed copy must be reinstalled\n"
          "  by hand. Never commit it and never store it as a GitHub secret.\n\n"
          f"Now pin the public key. Add this line inside KEYS in {KEYS_FILE}:\n\n"
          f"{line}\n\n"
          "then commit and push that before tagging the next release. Copies\n"
          "installed from that release onwards will accept updates signed with it.")
    return 0


def cmd_sign(a):
    key_paths = a.key or [DEFAULT_KEY]
    secrets = [load_key(p) for p in key_paths]
    artefacts = Folder(a.from_dir) if a.from_dir else Published(a.tag)
    source = GitRef(a.tag)
    manifest, sig = sign_release(a.tag, secrets, artefacts, source)

    publics = [ed25519.public_key(s) for s in secrets]
    pinned = signing.parse_pinned(source.read(KEYS_FILE).decode("utf-8"), f"{a.tag}:{KEYS_FILE}")
    for pub in publics:
        if pub not in pinned:
            print(f"warning: key {signing.key_id(pub)} is not pinned in {KEYS_FILE} at "
                  f"{a.tag}, so copies installed from {a.tag} will not accept the next "
                  "release signed with it", file=sys.stderr)
    # Proves the file about to be uploaded verifies, with the code that will
    # verify it, before anyone downloads it.
    signing.verify(manifest, sig.encode("utf-8"), publics)

    out = Path(a.out) if a.out else (
        Path(a.from_dir) if a.from_dir else ROOT / "release") / signing.SIGNATURE_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sig, encoding="utf-8")
    print(f"Signed {a.tag} with key {', '.join(signing.key_id(p) for p in publics)}: {out}\n\n"
          "Upload it (the asset name must stay SHA256SUMS.sig):\n\n"
          f"    gh release upload {a.tag} {out} --clobber\n\n"
          "then check it as an installed copy would:\n\n"
          f"    python3 tools/sign_release.py verify {a.tag}")
    return 0


def cmd_verify(a):
    rel = update_mod.find_release(a.tag)
    signed = update_mod.verify_release(rel, root=ROOT)
    blob = update_mod._get(rel["url"], binary=True, timeout=120)
    if hashlib.sha256(blob).hexdigest() != signed["sha256"]:
        raise SignError(f"{rel['name']} does not match the signed manifest")
    print(f"{a.tag}: signed by key {signed['key']}, which {KEYS_FILE} here pins; "
          f"{rel['name']} matches. `absh update` will accept it.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="make a signing key (once)")
    k.add_argument("--out", default=str(DEFAULT_KEY),
                   help=f"where to write the private key (default {DEFAULT_KEY})")
    k.add_argument("--label", help="who holds it, for the comment beside the pinned key")
    k.set_defaults(fn=cmd_keygen)

    s = sub.add_parser("sign", help="check a published release and sign its SHA256SUMS")
    s.add_argument("tag")
    s.add_argument("--key", action="append",
                   help=f"private key file; repeat to sign with two while rotating "
                        f"(default {DEFAULT_KEY})")
    s.add_argument("--from", dest="from_dir",
                   help="read the artefacts from this folder instead of GitHub")
    s.add_argument("--out", help="where to write SHA256SUMS.sig")
    s.set_defaults(fn=cmd_sign)

    v = sub.add_parser("verify", help="check a published release as `absh update` would")
    v.add_argument("tag")
    v.set_defaults(fn=cmd_verify)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (SignError, signing.SignatureError, update_mod.UpdateError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
