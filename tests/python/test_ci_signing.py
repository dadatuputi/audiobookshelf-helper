"""Release signing done by CI, with the key held as a GitHub Actions secret.

The maintainer chose this so that releasing needs nothing from anyone's
machine. What it must still guarantee: a release is never published unsigned
or signed with a key installed helpers do not pin; the secret never reaches a
log; and a re-cut tag ends up with this build's signature, not none.

The workflow's own steps are run here as written (tests/python/workflow_steps
lifts them out of release.yml), against a copy of the tree and a stub `gh`
that records what it was asked to do.
"""
import base64
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from absh import ed25519, signing  # noqa: E402
import workflow_steps as W  # noqa: E402

_spec = importlib.util.spec_from_file_location("sign_release", ROOT / "tools" / "sign_release.py")
SR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SR)

TAG = "v1.0.0-alpha.9"
SEED = bytes(range(32))                       # a test key, never a real one
SECRET = "ed25519:" + base64.b64encode(SEED).decode()
PUBLIC = ed25519.public_key(SEED)
OTHER = ed25519.public_key(bytes(range(1, 33)))


def keys_file(dirpath, *publics):
    """A release_keys.py shaped like the real one, pinning `publics`."""
    real = (ROOT / "absh" / "release_keys.py").read_text()
    head = real[:real.index("KEYS = [")]
    body = "".join(f'    "{signing.format_key(k)}",\n' for k in publics)
    path = Path(dirpath) / "release_keys.py"
    path.write_text(head + "KEYS = [\n" + body + "]\n")
    return path


class Secret(unittest.TestCase):
    def test_takes_the_prefixed_form_and_bare_base64(self):
        self.assertEqual(SR.ci_secret({SR.SECRET_ENV: SECRET}), SEED)
        bare = base64.b64encode(SEED).decode()
        self.assertEqual(SR.ci_secret({SR.SECRET_ENV: f"  {bare}\n"}), SEED)

    def test_every_refusal_says_what_to_do_and_never_repeats_the_value(self):
        cases = {
            "": "openssl rand -base64 32",
            "not*base64!": "not base64",
            base64.b64encode(b"short").decode(): "32",
        }
        for value, hint in cases.items():
            with self.subTest(value=value):
                with self.assertRaises(SR.SignError) as e:
                    SR.ci_secret({SR.SECRET_ENV: value})
                self.assertIn(hint, str(e.exception))
                if value:
                    self.assertNotIn(value, str(e.exception))


class Pin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="absh-pin-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_adds_the_key_and_reads_it_back_through_the_updaters_parser(self):
        path = keys_file(self.tmp)
        before = path.read_text()
        self.assertTrue(SR.pin(PUBLIC, path))
        self.assertEqual(signing.read_pinned(path), [PUBLIC])
        after = path.read_text()
        # The explanation above KEYS is the maintainer's documentation; it stays.
        self.assertTrue(after.startswith(before[:before.index("KEYS = [")]))
        self.assertIn("GitHub Actions", after)

    def test_running_it_again_changes_nothing(self):
        path = keys_file(self.tmp)
        SR.pin(PUBLIC, path)
        once = path.read_text()
        self.assertFalse(SR.pin(PUBLIC, path))
        self.assertEqual(path.read_text(), once)

    def test_keeps_keys_already_pinned(self):
        path = keys_file(self.tmp, OTHER)
        SR.pin(PUBLIC, path)
        self.assertEqual(sorted(signing.read_pinned(path)), sorted([PUBLIC, OTHER]))


class CiSign(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="absh-cisign-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.manifest = self.tmp / "SHA256SUMS"
        self.manifest.write_bytes(signing.make_manifest(TAG, {"a.zip": "0" * 64}))
        self.out = self.tmp / "SHA256SUMS.sig"
        self._env = os.environ.get(SR.SECRET_ENV)
        os.environ[SR.SECRET_ENV] = SECRET
        self.addCleanup(self._restore)

    def _restore(self):
        if self._env is None:
            os.environ.pop(SR.SECRET_ENV, None)
        else:
            os.environ[SR.SECRET_ENV] = self._env

    def run_tool(self, *extra, pinned=(PUBLIC,), tag=TAG):
        kf = keys_file(self.tmp, *pinned)
        return SR.main(["ci-sign", "--tag", tag, "--manifest", str(self.manifest),
                        "--out", str(self.out), "--keys-file", str(kf), *extra])

    def test_signs_what_the_updater_will_accept(self):
        self.assertEqual(self.run_tool(), 0)
        kid = signing.verify(self.manifest.read_bytes(), self.out.read_bytes(), [PUBLIC])
        self.assertEqual(kid, signing.key_id(PUBLIC))

    def test_an_unpinned_key_signs_nothing_and_names_the_line_to_pin(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_tool(pinned=(OTHER,)), 1)
        self.assertFalse(self.out.exists())
        self.assertIn(signing.format_key(PUBLIC), err.getvalue())
        self.assertNotIn(base64.b64encode(SEED).decode(), err.getvalue())

    def test_a_manifest_for_another_tag_is_refused(self):
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_tool(tag="v9.9.9"), 1)
        self.assertFalse(self.out.exists())


