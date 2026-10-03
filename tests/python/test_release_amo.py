"""The amo job's branching, run as release.yml has it.

Signing goes three ways - nothing (no credentials), unlisted (a prerelease:
signed in the run, xpi attached) and listed (a stable tag: submitted, signed
by AMO after review) - and fails three ways: AMO already has the version,
unlisted signing hands back nothing, signing fails outright. Only the unlisted
way had ever run for real. Every branch is exercised here by executing the
job's own steps, lifted out of release.yml so the test cannot drift from it.

Two stand-ins for what the steps call:

  Stubs  `npx` and `gh` on PATH that act out each outcome. Cheap, and they
         reach every branch, but they behave the way the stub says.
  Real   web-ext itself, from node_modules, signing against a stand-in AMO on
         localhost. This is what found that the listed branch was unreachable:
         web-ext waits for a signed file on both channels, and a listed one is
         not signed until a human reviews it, so the step timed out after
         fifteen minutes instead of reporting the submission.

What neither can show is AMO's own behaviour; see tools/amo_versions.py.
"""
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))
import workflow_steps as W  # noqa: E402
import verify_release as V  # noqa: E402
from test_amo_versions import Fake as FakeVersions, page  # noqa: E402

JOB = "amo"
PREFLIGHT = "Check AMO does not already have this version"
SIGN = "Sign / submit"
ATTACH = "Attach the signed XPI to the release"

ALPHA = {"TAG": "v1.0.0-alpha.2", "SEMVER": "1.0.0-alpha.2", "FIREFOX": "1.0.0.2",
         "PRERELEASE": "true"}
STABLE = {"TAG": "v1.0.0", "SEMVER": "1.0.0", "FIREFOX": "1.0.0", "PRERELEASE": "false"}
CREDS = {"AMO_JWT_ISSUER": "user:1:2", "AMO_JWT_SECRET": "s3cret"}
NO_CREDS = {"AMO_JWT_ISSUER": "", "AMO_JWT_SECRET": ""}   # how Actions passes unset secrets
STUB_CONTROLS = ("STUB_LOG", "STUB_NPX", "STUB_XPI")

NPX = r"""#!/bin/sh
echo "npx $*" >> "$STUB_LOG"
dir=""; prev=""
for a in "$@"; do
  [ "$prev" = "--artifacts-dir" ] && dir="$a"
  prev="$a"
done
case "$STUB_NPX" in
  signed)
    mkdir -p "$dir"
    printf 'signed' > "$dir/$STUB_XPI"
    echo "Signed xpi downloaded: $dir/$STUB_XPI" ;;
  submitted)
    echo "Waiting for approval and download of signed XPI skipped." ;;
  conflict)
    # web-ext reports this on stderr; the step has to see it through tee.
    echo 'WebExtError: Submission failed (2): Conflict' >&2
    echo '{ "version": [ "Version 1.0.0.2 already exists." ] }' >&2
    exit 1 ;;
  broken)
    echo 'WebExtError: Validation failed' >&2
    exit 7 ;;
esac
"""

GH = r"""#!/bin/sh
echo "gh $*" >> "$STUB_LOG"
"""


