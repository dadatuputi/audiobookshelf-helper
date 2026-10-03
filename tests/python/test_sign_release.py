"""tools/sign_release.py: the maintainer's half of release signing.

Two things matter. The private key must never end up anywhere it could be
committed, so keygen is tested for where it refuses to write. And `sign` must
be a check, not a rubber stamp: it signs only a release whose manifest names
the tag, whose archives match it, and whose native archive is the source - so
each of those is tested by handing it a release that is wrong in that way.

The last test is the whole chain with nothing stubbed: the real packager
builds the native archive, the real tool signs it after reading it over
HTTP, and the real updater installs it from the same feed.
"""
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from absh import ed25519, signing  # noqa: E402
from absh import update as U  # noqa: E402
from test_update import UpdateCase  # noqa: E402

_spec = importlib.util.spec_from_file_location("sign_release", ROOT / "tools" / "sign_release.py")
SR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SR)
TOOL = ROOT / "tools" / "sign_release.py"


class WorkingTree:
    """The source as checked out, standing in for `git show <tag>:...` so
    these tests do not depend on a tag existing or the tree being clean."""

    def ls(self, directory):
        return [f"{directory}/{p.name}" for p in sorted((ROOT / directory).iterdir())
                if p.is_file()]

    def read(self, path):
        return (ROOT / path).read_bytes()


class Keygen(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="absh-keygen-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args],
                              capture_output=True, text=True, timeout=60)

    def test_writes_a_private_key_and_says_exactly_where_the_public_one_goes(self):
        out = self.tmp / "keys" / "signing.key"
        p = self.run_tool("keygen", "--out", str(out), "--label", "test")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("absh/release_keys.py", p.stdout)
        line = next(ln for ln in p.stdout.splitlines() if '"ed25519:' in ln)
        # The printed line drops straight into KEYS and names the same key.
        pinned = signing.parse_pinned(f"KEYS = [\n{line}\n]\n")
        self.assertEqual(pinned, [ed25519.public_key(SR.load_key(out))])
        if os.name == "posix":
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_refuses_to_write_inside_a_git_working_tree(self):
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        out = self.tmp / "repo" / "deep" / "signing.key"
        p = self.run_tool("keygen", "--out", str(out))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("git working tree", p.stderr)
        self.assertFalse(out.exists())

    @unittest.skipUnless((ROOT / ".git").exists(),
                         "an unpacked source archive is not a git checkout")
    def test_refuses_this_repository_in_particular(self):
        out = ROOT / "release" / "signing.key"
        p = self.run_tool("keygen", "--out", str(out))
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(out.exists())

    def test_never_overwrites_a_key(self):
        out = self.tmp / "signing.key"
        self.assertEqual(self.run_tool("keygen", "--out", str(out)).returncode, 0)
        before = out.read_bytes()
        p = self.run_tool("keygen", "--out", str(out))
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(out.read_bytes(), before)


