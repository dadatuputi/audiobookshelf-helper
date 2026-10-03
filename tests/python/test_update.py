"""`absh update`, against a release feed that is served for real.

An updater is only worth having if a bad update cannot leave the helper
broken, and a forged one cannot be installed at all, so most of what is here
is the unhappy paths. Every one of them asserts the installation was left
alone, because "refused" means nothing if the files were already swapped.

- Provenance: no key pinned, no signature, a corrupted one, one by a key the
  install does not trust, a genuine one replayed onto another release, and an
  archive whose bytes are not the ones the manifest signed - with a digest
  GitHub would happily report for them.
- The archive: missing pieces, or entries that would write outside the install.
- The swap: a build that does not start, and one that starts and answers a
  ping but cannot do real work. Both must be rolled back.

The feed is a real HTTP server on localhost rather than a patched urlopen, so
the request, the JSON shape and the downloads are all exercised as written.
Every key is generated here and thrown away.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from absh import ed25519, signing  # noqa: E402
from absh import update as U  # noqa: E402

# Throwaway keys. TRUSTED is what a test install pins by default; OTHER is
# a perfectly good key that nobody pinned.
TRUSTED, OTHER, NEXT = os.urandom(32), os.urandom(32), os.urandom(32)


def pin_line(*secrets):
    return [signing.format_key(ed25519.public_key(s)) for s in secrets]


def build_native_zip(dest: Path, release: str, break_host=False, escape=False,
                     drop=None, break_devices=False, blind_devices=False):
    """The same archive tools/package.py produces, at a chosen version."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT / "native" / "absh_host.py", "absh_host.py")
        z.write(ROOT / "native" / "install.py", "install.py")
        z.write(ROOT / "extension" / "identity.json", "identity.json")
        z.write(ROOT / "extension" / "identity.py", "identity.py")
        for f in sorted((ROOT / "absh").glob("*.py")):
            arc = f"absh/{f.name}"
            if drop and arc == drop:
                continue
            if f.name == "version.py":
                z.writestr(arc, f'RELEASE = "{release}"\n'
                                'def release():\n    return RELEASE\n'
                                'def is_release():\n    return RELEASE != "dev"\n')
            elif f.name == "host.py" and break_host:
                # Importable but fatal on startup, which is how a bad release
                # would actually present: the browser sees only a disconnect.
                z.writestr(arc, 'raise SystemExit("this build is broken")\n')
            elif f.name == "devices.py" and break_devices:
                # Starts, imports, answers a ping - and fails the first time
                # it is asked to look at a volume.
                z.writestr(arc, f.read_text() + '\n\ndef candidates(*a, **k):\n'
                                '    raise OSError("this build cannot read a volume")\n')
            elif f.name == "devices.py" and blind_devices:
                # Answers ok and lists nothing, wherever it is pointed.
                z.writestr(arc, f.read_text() + '\n\ndef roots(system=None):\n'
                                '    return []\n')
            else:
                z.write(f, arc)
        if escape:
            z.writestr("../escaped.txt", "should never be written")
    return dest


def native_name(tag):
    return f"audiobookshelf-helper-native-{tag.lstrip('v')}.zip"


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