class Job(unittest.TestCase):
    """A working directory shaped like the runner's checkout, and PATH stubs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "work"
        shutil.copytree(ROOT / "tools", self.work / "tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.work / "extension" / "dist" / "firefox").mkdir(parents=True)
        shutil.copy(ROOT / "extension" / "identity.json", self.work / "extension")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.log.write_text("")
        self.stub("python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub(self, name, text):
        p = self.bin / name
        p.write_text(text)
        p.chmod(0o755)

    def calls(self):
        return self.log.read_text().splitlines()

    def run_step(self, name, env, **controls):
        allow = STUB_CONTROLS + tuple(controls)
        return W.run_step(JOB, name, {"STUB_LOG": str(self.log), **env, **controls},
                          self.work, [self.bin], allow=allow)

    def sign(self, tag, creds, npx=None):
        return self.run_step(SIGN, {**creds, "PRERELEASE": tag["PRERELEASE"],
                                    "TAG": tag["TAG"]},
                             STUB_NPX=npx or "", STUB_XPI=f"cb73684229b84553a7b8-{tag['FIREFOX']}.xpi")

    def attach(self, tag, signed):
        return self.run_step(ATTACH, {"GH_TOKEN": "t", "TAG": tag["TAG"],
                                      "SEMVER": tag["SEMVER"],
                                      "ATTEMPTED": signed.get("attempted", ""),
                                      "CHANNEL": signed.get("channel", "")})


@unittest.skipUnless(W.CAN_RUN, "runs the workflow's bash; not on Windows")
class Stubbed(Job):
    def setUp(self):
        super().setUp()
        self.stub("npx", NPX)
        self.stub("gh", GH)

    def test_no_credentials(self):
        s = self.sign(ALPHA, NO_CREDS, "signed")
        self.assertEqual(s.code, 0, s.log)
        self.assertIn("::notice title=AMO::skipped", s.log)
        self.assertEqual(s.outputs, {"attempted": "false"})
        a = self.attach(ALPHA, s.outputs)
        self.assertEqual(a.code, 0, a.log)
        self.assertIn("no credentials, so nothing was signed", a.log)
        self.assertEqual(self.calls(), [], "called web-ext or gh with no credentials")

    def test_prerelease_signs_unlisted_and_attaches_under_the_release_name(self):
        s = self.sign(ALPHA, CREDS, "signed")
        self.assertEqual(s.code, 0, s.log)
        self.assertEqual(s.outputs, {"attempted": "true", "channel": "unlisted"})
        npx = self.calls()[0]
        self.assertIn("--channel unlisted", npx)
        self.assertNotIn("--approval-timeout", npx, "unlisted must wait for its xpi")
        a = self.attach(ALPHA, s.outputs)
        self.assertEqual(a.code, 0, a.log)
        dest = "release-xpi/audiobookshelf-helper-firefox-1.0.0-alpha.2.xpi"
        self.assertTrue((self.work / dest).is_file())
        self.assertEqual([p.name for p in (self.work / "release-xpi").iterdir()],
                         [Path(dest).name], "AMO's name survived the rename")
        self.assertEqual(self.calls()[-1], f"gh release upload v1.0.0-alpha.2 {dest} --clobber")

    def test_stable_submits_listed_without_waiting_and_attaches_nothing(self):
        s = self.sign(STABLE, CREDS, "submitted")
        self.assertEqual(s.code, 0, s.log)
        self.assertEqual(s.outputs, {"attempted": "true", "channel": "listed"})
        self.assertIn("--channel listed --approval-timeout 0", self.calls()[0])
        a = self.attach(STABLE, s.outputs)
        self.assertEqual(a.code, 0, a.log)
        self.assertIn("listed submission accepted; AMO signs it after review", a.log)
        self.assertFalse([c for c in self.calls() if c.startswith("gh ")])

    def test_unlisted_signing_that_returns_nothing_is_an_error(self):
        s = self.sign(ALPHA, CREDS, "submitted")
        self.assertEqual(s.code, 0, s.log)
        a = self.attach(ALPHA, s.outputs)
        self.assertEqual(a.code, 1, a.log)
        self.assertIn("::error title=AMO::signing was attempted on the unlisted channel "
                      "and produced no xpi", a.log)

    def test_a_version_amo_already_has_is_named_as_such(self):
        s = self.sign(ALPHA, CREDS, "conflict")
        self.assertEqual(s.code, 1, s.log)
        self.assertIn("::error title=AMO::AMO already has this version", s.log)
        self.assertIn("bump the tag", s.log)
        self.assertEqual(s.outputs, {"attempted": "true", "channel": "unlisted"},
                         "verify-release needs to know signing was attempted")

    def test_an_ordinary_failure_keeps_its_exit_code_through_tee(self):
        s = self.sign(ALPHA, CREDS, "broken")
        self.assertEqual(s.code, 7, s.log)
        self.assertNotIn("already has this version", s.log)
        self.assertIn("Validation failed", (self.work / "sign.log").read_text())

    def test_attach_after_signing_never_ran(self):
        """An earlier step failed (say the pre-flight found a conflict), so the
        sign step was skipped and left no outputs. It used to say "no
        credentials", which sent people to check secrets that were fine."""
        a = self.attach(ALPHA, {})
        self.assertEqual(a.code, 0, a.log)
        self.assertIn("signing did not run", a.log)
        self.assertNotIn("no credentials", a.log)

    def test_what_signing_reports_is_what_verify_release_expects(self):
        cases = [(ALPHA, NO_CREDS, "signed", None),
                 (ALPHA, CREDS, "signed", V.REQUIRED),
                 (STABLE, CREDS, "submitted", V.OPTIONAL)]
        for tag, creds, npx, need in cases:
            s = self.sign(tag, creds, npx)
            got = V.xpi_expectation("success", s.outputs.get("attempted", ""),
                                    s.outputs.get("channel", ""))
            self.assertEqual(got[0], need, (tag["TAG"], s.outputs))
            self.assertIsNone(got[3], f"verify-release calls {s.outputs} broken wiring")
            shutil.rmtree(self.work / "release-xpi", ignore_errors=True)


