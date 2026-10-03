"""Updating the helper from the extension: `update-check` and `update`.

Driven the way the browser drives them - a real host process, spoken to over
its stdio framing - against a release feed served for real on localhost. The
installation being updated is a throwaway copy, because the one thing this
must never do is overwrite the checkout running the tests; that checkout is
used too, for the case where updating is refused because it is one.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from absh import releases as R  # noqa: E402
from absh import update as U  # noqa: E402
from absh import signing  # noqa: E402
from test_host_protocol import frame, unframe  # noqa: E402
from test_update import TRUSTED, build_native_zip, native_name, pin_line, sha  # noqa: E402


def release_assets(tmp, tag, archive=True, signed=True, **zip_kw):
    """What a release carries once CI has published it and the maintainer has
    signed it: the native archive, SHA256SUMS, and SHA256SUMS.sig by the key
    test installs pin. `archive=False` serves a stand-in archive, for a
    release that is only ever looked at, never installed."""
    blob = (build_native_zip(Path(tmp) / f"{tag}.zip", tag.lstrip("v"), **zip_kw).read_bytes()
            if archive else b"stand-in for a release nobody installs here")
    assets = {native_name(tag): blob}
    assets[signing.MANIFEST_NAME] = signing.make_manifest(tag, {native_name(tag): sha(blob)})
    if signed:
        assets[signing.SIGNATURE_NAME] = signing.sign_manifest(
            (TRUSTED,), assets[signing.MANIFEST_NAME]).encode()
    return assets


# ------------------------------------------------------------- the feed
class Feed:
    """GitHub's release endpoints, as much of them as the helper reads.

    `releases` is a list of (tag, prerelease, draft) or (tag, prerelease,
    draft, assets), assets mapping a file name to its bytes; with none, the
    release carries just a stand-in native archive. Every request is counted,
    so a test can say how many a check costs.
    """

    def __init__(self, releases, delay=0.0):
        self.requests = []
        outer = self
        releases = [r if len(r) == 4 else (*r, {native_name(r[0]): b"stand-in"})
                    for r in releases]
        files = {f"/dl/{tag}/{name}": body
                 for tag, _, _, assets in releases for name, body in assets.items()}

        def release(tag, pre, draft, assets):
            return {"tag_name": tag, "prerelease": pre, "draft": draft,
                    "assets": [{"name": n,
                                "browser_download_url": f"http://{outer.host}/dl/{tag}/{n}"}
                               for n in assets]}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                if delay:
                    time.sleep(delay)
                path = self.path.split("?")[0]
                prefix = f"/repos/{U.REPO}/releases"
                if path in files:
                    return self._send(files[path], "application/octet-stream")
                if path == prefix:
                    return self._json([release(*r) for r in releases])
                if path.startswith(prefix + "/tags/"):
                    tag = path[len(prefix + "/tags/"):]
                    for r in releases:
                        if r[0] == tag:
                            return self._json(release(*r))
                self.send_response(404)
                self.end_headers()

            def _json(self, obj):
                self._send(json.dumps(obj).encode(), "application/json")

            def _send(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def api(self):
        return f"http://{self.host}"

    def listings(self):
        return [p for p in self.requests if p.split("?")[0].endswith("/releases")]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def unreachable_api():
    """An address that refuses connections, found rather than assumed."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------- pure version logic
class Versions(unittest.TestCase):
    ORDER = ["0.9.0", "1.0.0-alpha.1", "1.0.0-alpha.3", "1.0.0-alpha.10",
             "1.0.0-alpha.beta", "1.0.0-beta.1", "1.0.0-rc.1", "1.0.0", "1.0.1",
             "1.10.0", "2.0.0"]

    def test_orders_prereleases_the_way_semver_does(self):
        for i, a in enumerate(self.ORDER):
            for j, b in enumerate(self.ORDER):
                want = (i > j) - (i < j)
                self.assertEqual(R.compare(a, b), want, f"{a} vs {b}")

    def test_a_leading_v_is_the_tag_not_the_version(self):
        self.assertEqual(R.compare("v1.0.0-alpha.2", "1.0.0-alpha.2"), 0)
        self.assertTrue(R.newer("v1.0.0-alpha.10", "1.0.0-alpha.9"))

    def test_build_metadata_does_not_order(self):
        self.assertEqual(R.compare("1.0.0+abc", "1.0.0+def"), 0)

    def test_dev_and_unknown_are_not_behind_anything(self):
        # A checkout is not older than a release, and offering it one as an
        # "update" would be a downgrade of the code someone is editing.
        for odd in ("dev", "unknown", "", None, "1.0", "1.0.0.1", "banana"):
            self.assertIsNone(R.compare("1.0.0", odd), odd)
            self.assertFalse(R.newer("9.9.9", odd), odd)

    def test_which_are_prereleases(self):
        self.assertTrue(R.is_prerelease("v1.0.0-alpha.1"))
        self.assertFalse(R.is_prerelease("v1.0.0"))
        self.assertFalse(R.is_prerelease("dev"))

    def test_a_tag_from_outside_cannot_reshape_the_url(self):
        self.assertTrue(R.valid_tag("v1.0.0-alpha.3"))
        for bad in ("", "../../etc", "v1/../x", "v1?x=1", "a b", 5, None, "v" * 80):
            self.assertFalse(R.valid_tag(bad), bad)


