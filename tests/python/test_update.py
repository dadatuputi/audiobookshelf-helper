"""`absh update`, against a release feed that is served for real.

An updater is only worth having if a bad update cannot leave the helper
broken, so most of what is here is the unhappy paths: a digest that does not
match, an archive missing pieces, an archive that would write outside the
install, and a release whose helper does not start. The last one matters most
- a swap that bricks the host takes the UI that would have explained it with
it, so the test asserts the previous version is put back.

The feed is a real HTTP server on localhost rather than a patched urlopen, so
the request, the JSON shape and the download are all exercised as written.
"""
import hashlib
import json
import os
import shutil
import sys
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from absh import update as U  # noqa: E402

# What a real native archive contains, per tools/package.py.
PACKAGE_FILES = ["absh_host.py", "install.py", "identity.json", "identity.py"]


def build_native_zip(dest: Path, release: str, break_host=False, escape=False,
                     drop=None):
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
            else:
                z.write(f, arc)
        if escape:
            z.writestr("../escaped.txt", "should never be written")
    return dest


class Feed:
    """A GitHub-shaped release feed, served over localhost."""

    def __init__(self, zip_path: Path, tag: str, digest=True, prerelease=True):
        blob = zip_path.read_bytes()
        sha = hashlib.sha256(blob).hexdigest()
        name = f"audiobookshelf-helper-native-{tag.lstrip('v')}.zip"
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.endswith(".zip"):
                    body, ctype = blob, "application/zip"
                else:
                    asset = {"name": name,
                             "browser_download_url": f"http://{outer.host}/{name}"}
                    if digest:
                        asset["digest"] = f"sha256:{sha}"
                    body = json.dumps({"tag_name": tag, "prerelease": prerelease,
                                       "assets": [asset]}).encode()
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


class UpdateCase(unittest.TestCase):
    """A throwaway installation, and a feed offering it something newer."""

    def install(self, release="1.0.0-alpha.1"):
        root = Path(self.tmp) / "install"
        (root / "absh").mkdir(parents=True)
        shutil.copy2(ROOT / "native" / "absh_host.py", root / "absh_host.py")
        for f in (ROOT / "absh").glob("*.py"):
            shutil.copy2(f, root / "absh" / f.name)
        (root / "absh" / "version.py").write_text(
            f'RELEASE = "{release}"\n'
            'def release():\n    return RELEASE\n'
            'def is_release():\n    return RELEASE != "dev"\n')
        return root

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="absh-upd-")
        self.root = self.install()
        self.feeds = []
        self._api = U.API

    def tearDown(self):
        for f in self.feeds:
            f.close()
        U.API = self._api
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, tag="v1.0.0-alpha.2", **kw):
        z = build_native_zip(Path(self.tmp) / f"{tag}.zip", tag.lstrip("v"),
                             **{k: v for k, v in kw.items()
                                if k in ("break_host", "escape", "drop")})
        feed = Feed(z, tag, digest=kw.get("digest", True))
        self.feeds.append(feed)
        U.API = feed.api
        return feed


class Reads(UpdateCase):
    def test_reads_the_version_of_the_copy_being_updated(self):
        # Not the version of whatever absh happens to be imported: the thing
        # under update is an installation on disk.
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")
        self.assertEqual(U.installed_release(Path(self.tmp) / "nothing"), "unknown")

    def test_finds_the_native_asset_and_its_digest(self):
        self.serve("v1.0.0-alpha.2")
        rel = U.find_release()
        self.assertEqual(rel["tag"], "v1.0.0-alpha.2")
        self.assertTrue(rel["name"].startswith("audiobookshelf-helper-native-"))
        self.assertEqual(len(rel["digest"]), 64)

    def test_a_named_tag_is_fetched_by_name(self):
        self.serve("v1.0.0-alpha.1")
        self.assertEqual(U.find_release("v1.0.0-alpha.1")["tag"], "v1.0.0-alpha.1")


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

    def test_bytes_that_do_not_match_the_published_digest(self):
        feed = self.serve("v1.0.0-alpha.2")
        # Serve a different archive than the digest describes.
        other = build_native_zip(Path(self.tmp) / "other.zip", "9.9.9")
        U.API = feed.api
        import absh.update as mod
        real = mod._get

        def swapped(url, binary=False, timeout=30):
            if binary:
                return other.read_bytes()
            return real(url, binary, timeout)
        mod._get = swapped
        try:
            with self.assertRaises(U.UpdateError) as e:
                U.apply(root=self.root)
            self.assertIn("digest", str(e.exception))
        finally:
            mod._get = real
        # And nothing was touched.
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")

    def test_an_archive_missing_what_it_needs(self):
        self.serve("v1.0.0-alpha.2", drop="absh/host.py")
        with self.assertRaises(U.UpdateError) as e:
            U.apply(root=self.root)
        self.assertIn("missing", str(e.exception))
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")

    def test_an_archive_that_would_write_outside_the_install(self):
        self.serve("v1.0.0-alpha.2", escape=True)
        with self.assertRaises(U.UpdateError) as e:
            U.apply(root=self.root)
        self.assertIn("escapes", str(e.exception))
        self.assertFalse((Path(self.tmp) / "escaped.txt").exists())
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")


class Applies(UpdateCase):
    def test_swaps_the_installation_and_reports_it(self):
        self.serve("v1.0.0-alpha.2")
        out = U.apply(root=self.root)
        self.assertTrue(out["updated"])
        self.assertEqual(out["current"], "1.0.0-alpha.1")
        self.assertEqual(out["latest"], "v1.0.0-alpha.2")
        # The installation on disk is the new one, and it still runs.
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.2")
        self.assertIsNone(U._self_check(self.root))

    def test_says_so_rather_than_working_when_already_current(self):
        self.serve("v1.0.0-alpha.1")
        out = U.apply(root=self.root)
        self.assertFalse(out["updated"])
        self.assertIn("latest", out["reason"])

    def test_a_named_tag_installs_even_when_it_is_not_newer(self):
        # Going back to a known-good build is the point, and it is also the
        # only way to exercise any of this before a newer release exists.
        self.serve("v1.0.0-alpha.1")
        out = U.apply(tag="v1.0.0-alpha.1", root=self.root)
        self.assertTrue(out["updated"])

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


if __name__ == "__main__":
    unittest.main()