@unittest.skipUnless(W.CAN_RUN, "runs the workflow's bash; not on Windows")
class Preflight(Job):
    """The pre-flight step, running the real tools/amo_versions.py."""

    def setUp(self):
        super().setUp()
        self.amo = None

    def tearDown(self):
        if self.amo:
            self.amo.close()
            self.amo.httpd.server_close()
        super().tearDown()

    def preflight(self, creds, have):
        self.amo = FakeVersions([page(have)])
        return self.run_step(PREFLIGHT, {**creds, "FIREFOX_VERSION": "1.0.0.2",
                                         "TAG": "v1.0.0-alpha.2"},
                             AMO_API=self.amo.api + "/api/v5")

    def test_a_version_amo_has(self):
        r = self.preflight(CREDS, ["1.0.0.1", "1.0.0.2"])
        self.assertEqual(r.code, 1, r.log)
        self.assertIn("::error title=AMO::AMO already has version 1.0.0.2", r.log)
        self.assertIn("bump the tag", r.log)

    def test_a_version_amo_does_not_have(self):
        self.assertEqual(self.preflight(CREDS, ["1.0.0.1"]).code, 0)

    def test_no_credentials_lets_the_job_go_on(self):
        r = self.preflight(NO_CREDS, ["1.0.0.2"])
        self.assertEqual(r.code, 0, r.log)
        self.assertEqual(self.amo.seen_auth, [], "asked AMO without credentials")


class FakeAmo:
    """The parts of AMO's v5 signing API that web-ext 8 drives: upload,
    validation, version submission, approval status and file download."""

    def __init__(self, approve, conflict=False):
        outer = self
        self.seen = []
        self.channel = None

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, status, payload, kind="application/json"):
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.seen.append(f"POST {self.path}")
                if self.path.endswith("/addons/upload/"):
                    outer.channel = ("listed" if b'name="channel"\r\n\r\nlisted' in body
                                     else "unlisted")
                    self.send(201, {"uuid": "u1", "channel": outer.channel})
                else:
                    self.send(404, {"detail": "Not found."})

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                outer.seen.append(f"PUT {self.path}")
                if conflict:
                    self.send(409, {"version": ["Version 1.0.0.2 already exists."]})
                else:
                    self.send(201, {"guid": "x", "version": {
                        "id": 42, "edit_url": "https://addons.example/edit/42"}})

            def do_GET(self):
                outer.seen.append(f"GET {self.path}")
                path = urllib.parse.urlsplit(self.path).path
                if path.endswith("/addons/upload/u1/"):
                    self.send(200, {"uuid": "u1", "processed": True, "valid": True,
                                    "validation": {"errors": 0}})
                elif path.endswith("/versions/42/"):
                    status = "public" if approve else "unreviewed"
                    self.send(200, {"id": 42, "file": {
                        "status": status, "url": f"{outer.base}/files/cb73684229b84553a7b8-1.0.0.2.xpi"}})
                elif path.startswith("/files/"):
                    self.send(200, b"PK signed", "application/x-xpinstall")
                else:
                    self.send(404, {"detail": "Not found."})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