class Feed:
    """A GitHub-shaped release feed, served over localhost.

    `assets` is every file attached to the release, by name. GitHub's own
    digest is reported for each one, computed over the bytes actually served -
    which is exactly why it proves nothing about who published them.
    """

    def __init__(self, tag, assets, prerelease=True):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/download/"):
                    name = urllib.parse.unquote(self.path[len("/download/"):])
                    if name not in assets:
                        self.send_error(404)
                        return
                    body, ctype = assets[name], "application/octet-stream"
                else:
                    body = json.dumps({
                        "tag_name": tag, "prerelease": prerelease,
                        "assets": [{"name": n,
                                    "browser_download_url":
                                        f"http://{outer.host}/download/{urllib.parse.quote(n)}",
                                    "digest": f"sha256:{sha(b)}"}
                                   for n, b in assets.items()],
                    }).encode()
                    ctype = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def api(self):
        return f"http://{self.host}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class UpdateCase(unittest.TestCase):
    """A throwaway installation, and a feed offering it something newer."""

    def install(self, release="1.0.0-alpha.1", keys=(TRUSTED,)):
        root = Path(self.tmp) / "install"
        (root / "absh").mkdir(parents=True)
        shutil.copy2(ROOT / "native" / "absh_host.py", root / "absh_host.py")
        for f in (ROOT / "absh").glob("*.py"):
            shutil.copy2(f, root / "absh" / f.name)
        (root / "absh" / "version.py").write_text(
            f'RELEASE = "{release}"\n'
            'def release():\n    return RELEASE\n'
            'def is_release():\n    return RELEASE != "dev"\n')
        self.pin(root, *keys)
        return root

    def pin(self, root, *secrets):
        (root / "absh" / "release_keys.py").write_text(
            "KEYS = " + json.dumps(pin_line(*secrets)) + "\n")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="absh-upd-")
        self.root = self.install()
        self.feeds = []
        self._api = U.API

    def tearDown(self):
        for f in self.feeds:
            f.close()
        U.API = self._api
        shutil.rmtree(self.tmp, ignore_errors=True)

    def release(self, tag="v1.0.0-alpha.2", manifest_tag=None, **kw):
        """A release's assets as CI publishes them: archives and SHA256SUMS."""
        z = build_native_zip(Path(self.tmp) / f"{tag}.zip", tag.lstrip("v"), **kw)
        native = z.read_bytes()
        chrome = b"stand-in for the chrome archive"
        chrome_name = f"audiobookshelf-helper-chrome-{tag.lstrip('v')}.zip"
        manifest = signing.make_manifest(manifest_tag or tag, {
            native_name(tag): sha(native), chrome_name: sha(chrome)})
        return {native_name(tag): native, chrome_name: chrome,
                signing.MANIFEST_NAME: manifest}

    def sign(self, assets, *secrets):
        """...and the signature the maintainer attaches afterwards."""
        assets[signing.SIGNATURE_NAME] = signing.sign_manifest(
            secrets or (TRUSTED,), assets[signing.MANIFEST_NAME]).encode()
        return assets

    def publish(self, tag, assets):
        feed = Feed(tag, assets)
        self.feeds.append(feed)
        U.API = feed.api
        return feed

    def serve(self, tag="v1.0.0-alpha.2", **kw):
        """The ordinary case: a release, signed with the trusted key."""
        return self.publish(tag, self.sign(self.release(tag, **kw)))

    def assertUntouched(self):
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")

    def assertRefused(self, fragment, **kw):
        with self.assertRaises(U.UpdateError) as e:
            U.apply(root=self.root, **kw)
        self.assertIn(fragment, str(e.exception))
        self.assertUntouched()
        return str(e.exception)


class Reads(UpdateCase):
    def test_reads_the_version_of_the_copy_being_updated(self):
        # Not the version of whatever absh happens to be imported: the thing
        # under update is an installation on disk.
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")
        self.assertEqual(U.installed_release(Path(self.tmp) / "nothing"), "unknown")

    def test_finds_the_native_asset_its_digest_and_its_signature(self):
        self.serve("v1.0.0-alpha.2")
        rel = U.find_release()
        self.assertEqual(rel["tag"], "v1.0.0-alpha.2")
        self.assertTrue(rel["name"].startswith("audiobookshelf-helper-native-"))
        self.assertEqual(len(rel["digest"]), 64)
        self.assertTrue(rel["manifest_url"].endswith("/SHA256SUMS"))
        self.assertTrue(rel["signature_url"].endswith("/SHA256SUMS.sig"))

    def test_a_named_tag_is_fetched_by_name(self):
        self.serve("v1.0.0-alpha.1")
        self.assertEqual(U.find_release("v1.0.0-alpha.1")["tag"], "v1.0.0-alpha.1")

    def test_trusted_keys_come_from_the_installation_on_disk(self):
        self.pin(self.root, TRUSTED, NEXT)
        self.assertEqual(U.trusted_keys(self.root),
                         [ed25519.public_key(TRUSTED), ed25519.public_key(NEXT)])


# Whether "this directory is read-only" is a thing that can be arranged here,
# and why not when it isn't. Decided at import, because the decorator below is
# evaluated when the class body runs: os.geteuid does not exist off POSIX, and
# reaching for it there takes the whole module out of the run rather than one
# test.
NO_WRITE_TEST = (
    "chmod does not make a directory unwritable on this platform"
    if os.name != "posix"
    else "root can write anywhere" if os.geteuid() == 0
    else "")