class Sign(unittest.TestCase):
    """A release in a folder, as CI leaves it, wrong in one way at a time."""

    TAG = "v1.0.0-alpha.9"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="absh-sign-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "release"
        self.native = SR.PK.build_native(self.dir, "1.0.0-alpha.9")
        (self.dir / "version.py").unlink()          # build_native's scratch copy
        self.chrome = self.dir / "audiobookshelf-helper-chrome-1.0.0-alpha.9.zip"
        self.chrome.write_bytes(b"stand-in")
        self.key = os.urandom(32)
        self.manifest()

    def manifest(self, tag=None):
        (self.dir / signing.MANIFEST_NAME).write_bytes(
            signing.manifest_for(tag or self.TAG, [self.native, self.chrome]))

    def sign(self):
        return SR.sign_release(self.TAG, [self.key], SR.Folder(self.dir), WorkingTree())

    def test_signs_a_release_that_is_what_it_says(self):
        manifest, sig = self.sign()
        self.assertEqual(manifest, (self.dir / signing.MANIFEST_NAME).read_bytes())
        self.assertEqual(signing.verify(manifest, sig.encode(), [ed25519.public_key(self.key)]),
                         signing.key_id(ed25519.public_key(self.key)))

    def test_refuses_a_manifest_for_another_tag(self):
        self.manifest("v1.0.0-alpha.8")
        with self.assertRaisesRegex(SR.SignError, "not v1.0.0-alpha.9"):
            self.sign()

    def test_refuses_an_archive_that_does_not_match_the_manifest(self):
        self.chrome.write_bytes(b"changed after the manifest was written")
        with self.assertRaisesRegex(SR.SignError, "does not match"):
            self.sign()

    def test_refuses_an_archive_the_manifest_does_not_list(self):
        (self.dir / "audiobookshelf-helper-extra-1.0.0-alpha.9.zip").write_bytes(b"x")
        with self.assertRaisesRegex(SR.SignError, "does not list"):
            self.sign()

    def rewrite_native(self, change):
        with zipfile.ZipFile(self.native) as z:
            files = {i.filename: z.read(i) for i in z.infolist()}
        change(files)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
        self.native.write_bytes(buf.getvalue())
        self.manifest()                          # a consistent, dishonest release

    def test_refuses_a_native_archive_that_is_not_the_source(self):
        # Exactly what a compromised workflow would publish: consistent
        # digests over code that is not what was tagged.
        self.rewrite_native(lambda f: f.update({"absh/host.py": f["absh/host.py"] + b"\n# x\n"}))
        with self.assertRaisesRegex(SR.SignError, "absh/host.py differs"):
            self.sign()

    def test_refuses_a_native_archive_with_something_added(self):
        self.rewrite_native(lambda f: f.update({"absh/sitecustomize.py": b"import os\n"}))
        with self.assertRaisesRegex(SR.SignError, "not in the tagged source"):
            self.sign()

    def test_refuses_a_native_archive_with_something_missing(self):
        self.rewrite_native(lambda f: f.pop("absh/signing.py"))
        with self.assertRaisesRegex(SR.SignError, "absh/signing.py is missing"):
            self.sign()

    def test_refuses_a_version_stamp_that_is_not_the_tag(self):
        self.rewrite_native(lambda f: f.update(
            {"absh/version.py": f["absh/version.py"].replace(b"alpha.9", b"alpha.7")}))
        with self.assertRaisesRegex(SR.SignError, "version.py differs"):
            self.sign()


@unittest.skipUnless((ROOT / ".git").exists(), "not a git checkout")
class GitSource(unittest.TestCase):
    def test_reads_files_as_committed(self):
        src = SR.GitRef("HEAD")
        self.assertIn("absh/update.py", src.ls("absh"))
        self.assertIn(b'RELEASE = "dev"', src.read("absh/version.py"))

    def test_a_missing_tag_says_to_fetch_tags(self):
        with self.assertRaisesRegex(SR.SignError, "git fetch --tags"):
            SR.GitRef("v0.0.0-no-such-tag").read("absh/update.py")


class EndToEnd(UpdateCase):
    def test_packaged_signed_over_http_and_installed(self):
        tag = "v1.0.0-alpha.9"
        out = Path(self.tmp) / "ci"
        native = SR.PK.build_native(out, "1.0.0-alpha.9")
        assets = {native.name: native.read_bytes()}
        assets[signing.MANIFEST_NAME] = signing.manifest_for(tag, [native])
        self.publish(tag, assets)

        # The maintainer's step, reading the published release over HTTP.
        manifest, sig = SR.sign_release(tag, [self.key], SR.Published(tag), WorkingTree())
        assets[signing.SIGNATURE_NAME] = sig.encode()   # `gh release upload`

        out = U.apply(root=self.root)
        self.assertTrue(out["updated"])
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.9")

    def test_verify_uses_the_keys_this_checkout_pins(self):
        # Until a key is pinned here, `verify` says so rather than passing.
        tag = "v1.0.0-alpha.9"
        self.serve(tag)
        if signing.read_pinned(ROOT / "absh" / "release_keys.py"):
            self.skipTest("a production key is pinned; the throwaway one is not it")
        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            self.assertEqual(SR.main(["verify", tag]), 1)
        finally:
            sys.stderr = real
        self.assertIn("pins no release-signing key", err.getvalue())

    def setUp(self):
        super().setUp()
        self.key = os.urandom(32)
        self.pin(self.root, self.key)


if __name__ == "__main__":
    unittest.main()