class ChoosingTheLatest(unittest.TestCase):
    def setUp(self):
        self._api = U.API
        self.feed = None

    def tearDown(self):
        U.API = self._api
        if self.feed:
            self.feed.close()

    def serve(self, releases):
        self.feed = Feed(releases)
        U.API = self.feed.api

    def test_by_version_not_by_the_order_they_were_listed(self):
        self.serve([("v1.0.0-alpha.9", True, False), ("v1.0.0-alpha.10", True, False),
                    ("v1.0.0-alpha.2", True, False)])
        self.assertEqual(R.latest("1.0.0-alpha.1")["tag"], "v1.0.0-alpha.10")

    def test_a_prerelease_install_is_offered_prereleases(self):
        # Every release published so far is one, and GitHub's own "latest"
        # endpoint answers 404 for that - the reason this does not use it.
        self.serve([("v1.0.0-alpha.3", True, False), ("v0.9.0", False, False)])
        rel = R.latest("1.0.0-alpha.1")
        self.assertEqual(rel["tag"], "v1.0.0-alpha.3")
        self.assertTrue(rel["prerelease"])

    def test_a_stable_install_is_offered_only_stable_releases(self):
        self.serve([("v1.1.0-beta.1", True, False), ("v1.0.1", False, False)])
        self.assertEqual(R.latest("1.0.0")["tag"], "v1.0.1")

    def test_a_stable_install_with_nothing_stable_to_go_to(self):
        self.serve([("v1.1.0-beta.1", True, False)])
        with self.assertRaises(U.UpdateError) as e:
            R.latest("1.0.0")
        self.assertIn("no stable release", str(e.exception))

    def test_drafts_are_not_releases(self):
        self.serve([("v1.0.0-alpha.5", True, True), ("v1.0.0-alpha.3", True, False)])
        self.assertEqual(R.latest("1.0.0-alpha.1")["tag"], "v1.0.0-alpha.3")

    def test_a_checkout_is_told_the_newest_of_anything(self):
        self.serve([("v1.0.0", False, False), ("v1.1.0-alpha.1", True, False)])
        self.assertEqual(R.latest("dev")["tag"], "v1.1.0-alpha.1")

    def test_an_unreachable_feed_is_an_update_error(self):
        U.API = unreachable_api()
        with self.assertRaises(U.UpdateError) as e:
            R.latest("1.0.0-alpha.1")
        self.assertIn("could not reach the release feed", str(e.exception))