class Refuses(UpdateCase):
    def test_a_checkout(self):
        (self.root / ".git").mkdir()
        self.assertIn("git checkout", U.refuse_reason(self.root))

    def test_a_copy_with_no_release_to_update_from(self):
        (self.root / "absh" / "version.py").write_text('RELEASE = "dev"\n')
        self.assertIn("dev", U.refuse_reason(self.root))

    @unittest.skipIf(NO_WRITE_TEST, NO_WRITE_TEST)
    def test_an_install_it_cannot_write(self):
        self.root.chmod(0o555)
        try:
            self.assertIn("not writable", U.refuse_reason(self.root))
        finally:
            self.root.chmod(0o755)

    def test_an_archive_missing_what_it_needs(self):
        self.serve("v1.0.0-alpha.2", drop="absh/host.py")
        self.assertRefused("missing")

    def test_an_archive_that_would_write_outside_the_install(self):
        self.serve("v1.0.0-alpha.2", escape=True)
        self.assertRefused("escapes")
        self.assertFalse((Path(self.tmp) / "escaped.txt").exists())


class RefusesWithoutProvenance(UpdateCase):
    """Each of these is a release that anyone able to publish could make."""

    def test_a_copy_that_pins_no_key_says_what_to_do(self):
        self.pin(self.root)                     # KEYS = []
        self.serve("v1.0.0-alpha.2")
        why = U.refuse_reason(self.root)
        self.assertIn("pins no release-signing key", why)
        # The user is told the way round it, and the maintainer the fix.
        self.assertIn("install.py", why)
        self.assertIn("tools/sign_release.py keygen", why)
        self.assertIn("absh/release_keys.py", why)
        self.assertRefused("pins no release-signing key")

    def test_a_copy_whose_key_list_is_missing_pins_nothing(self):
        (self.root / "absh" / "release_keys.py").unlink()
        self.assertIn("pins no release-signing key", U.refuse_reason(self.root))

    def test_a_key_list_that_does_not_parse_is_not_read_as_empty(self):
        (self.root / "absh" / "release_keys.py").write_text('KEYS = ["ed25519:nonsense"]\n')
        self.assertIn("cannot read the signing keys", U.refuse_reason(self.root))

    def test_the_key_list_is_read_not_run(self):
        # The trust list is data. A file that would do something if imported
        # is read for its KEYS and nothing else.
        marker = Path(self.tmp) / "ran"
        (self.root / "absh" / "release_keys.py").write_text(
            f"open({str(marker)!r}, 'w').close()\nKEYS = {json.dumps(pin_line(TRUSTED))}\n")
        self.assertIsNone(U.refuse_reason(self.root))
        self.assertFalse(marker.exists())

    def test_an_unsigned_release(self):
        # Every release published before signing existed looks like this.
        self.publish("v1.0.0-alpha.2", self.release("v1.0.0-alpha.2"))
        why = self.assertRefused("is not signed")
        self.assertIn("by hand", why)

    def test_a_release_with_no_manifest_either(self):
        assets = self.release("v1.0.0-alpha.2")
        del assets[signing.MANIFEST_NAME]
        self.publish("v1.0.0-alpha.2", assets)
        self.assertRefused("is not signed")

    def test_a_corrupted_signature(self):
        assets = self.sign(self.release())
        line = assets[signing.SIGNATURE_NAME].decode().splitlines()
        kind, kid, sig = line[1].split()
        raw = bytearray(base64.b64decode(sig))
        raw[10] ^= 0x01
        line[1] = f"{kind} {kid} {base64.b64encode(bytes(raw)).decode()}"
        assets[signing.SIGNATURE_NAME] = ("\n".join(line) + "\n").encode()
        self.publish("v1.0.0-alpha.2", assets)
        self.assertRefused("has been altered")

    def test_a_manifest_changed_after_it_was_signed(self):
        assets = self.sign(self.release())
        assets[signing.MANIFEST_NAME] += (sha(b"x") + "  extra.zip\n").encode()
        self.publish("v1.0.0-alpha.2", assets)
        self.assertRefused("has been altered")

    def test_a_signature_by_a_key_this_copy_does_not_pin(self):
        self.publish("v1.0.0-alpha.2", self.sign(self.release(), OTHER))
        why = self.assertRefused("does not trust")
        self.assertIn(signing.key_id(ed25519.public_key(OTHER)), why)

    def test_a_signature_that_borrows_a_pinned_keys_id(self):
        # The id on the line only says which key to try; claiming the trusted
        # key's id with another key's signature is still a forgery.
        assets = self.sign(self.release(), OTHER)
        assets[signing.SIGNATURE_NAME] = assets[signing.SIGNATURE_NAME].replace(
            signing.key_id(ed25519.public_key(OTHER)).encode(),
            signing.key_id(ed25519.public_key(TRUSTED)).encode())
        self.publish("v1.0.0-alpha.2", assets)
        self.assertRefused("has been altered")

    def test_a_genuine_signature_replayed_onto_another_release(self):
        # Signed for real, for alpha.3 - and attached to a release calling
        # itself alpha.2. The signature verifies; the claim does not.
        assets = self.sign(self.release("v1.0.0-alpha.2", manifest_tag="v1.0.0-alpha.3"))
        self.publish("v1.0.0-alpha.2", assets)
        self.assertRefused("belongs to a different release")

    def test_an_archive_that_is_not_the_one_that_was_signed(self):
        # The manifest and signature are genuine; the archive beside them was
        # swapped afterwards. GitHub's digest matches the swapped bytes, as it
        # would for anyone who can upload an asset.
        assets = self.sign(self.release("v1.0.0-alpha.2"))
        other = build_native_zip(Path(self.tmp) / "other.zip", "9.9.9")
        assets[native_name("v1.0.0-alpha.2")] = other.read_bytes()
        self.publish("v1.0.0-alpha.2", assets)
        why = self.assertRefused("signed manifest")
        self.assertIn("digest", why)

    def test_a_manifest_that_does_not_list_the_archive(self):
        assets = self.release("v1.0.0-alpha.2")
        assets[signing.MANIFEST_NAME] = signing.make_manifest(
            "v1.0.0-alpha.2", {"audiobookshelf-helper-chrome-1.0.0-alpha.2.zip": sha(b"c")})
        self.publish("v1.0.0-alpha.2", self.sign(assets))
        self.assertRefused("does not list")

    def test_going_back_by_name_still_needs_a_signature(self):
        self.publish("v1.0.0-alpha.1", self.release("v1.0.0-alpha.1"))
        self.assertRefused("is not signed", tag="v1.0.0-alpha.1")