WEB_EXT = ROOT / "node_modules" / "web-ext" / "bin" / "web-ext.js"
NODE = shutil.which("node")
HAVE_WEB_EXT = bool(W.CAN_RUN and NODE and WEB_EXT.is_file())


# CI sets ABSH_REQUIRE_WEB_EXT in the one job that has node_modules, so these
# cannot quietly skip everywhere and be mistaken for passing.
@unittest.skipUnless(HAVE_WEB_EXT or os.environ.get("ABSH_REQUIRE_WEB_EXT"),
                     "needs bash, node and `npm ci` (web-ext in node_modules)")
class RealWebExt(Job):
    """The sign step driving the web-ext this repo pins, against FakeAmo."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_WEB_EXT:
            raise AssertionError(f"ABSH_REQUIRE_WEB_EXT is set but there is no "
                                 f"bash/node/{WEB_EXT.relative_to(ROOT)}")
        cls.built = Path(tempfile.mkdtemp())
        subprocess.run([sys.executable, str(ROOT / "tools" / "package.py"),
                        "--tag", "v1.0.0-alpha.2", "--out", str(cls.built)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.built, ignore_errors=True)

    def setUp(self):
        super().setUp()
        with zipfile.ZipFile(self.built / "audiobookshelf-helper-firefox-1.0.0-alpha.2.zip") as z:
            z.extractall(self.work / "extension" / "dist" / "firefox")
        # `npx web-ext` resolves to the repo's own node_modules on a runner;
        # this does the same from a directory that has none.
        self.stub("npx", f'#!/bin/sh\n[ "$1" = web-ext ] && shift\n'
                         f'exec "{NODE}" "{WEB_EXT}" "$@"\n')
        self.stub("gh", GH)
        self.amo = None

    def tearDown(self):
        if self.amo:
            self.amo.close()
        super().tearDown()

    def sign_against(self, tag, amo):
        self.amo = amo
        env = {**CREDS, "PRERELEASE": tag["PRERELEASE"], "TAG": tag["TAG"]}
        controls = {"WEB_EXT_AMO_BASE_URL": amo.base + "/api/v5/",
                    "WEB_EXT_NO_CONFIG_DISCOVERY": "true", "NO_UPDATE_NOTIFIER": "1"}
        return self.run_step(SIGN, env, **controls)

    def test_unlisted_comes_back_signed(self):
        s = self.sign_against(ALPHA, FakeAmo(approve=True))
        self.assertEqual(s.code, 0, s.log)
        self.assertEqual(self.amo.channel, "unlisted")
        xpis = list((self.work / "release-xpi").glob("*.xpi"))
        self.assertEqual([p.name for p in xpis], ["cb73684229b84553a7b8-1.0.0.2.xpi"])
        a = self.attach(ALPHA, s.outputs)
        self.assertEqual(a.code, 0, a.log)
        self.assertIn("audiobookshelf-helper-firefox-1.0.0-alpha.2.xpi --clobber",
                      self.calls()[-1])

    def test_listed_returns_at_submission(self):
        """AMO never approves this one inside the run, as for a real listed
        version awaiting review. Without --approval-timeout 0 web-ext polls for
        fifteen minutes and then fails the step."""
        s = self.sign_against(STABLE, FakeAmo(approve=False))
        self.assertEqual(s.code, 0, s.log)
        self.assertEqual(self.amo.channel, "listed")
        self.assertFalse(any("/versions/42/" in r for r in self.amo.seen),
                         "waited for approval of a listed version")
        self.assertFalse(list((self.work / "release-xpi").glob("*.xpi")))
        a = self.attach(STABLE, s.outputs)
        self.assertEqual(a.code, 0, a.log)
        self.assertIn("listed submission accepted", a.log)

    def test_the_conflict_message_web_ext_prints_is_the_one_the_step_looks_for(self):
        s = self.sign_against(ALPHA, FakeAmo(approve=True, conflict=True))
        self.assertNotEqual(s.code, 0, s.log)
        self.assertIn("::error title=AMO::AMO already has this version", s.log)


if __name__ == "__main__":
    unittest.main()