# --------------------------------------------------------- over the wire
class HostCase(unittest.TestCase):
    """A throwaway installed copy, its own config, and a feed to talk to."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="absh-hostupd-"))
        self.root = self.install("1.0.0-alpha.1")
        self.feeds = []
        self.env = dict(os.environ, ABSH_CONFIG=str(self.tmp / "config.json"),
                        ABSH_UPDATE_API=unreachable_api())

    def tearDown(self):
        for f in self.feeds:
            f.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, release):
        root = self.tmp / "install"
        (root / "absh").mkdir(parents=True)
        shutil.copy2(ROOT / "native" / "absh_host.py", root / "absh_host.py")
        for f in (ROOT / "absh").glob("*.py"):
            shutil.copy2(f, root / "absh" / f.name)
        (root / "absh" / "version.py").write_text(
            f'RELEASE = "{release}"\n'
            'def release():\n    return RELEASE\n'
            'def is_release():\n    return RELEASE != "dev"\n')
        # Deliberately not back-dated. Written now, the old version.py and
        # the one unpacked over it in the same second share an mtime and a
        # size - all Python checks before reusing __pycache__ - so the swap
        # has to clear the cache for the next helper to report what it is.
        self.pin(root, TRUSTED)
        return root

    def pin(self, root, *secrets):
        (root / "absh" / "release_keys.py").write_text(
            "KEYS = " + json.dumps(pin_line(*secrets)) + "\n")

    def serve(self, releases, installable=(), unsigned=(), delay=0.0, **zip_kw):
        """Every release signed by the pinned key unless named in `unsigned`;
        only those in `installable` carry a real archive."""
        releases = [(*r[:3], release_assets(self.tmp, r[0], archive=r[0] in installable,
                                            signed=r[0] not in unsigned, **zip_kw))
                    for r in releases]
        feed = Feed(releases, delay=delay)
        self.feeds.append(feed)
        self.env["ABSH_UPDATE_API"] = feed.api
        return feed

    def host(self, msgs, script=None, timeout=120):
        """Send every message, close stdin, and read everything said back."""
        p = subprocess.run([sys.executable, str(script or self.root / "absh_host.py")],
                           input=frame(msgs), capture_output=True,
                           timeout=timeout, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[:800])
        return unframe(p.stdout)

    def done(self, replies, rid):
        return next(r for r in replies if r.get("rid") == rid and r.get("event") == "done")


class UpdateCheck(HostCase):
    def test_a_newer_release_is_reported(self):
        self.serve([("v1.0.0-alpha.2", True, False), ("v1.0.0-alpha.1", True, False)])
        r = self.done(self.host([{"cmd": "update-check", "rid": 1}]), 1)
        self.assertTrue(r["ok"])
        self.assertEqual(r["installed"], "1.0.0-alpha.1")
        self.assertEqual(r["latest"], "v1.0.0-alpha.2")
        self.assertTrue(r["newer"])
        self.assertTrue(r["prerelease"])
        self.assertIsNone(r["refused"])
        self.assertIsNone(r["releaseRefused"], "a signed release was not accepted")
        self.assertIsNone(r["checkError"])

    def test_a_newer_release_this_copy_would_refuse_says_why(self):
        # Published, not yet signed: the page must not offer a button that
        # the updater would then refuse.
        self.serve([("v1.0.0-alpha.2", True, False)], unsigned=["v1.0.0-alpha.2"])
        r = self.done(self.host([{"cmd": "update-check", "rid": 1}]), 1)
        self.assertTrue(r["newer"])
        self.assertIsNone(r["refused"])
        self.assertIn("is not signed", r["releaseRefused"])

    def test_a_copy_that_pins_no_key_says_so_word_for_word(self):
        """What every copy built before a key is pinned will show."""
        self.pin(self.root)                     # KEYS = []
        self.serve([("v1.0.0-alpha.2", True, False)])
        r = self.done(self.host([{"cmd": "update-check", "rid": 1},
                                 {"cmd": "ping", "rid": 2}]), 1)
        self.assertEqual(r["refused"], U.refuse_reason(self.root))
        self.assertIn("pins no release-signing key", r["refused"])
        self.assertTrue(r["newer"])
        self.assertIsNone(r["releaseRefused"], "checked a signature it could never accept")

    def test_already_current(self):
        self.serve([("v1.0.0-alpha.1", True, False)])
        r = self.done(self.host([{"cmd": "update-check", "rid": 1}]), 1)
        self.assertTrue(r["ok"])
        self.assertFalse(r["newer"])
        self.assertEqual(r["latest"], "v1.0.0-alpha.1")

    def test_a_checkout_says_why_it_cannot_be_updated(self):
        """The state every developer and this repo's own e2e suite is in."""
        self.serve([("v1.0.0-alpha.2", True, False)])
        r = self.done(self.host([{"cmd": "update-check", "rid": 1}],
                                script=ROOT / "native" / "absh_host.py"), 1)
        self.assertTrue(r["ok"])
        self.assertEqual(r["installed"], "dev")
        self.assertFalse(r["newer"], "a checkout is not behind a release")
        self.assertIn("git checkout", r["refused"])
        # Still says what the latest is: that much is true either way.
        self.assertEqual(r["latest"], "v1.0.0-alpha.2")

    def test_an_unreachable_feed_is_still_an_answer(self):
        # The page shows "couldn't check" and keeps working; the local half -
        # what is installed, whether it could be replaced - needs no network.
        r = self.done(self.host([{"cmd": "update-check", "rid": 1}]), 1)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["latest"])
        self.assertFalse(r["newer"])
        self.assertEqual(r["installed"], "1.0.0-alpha.1")
        self.assertIn("could not reach the release feed", r["checkError"])

    def test_a_check_reads_the_signature_but_downloads_no_archive(self):
        # The listing, the release, and SHA256SUMS with its signature: four
        # small requests, once a day at most.
        feed = self.serve([("v1.0.0-alpha.2", True, False)], installable=["v1.0.0-alpha.2"])
        self.host([{"cmd": "update-check", "rid": 1}])
        self.assertEqual(len(feed.requests), 4, feed.requests)
        self.assertFalse(any(p.endswith(".zip") for p in feed.requests), feed.requests)

    def test_nothing_newer_costs_two(self):
        feed = self.serve([("v1.0.0-alpha.1", True, False)])
        self.host([{"cmd": "update-check", "rid": 1}])
        self.assertEqual(len(feed.requests), 2, feed.requests)

    def test_a_slow_feed_does_not_hold_up_everything_else(self):
        """The page's device badges ride the same pipe as the check."""
        self.serve([("v1.0.0-alpha.2", True, False)], delay=2.0)
        replies = self.host([{"cmd": "update-check", "rid": 1}, {"cmd": "ping", "rid": 2}])
        done = [r["rid"] for r in replies if r.get("event") == "done"]
        self.assertEqual(done, [2, 1], "ping waited behind the release feed")
        # And the slow answer was still delivered before the helper exited.
        self.assertEqual(self.done(replies, 1)["latest"], "v1.0.0-alpha.2")

    def test_ping_says_whether_this_copy_can_update_itself(self):
        r = self.done(self.host([{"cmd": "ping", "rid": 1}]), 1)
        self.assertEqual(r["release"], "1.0.0-alpha.1")
        self.assertIsNone(r["updateRefused"])
        r = self.done(self.host([{"cmd": "ping", "rid": 1}],
                                script=ROOT / "native" / "absh_host.py"), 1)
        self.assertIn("git checkout", r["updateRefused"])