class Applies(UpdateCase):
    def test_swaps_the_installation_and_reports_it(self):
        self.serve("v1.0.0-alpha.2")
        steps = []
        out = U.apply(root=self.root, on_step=steps.append)
        self.assertTrue(out["updated"])
        self.assertEqual(out["current"], "1.0.0-alpha.1")
        self.assertEqual(out["latest"], "v1.0.0-alpha.2")
        # The installation on disk is the new one, and it still runs.
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.2")
        self.assertIsNone(U._self_check(self.root))
        self.assertTrue(any("signed by trusted key" in s for s in steps), steps)

    def test_says_so_rather_than_working_when_already_current(self):
        self.serve("v1.0.0-alpha.1")
        out = U.apply(root=self.root)
        self.assertFalse(out["updated"])
        self.assertIn("latest", out["reason"])

    def test_does_not_go_backwards_unless_asked_by_name(self):
        # An older release being presented as the latest - by mistake or by
        # someone who can mark releases - is not an update.
        shutil.rmtree(self.root)
        self.root = self.install("1.0.0-alpha.3")
        self.serve("v1.0.0-alpha.2")
        out = U.apply(root=self.root)
        self.assertFalse(out["updated"])
        self.assertIn("--tag", out["reason"])
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.3")

    def test_a_named_tag_installs_even_when_it_is_not_newer(self):
        # Going back to a known-good build is the point, and it is also the
        # only way to exercise any of this before a newer release exists.
        self.serve("v1.0.0-alpha.1")
        out = U.apply(tag="v1.0.0-alpha.1", root=self.root)
        self.assertTrue(out["updated"])

    def test_a_second_pinned_key_is_trusted_too(self):
        # Rotation, step two: a copy that pins both accepts the new key alone.
        self.pin(self.root, TRUSTED, NEXT)
        self.publish("v1.0.0-alpha.2", self.sign(self.release(), NEXT))
        self.assertTrue(U.apply(root=self.root)["updated"])

    def test_a_release_signed_by_old_and_new_keys_installs_on_either(self):
        # Rotation, step three: one release, two signatures, so a copy that
        # never learned the new key still verifies against the old one.
        self.publish("v1.0.0-alpha.2", self.sign(self.release(), NEXT, TRUSTED))
        self.assertTrue(U.apply(root=self.root)["updated"])

    def test_puts_the_old_version_back_when_the_new_one_will_not_start(self):
        """The failure this whole command has to survive."""
        self.serve("v1.0.0-alpha.2", break_host=True)
        with self.assertRaises(U.UpdateError) as e:
            U.apply(root=self.root)
        self.assertIn("put the previous version back", str(e.exception))
        # Restored, and provably working rather than merely present.
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")
        self.assertIsNone(U._self_check(self.root),
                          "the helper does not start after the rollback")

    def test_puts_the_old_version_back_when_the_new_one_answers_but_cannot_work(self):
        # Starts, imports, answers ping. A ping-only check would keep it.
        self.serve("v1.0.0-alpha.2", break_devices=True)
        with self.assertRaises(U.UpdateError) as e:
            U.apply(root=self.root)
        self.assertIn("cannot list devices", str(e.exception))
        self.assertIn("put the previous version back", str(e.exception))
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")
        self.assertIsNone(U._self_check(self.root))

    def test_puts_the_old_version_back_when_the_new_one_sees_nothing(self):
        self.serve("v1.0.0-alpha.2", blind_devices=True)
        with self.assertRaises(U.UpdateError) as e:
            U.apply(root=self.root)
        self.assertIn("did not see a test device", str(e.exception))
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")