@unittest.skipIf(W.BASH is None, "needs bash, as Actions has")
class Workflow(unittest.TestCase):
    """The package job's guard and sign steps, and github-release's re-cut path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="absh-wf-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ("absh", "tools"):
            shutil.copytree(ROOT / d, self.tmp / d,
                            ignore=shutil.ignore_patterns("__pycache__"))
        (self.tmp / "release").mkdir()
        (self.tmp / "release" / "SHA256SUMS").write_bytes(
            signing.make_manifest(TAG, {"a.zip": "0" * 64}))

    def pin_in_tree(self, *publics):
        shutil.copy(keys_file(self.tmp, *publics), self.tmp / "absh" / "release_keys.py")

    def guard(self, **env):
        return W.run_step("package", "Check the signing key before building anything",
                          env, self.tmp)

    def test_guard_stops_a_release_with_no_secret(self):
        self.pin_in_tree(PUBLIC)
        ran = self.guard(RELEASE_SIGNING_KEY="")
        self.assertNotEqual(ran.code, 0)
        self.assertIn("::error", ran.log)
        self.assertIn("not set", ran.log)

    def test_guard_stops_a_release_whose_key_is_not_pinned(self):
        self.pin_in_tree(OTHER)
        ran = self.guard(RELEASE_SIGNING_KEY=SECRET)
        self.assertNotEqual(ran.code, 0)
        self.assertIn(signing.format_key(PUBLIC), ran.log)
        self.assertNotIn(base64.b64encode(SEED).decode(), ran.log)

    def test_guard_lets_a_pinned_key_through(self):
        self.pin_in_tree(PUBLIC)
        self.assertEqual(self.guard(RELEASE_SIGNING_KEY=SECRET).code, 0)

    def test_the_sign_step_publishes_a_signature_installed_helpers_accept(self):
        self.pin_in_tree(PUBLIC)
        ran = W.run_step("package", "Sign the release",
                         {"RELEASE_SIGNING_KEY": SECRET, "TAG": TAG}, self.tmp)
        self.assertEqual(ran.code, 0, ran.log)
        sig = (self.tmp / "release" / "SHA256SUMS.sig").read_bytes()
        manifest = (self.tmp / "release" / "SHA256SUMS").read_bytes()
        self.assertEqual(signing.verify(manifest, sig, [PUBLIC]), signing.key_id(PUBLIC))
        self.assertNotIn(base64.b64encode(SEED).decode(), ran.log)

    def test_a_recut_tag_keeps_this_builds_signature(self):
        """Stale assets go first; this build's signature is uploaded after.

        Deleting stale signatures after the upload would delete the one this
        build just made, and publish a release no helper could install.
        """
        (self.tmp / "release" / "a.zip").write_bytes(b"zip")
        (self.tmp / "release" / "SHA256SUMS.sig").write_bytes(b"sig")
        calls = self.tmp / "gh-calls"
        stub = self.tmp / "bin"
        stub.mkdir()
        gh = stub / "gh"
        gh.write_text(f'''#!/usr/bin/env bash
echo "$*" >> "{calls}"
if [ "$1 $2" = "release view" ]; then
  case "$*" in *--json*) printf 'SHA256SUMS.sig\\nold-1.0.0.1.xpi\\n' ;; esac
  exit 0
fi
exit 0
''')
        gh.chmod(0o755)
        ran = W.run_step("github-release", "Publish the release",
                         {"GH_TOKEN": "x", "TAG": TAG, "PRERELEASE": "true"},
                         self.tmp, path_prepend=[stub])
        self.assertEqual(ran.code, 0, ran.log)
        log = calls.read_text().splitlines()
        deleted = [i for i, c in enumerate(log) if c.startswith("release delete-asset")]
        uploads = [i for i, c in enumerate(log)
                   if c.startswith("release upload") and "SHA256SUMS.sig" in c]
        self.assertTrue(deleted, log)
        self.assertEqual(len(uploads), 1, log)
        self.assertLess(max(deleted), uploads[0],
                        "the stale signature is removed after this build's is uploaded")


if __name__ == "__main__":
    unittest.main()