class Update(HostCase):
    def test_installs_steps_aside_and_the_next_helper_is_the_new_one(self):
        """The whole point: the extension's next connection runs new code.

        The reply is the old process's last word. It exits rather than answer
        the ping queued behind it, because its answer would be the old code
        speaking from memory; the fresh process spawned afterwards is the one
        that can truthfully say what is installed.
        """
        self.serve([("v1.0.0-alpha.2", True, False)], installable=["v1.0.0-alpha.2"])
        replies = self.host([{"cmd": "update", "rid": 1, "progress": True},
                             {"cmd": "ping", "rid": 2}])
        r = self.done(replies, 1)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["updated"])
        self.assertTrue(r["restarting"])
        self.assertEqual((r["from"], r["to"]), ("1.0.0-alpha.1", "v1.0.0-alpha.2"))
        steps = [e["message"] for e in replies if e.get("event") == "step"]
        self.assertTrue(any("downloading" in s for s in steps), steps)
        self.assertFalse(any(x.get("rid") == 2 for x in replies),
                         "the old process answered after replacing itself")

        again = self.done(self.host([{"cmd": "ping", "rid": 3}]), 3)
        self.assertEqual(again["release"], "1.0.0-alpha.2")

    def test_installs_the_release_the_page_showed(self):
        # Not whatever became latest between the check and the click.
        self.serve([("v1.0.0-alpha.3", True, False), ("v1.0.0-alpha.2", True, False)],
                   installable=["v1.0.0-alpha.2", "v1.0.0-alpha.3"])
        r = self.done(self.host([{"cmd": "update", "rid": 1, "tag": "v1.0.0-alpha.2"}]), 1)
        self.assertEqual(r["to"], "v1.0.0-alpha.2")
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.2")

    def test_never_goes_backwards_from_a_page(self):
        self.serve([("v1.0.0-alpha.1", True, False)], installable=["v1.0.0-alpha.1"])
        (self.root / "absh" / "version.py").write_text('RELEASE = "1.0.0-alpha.2"\n'
                                                       'def release():\n    return RELEASE\n')
        replies = self.host([{"cmd": "update", "rid": 1, "tag": "v1.0.0-alpha.1"},
                             {"cmd": "ping", "rid": 2}])
        r = self.done(replies, 1)
        self.assertTrue(r["ok"])
        self.assertFalse(r["updated"])
        self.assertFalse(r["restarting"])
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.2")
        # Nothing changed, so there is no reason to hang up.
        self.assertTrue(self.done(replies, 2)["ok"])

    def test_a_refusal_reaches_the_page_word_for_word(self):
        """Whatever the updater says is the sentence the user reads.

        A checkout's refusal is used because its exact text is knowable here;
        the same path carries every other UpdateError, including a release
        that cannot be verified.
        """
        script = ROOT / "native" / "absh_host.py"
        feed = self.serve([("v1.0.0-alpha.2", True, False)])
        replies = self.host([{"cmd": "update", "rid": 1}, {"cmd": "ping", "rid": 2}],
                            script=script)
        r = self.done(replies, 1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], U.refuse_reason(ROOT))
        self.assertNotIn("trace", r)
        self.assertEqual(feed.requests, [], "a refused copy asked the network anyway")
        self.assertTrue(self.done(replies, 2)["ok"], "a failed update hung up")

    def test_no_key_pinned_reaches_the_page_word_for_word(self):
        self.pin(self.root)
        feed = self.serve([("v1.0.0-alpha.2", True, False)], installable=["v1.0.0-alpha.2"])
        r = self.done(self.host([{"cmd": "update", "rid": 1, "tag": "v1.0.0-alpha.2"}]), 1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], U.refuse_reason(self.root))
        self.assertEqual(feed.requests, [])
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")

    def test_an_unsigned_release_is_refused_word_for_word(self):
        self.serve([("v1.0.0-alpha.2", True, False)], installable=["v1.0.0-alpha.2"],
                   unsigned=["v1.0.0-alpha.2"])
        r = self.done(self.host([{"cmd": "update", "rid": 1}]), 1)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"].startswith("release v1.0.0-alpha.2 is not signed"), r["error"])
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")

    def test_a_failed_swap_is_reported_verbatim_and_the_helper_keeps_going(self):
        self.serve([("v1.0.0-alpha.2", True, False)], installable=["v1.0.0-alpha.2"],
                   break_host=True)
        replies = self.host([{"cmd": "update", "rid": 1}, {"cmd": "ping", "rid": 2}])
        r = self.done(replies, 1)
        self.assertFalse(r["ok"])
        # The updater's own words, not a type name and a traceback.
        self.assertTrue(r["error"].endswith("put the previous version back"), r["error"])
        self.assertFalse(r["error"].startswith("UpdateError"))
        self.assertNotIn("trace", r)
        self.assertEqual(self.done(replies, 2)["release"], "1.0.0-alpha.1")
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")

    def test_an_unreachable_feed(self):
        r = self.done(self.host([{"cmd": "update", "rid": 1}]), 1)
        self.assertFalse(r["ok"])
        self.assertIn("could not reach the release feed", r["error"])

    def test_a_tag_that_is_not_a_tag(self):
        r = self.done(self.host([{"cmd": "update", "rid": 1, "tag": "../../x"}]), 1)
        self.assertFalse(r["ok"])
        self.assertIn("not a release tag", r["error"])