class SelfCheck(UpdateCase):
    def test_does_not_depend_on_what_is_plugged_in_or_configured(self):
        # Whatever this machine has mounted or configured, the check looks
        # only at the stand-in it makes - so it passes in CI with no player,
        # and never reads a user's real device or settings.
        saved = {k: os.environ.get(k) for k in ("ABSH_DEVICE_ROOTS", "ABSH_CONFIG")}
        os.environ["ABSH_DEVICE_ROOTS"] = str(Path(self.tmp) / "nowhere")
        bad = Path(self.tmp) / "config.json"
        bad.write_text('{"subdir": "../../escape"}')
        os.environ["ABSH_CONFIG"] = str(bad)
        try:
            self.assertIsNone(U._self_check(self.root))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class Cli(UpdateCase):
    """`absh update --check`, run as a user would: from the installed copy."""

    def check(self):
        env = dict(os.environ, ABSH_UPDATE_API=U.API, NO_COLOR="1",
                   ABSH_CONFIG=str(Path(self.tmp) / "none.json"))
        return subprocess.run([sys.executable, "-m", "absh.cli", "update", "--check"],
                              cwd=str(self.root), env=env, capture_output=True,
                              text=True, timeout=60)

    def test_says_a_signed_release_is_good(self):
        self.serve("v1.0.0-alpha.2")
        p = self.check()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("signature   good", p.stdout)

    def test_says_an_unsigned_one_will_be_refused(self):
        self.publish("v1.0.0-alpha.2", self.release("v1.0.0-alpha.2"))
        p = self.check()
        self.assertEqual(p.returncode, 1)
        self.assertIn("not signed", p.stdout)
        self.assertNotIn("run `absh update`", p.stdout)


class Orders(unittest.TestCase):
    def test_release_strings_sort_as_releases(self):
        seq = ["1.0.0-alpha.1", "v1.0.0-alpha.2", "1.0.0-alpha.10", "1.0.0-beta.1",
               "1.0.0-rc.1", "1.0.0", "1.0.1-alpha.1", "1.0.1", "1.10.0"]
        keys = [U._order(v) for v in seq]
        self.assertEqual(keys, sorted(keys))
        self.assertIsNone(U._order("dev"))


if __name__ == "__main__":
    unittest.main()