class CommandLine(HostCase):
    """`absh update` makes the same choice the button does."""

    def absh(self, *args):
        return subprocess.run([sys.executable, "-m", "absh.cli", "update", *args],
                              cwd=self.root, capture_output=True, text=True,
                              timeout=120, env=self.env)

    def test_check_finds_a_prerelease_github_calls_no_release_at_all(self):
        self.serve([("v1.0.0-alpha.2", True, False)])
        p = self.absh("--check")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("v1.0.0-alpha.2", p.stdout)
        self.assertIn("run `absh update`", p.stdout)
        self.assertIn("signature   good", p.stdout)

    def test_updates_forward(self):
        self.serve([("v1.0.0-alpha.2", True, False)], installable=["v1.0.0-alpha.2"])
        p = self.absh()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.2")

    def test_does_nothing_when_current(self):
        feed = self.serve([("v1.0.0-alpha.1", True, False)], installable=["v1.0.0-alpha.1"])
        p = self.absh()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("already on the latest", p.stdout)
        self.assertFalse(any("/dl/" in r for r in feed.requests))

    def test_a_named_tag_still_goes_back(self):
        self.serve([("v1.0.0-alpha.1", True, False)], installable=["v1.0.0-alpha.1"])
        (self.root / "absh" / "version.py").write_text(
            'RELEASE = "1.0.0-alpha.2"\ndef release():\n    return RELEASE\n')
        p = self.absh("--tag", "v1.0.0-alpha.1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(U.installed_release(self.root), "1.0.0-alpha.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
