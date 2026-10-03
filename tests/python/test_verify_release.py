"""The post-publish check: does the release GitHub serves match its tag?

Both release failures of the alpha cycle were in what got published, not what
got built, and every job reported success over them. So the verifier is run
here against a real HTTP stand-in for GitHub's releases API on localhost,
serving releases assembled from archives tools/package.py really builds -
including the exact shape alpha.1 shipped in, which it must refuse.
"""
import hashlib
import io
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
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(HERE))
import verify_release as V  # noqa: E402
import workflow_steps as W  # noqa: E402

REPO = "dadatuputi/audiobookshelf-helper"
ALPHA, STABLE = "v1.0.0-alpha.2", "v1.0.0"
BUILT = {}          # tag -> directory of what package.py made for it
T0, T1, T2 = "2026-10-03T10:00:00Z", "2026-10-03T10:05:00Z", "2026-10-03T10:09:00Z"


def setUpModule():
    for tag in (ALPHA, STABLE):
        out = Path(tempfile.mkdtemp(prefix="verify-artifacts-"))
        subprocess.run([sys.executable, str(ROOT / "tools" / "package.py"),
                        "--tag", tag, "--out", str(out)], check=True, capture_output=True)
        BUILT[tag] = out


def tearDownModule():
    for d in BUILT.values():
        shutil.rmtree(d, ignore_errors=True)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def signed_xpi(tag, version=None, signed=True, gecko=None):
    """What AMO hands back: the firefox bundle plus its signature files."""
    info = V.RV.parse_tag(tag)
    src = BUILT[tag] / V.zip_name("firefox", info["semver"])
    buf = io.BytesIO()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(buf, "w") as zout:
        for n in zin.namelist():
            data = zin.read(n)
            if n == "manifest.json" and (version or gecko):
                m = json.loads(data)
                if version:
                    m["version"] = version
                if gecko:
                    m["browser_specific_settings"]["gecko"]["id"] = gecko
                data = json.dumps(m).encode()
            zout.writestr(n, data)
        if signed:
            for n in ("META-INF/cose.manifest", "META-INF/cose.sig",
                      "META-INF/manifest.mf", "META-INF/mozilla.sf", "META-INF/mozilla.rsa"):
                zout.writestr(n, b"stand-in signature")
    return buf.getvalue()


class GitHub:
    """GET /repos/{repo}/releases/tags/{tag}, and the asset download host."""

    def __init__(self):
        outer = self
        self.releases = {}
        self.blobs = {}
        self.auth = []          # (path, Authorization or None)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                outer.auth.append((path, self.headers.get("Authorization")))
                prefix = f"/repos/{REPO}/releases/tags/"
                if path.startswith(prefix) and path[len(prefix):] in outer.releases:
                    body, kind = json.dumps(outer.releases[path[len(prefix):]]).encode(), "json"
                elif path.startswith("/download/") and path[10:] in outer.blobs:
                    body, kind = outer.blobs[path[10:]], "octet-stream"
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", f"application/{kind}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def asset(self, name, blob, when=T0, digest=True, state="uploaded"):
        self.blobs[name] = blob
        return {"name": name, "state": state, "size": len(blob), "updated_at": when,
                "digest": f"sha256:{sha(blob)}" if digest else None,
                "browser_download_url": f"{self.base}/download/{name}"}

    def publish(self, tag, files=None, prerelease=None, extra=(), draft=False):
        """A release for `tag` carrying `files` (name -> bytes, default: the
        four zips exactly as built) plus `extra` asset dicts."""
        info = V.RV.parse_tag(tag)
        if files is None:
            files = {V.zip_name(k, info["semver"]):
                     (BUILT[tag] / V.zip_name(k, info["semver"])).read_bytes()
                     for k in V.ZIP_KINDS}
        assets = [self.asset(n, b) for n, b in files.items()] + list(extra)
        self.releases[tag] = {
            "tag_name": tag, "name": tag, "draft": draft,
            "prerelease": info["prerelease"] if prerelease is None else prerelease,
            "assets": assets,
        }
        return self.releases[tag]


class Case(unittest.TestCase):
    def setUp(self):
        self.gh = GitHub()
        self._env = {k: os.environ.get(k) for k in ("GITHUB_API_URL", "GH_TOKEN", "GITHUB_TOKEN")}
        os.environ["GITHUB_API_URL"] = self.gh.base
        os.environ["GH_TOKEN"] = "test-token"
        os.environ.pop("GITHUB_TOKEN", None)
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        self.gh.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def verify(self, tag, amo=("success", "true", "unlisted"), artifacts=None):
        return V.verify(tag, REPO, artifacts or BUILT[tag], *amo)

    def assertClean(self, result):
        problems, _, _ = result
        self.assertEqual(problems, [])

    def assertProblem(self, result, *needles):
        problems, _, _ = result
        hit = [p for p in problems if all(n in p for n in needles)]
        self.assertTrue(hit, f"no problem mentioning {needles}; got {problems}")
        return hit[0]

    def xpi(self, tag, when=T1, name=None, **kw):
        semver = V.RV.parse_tag(tag)["semver"]
        return self.gh.asset(name or V.xpi_name(semver), signed_xpi(tag, **kw), when=when)

    def artifacts_copy(self, tag):
        dest = self.tmp / "artifacts"
        shutil.copytree(BUILT[tag], dest)
        return dest


class Matches(Case):
    def test_a_prerelease_with_its_signed_xpi(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        problems, warnings, notes = self.verify(ALPHA)
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])
        self.assertTrue(any("native helper is stamped 1.0.0-alpha.2" in n for n in notes), notes)

    def test_a_prerelease_with_no_amo_credentials(self):
        self.gh.publish(ALPHA)
        self.assertClean(self.verify(ALPHA, ("success", "false", "")))

    def test_a_stable_release_submitted_to_the_listed_channel(self):
        # AMO signs a listed version after review, so no xpi is the normal case.
        self.gh.publish(STABLE)
        self.assertClean(self.verify(STABLE, ("success", "true", "listed")))

    def test_a_listed_xpi_is_allowed_if_it_is_right(self):
        self.gh.publish(STABLE, extra=[self.xpi(STABLE)])
        self.assertClean(self.verify(STABLE, ("success", "true", "listed")))

    def test_checksums_and_their_signature_are_allowed(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA),
                                      self.gh.asset("SHA256SUMS", b"sums\n", when=T0),
                                      self.gh.asset("SHA256SUMS.sig", b"sig", when=T2)])
        self.assertClean(self.verify(ALPHA))

    def test_the_token_goes_to_the_api_and_not_the_download_host(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        self.assertClean(self.verify(ALPHA))
        api = [a for p, a in self.gh.auth if p.startswith("/repos/")]
        dl = [a for p, a in self.gh.auth if p.startswith("/download/")]
        self.assertEqual(api, ["Bearer test-token"])
        self.assertEqual(dl, [None], "sent the GitHub token to the asset host")


class TheAlphaOneShape(Case):
    """What v1.0.0-alpha.1 actually shipped: this build's four zips and an xpi
    from the previous build, still under AMO's internal file name."""

    def test_is_refused(self):
        stale = self.xpi(ALPHA, when=T0, name="cb73684229b84553a7b8-1.0.0.1.xpi",
                         version="1.0.0.1")
        self.gh.publish(ALPHA, extra=[stale])
        # Either way the amo job went, that file does not belong.
        for amo in (("success", "false", ""), ("failure", "true", "unlisted")):
            p = self.assertProblem(self.verify(ALPHA, amo),
                                   "cb73684229b84553a7b8-1.0.0.1.xpi", "AMO's internal id")
            self.assertIn("gh release delete-asset v1.0.0-alpha.2 "
                          "cb73684229b84553a7b8-1.0.0.1.xpi --yes", p)

    def test_and_so_is_a_correctly_named_xpi_from_before_this_build(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA, when="2026-09-01T00:00:00Z")])
        self.assertProblem(self.verify(ALPHA), "audiobookshelf-helper-firefox-1.0.0-alpha.2.xpi",
                           "before", "previous build")

    def test_an_xpi_when_nothing_was_signed_this_run(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        self.assertProblem(self.verify(ALPHA, ("success", "false", "")),
                           "no AMO credentials", "left by another build")


class Assets(Case):
    def test_a_missing_zip(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        rel = self.gh.releases[ALPHA]
        rel["assets"] = [a for a in rel["assets"] if "-native-" not in a["name"]]
        self.assertProblem(self.verify(ALPHA), "audiobookshelf-helper-native-1.0.0-alpha.2.zip",
                           "missing", "re-run the github-release job")

    def test_a_zip_named_for_another_version(self):
        old = self.gh.asset("audiobookshelf-helper-native-1.0.0-alpha.1.zip", b"old")
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA), old])
        self.assertProblem(self.verify(ALPHA), "native-1.0.0-alpha.1.zip",
                           "named for version 1.0.0-alpha.1")

    def test_something_else_entirely(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA), self.gh.asset("notes.txt", b"x")])
        self.assertProblem(self.verify(ALPHA), "notes.txt", "not an asset this release produces")

    def test_bytes_from_another_build(self):
        semver = "1.0.0-alpha.2"
        files = {V.zip_name(k, semver): (BUILT[ALPHA] / V.zip_name(k, semver)).read_bytes()
                 for k in V.ZIP_KINDS}
        files[V.zip_name("chrome", semver)] += b"\0"
        self.gh.publish(ALPHA, files=files, extra=[self.xpi(ALPHA)])
        self.assertProblem(self.verify(ALPHA), "chrome-1.0.0-alpha.2.zip",
                           "bytes from another build")

    def test_no_digest_reported(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        self.gh.releases[ALPHA]["assets"][0]["digest"] = None
        self.assertProblem(self.verify(ALPHA), "no sha256 digest")

    def test_an_upload_that_never_completed(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        self.gh.releases[ALPHA]["assets"][1]["state"] = "starter"
        self.assertProblem(self.verify(ALPHA), "'starter'", "never completed")

    def test_a_checksum_manifest_this_build_made_must_be_published(self):
        art = self.artifacts_copy(ALPHA)
        (art / "SHA256SUMS").write_bytes(b"this build's sums\n")
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        self.assertProblem(self.verify(ALPHA, artifacts=art), "SHA256SUMS", "missing",
                           "this run built it")
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA), self.gh.asset("SHA256SUMS", b"old\n")])
        self.assertProblem(self.verify(ALPHA, artifacts=art), "SHA256SUMS",
                           "bytes from another build")

    def test_a_signature_older_than_the_manifest_it_signs(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA),
                                      self.gh.asset("SHA256SUMS", b"sums\n", when=T1),
                                      self.gh.asset("SHA256SUMS.sig", b"sig", when=T0)])
        self.assertProblem(self.verify(ALPHA), "SHA256SUMS.sig", "previous build")


class Release(Case):
    def test_no_release_for_the_tag(self):
        self.assertProblem(self.verify(ALPHA), "no published release for v1.0.0-alpha.2")

    def test_a_prerelease_published_as_stable(self):
        self.gh.publish(ALPHA, prerelease=False, extra=[self.xpi(ALPHA)])
        self.assertProblem(self.verify(ALPHA), "prerelease=false",
                           "gh release edit v1.0.0-alpha.2 --prerelease=true")

    def test_a_stable_release_published_as_prerelease(self):
        self.gh.publish(STABLE, prerelease=True)
        self.assertProblem(self.verify(STABLE, ("success", "true", "listed")),
                           "prerelease=true", "--prerelease=false")

    def test_a_draft(self):
        self.gh.publish(ALPHA, draft=True, extra=[self.xpi(ALPHA)])
        self.assertProblem(self.verify(ALPHA), "draft")

    def test_github_unreachable(self):
        os.environ["GITHUB_API_URL"] = "http://127.0.0.1:1"
        self.assertProblem(self.verify(ALPHA), "could not reach GitHub", "re-run")


class Contents(Case):
    def test_a_native_helper_not_stamped_with_the_tag(self):
        """`absh update` reads this; a "dev" stamp offers the update forever."""
        art = self.artifacts_copy(ALPHA)
        native = art / V.zip_name("native", "1.0.0-alpha.2")
        buf = io.BytesIO()
        with zipfile.ZipFile(native) as zin, zipfile.ZipFile(buf, "w") as zout:
            for n in zin.namelist():
                data = zin.read(n)
                if n == "absh/version.py":
                    data = data.replace(b'RELEASE = "1.0.0-alpha.2"', b'RELEASE = "dev"')
                zout.writestr(n, data)
        native.write_bytes(buf.getvalue())
        files = {p.name: p.read_bytes() for p in art.glob("*.zip")}
        self.gh.publish(ALPHA, files=files, extra=[self.xpi(ALPHA)])
        self.assertProblem(self.verify(ALPHA, artifacts=art), "RELEASE='dev'", "stamp_version")


class SignedAddon(Case):
    def test_required_when_unlisted_signing_ran(self):
        self.gh.publish(ALPHA)
        self.assertProblem(self.verify(ALPHA), "firefox-1.0.0-alpha.2.xpi", "missing",
                           "attach step")

    def test_wrong_version_inside(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA, version="1.0.0.1")])
        self.assertProblem(self.verify(ALPHA), "add-on version '1.0.0.1'", "1.0.0.2")

    def test_unsigned(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA, signed=False)])
        self.assertProblem(self.verify(ALPHA), "no AMO signature")

    def test_another_addon(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA, gecko="someone@else")])
        self.assertProblem(self.verify(ALPHA), "someone@else")


class WhatAmoDid(Case):
    """The verifier is only as honest as what it is told about the amo job."""

    def test_the_mapping(self):
        R, O = V.REQUIRED, V.OPTIONAL
        self.assertEqual(V.xpi_expectation("success", "true", "unlisted")[0], R)
        self.assertEqual(V.xpi_expectation("success", "true", "listed")[0], O)
        self.assertIsNone(V.xpi_expectation("success", "false", "")[0])

    def test_a_failed_amo_job_is_said_rather_than_assumed(self):
        self.gh.publish(ALPHA)
        problems, warnings, _ = self.verify(ALPHA, ("failure", "true", "unlisted"))
        self.assertEqual(problems, [])
        self.assertTrue(warnings and "ended 'failure'" in warnings[0], warnings)
        self.assertIn("not required", warnings[0])

    def test_a_skipped_amo_job(self):
        self.gh.publish(ALPHA)
        problems, warnings, _ = self.verify(ALPHA, ("skipped", "", ""))
        self.assertEqual(problems, [])
        self.assertTrue(warnings)

    def test_success_with_no_outputs_means_the_wiring_is_broken(self):
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        self.assertProblem(self.verify(ALPHA, ("success", "", "")), "wiring")


class Cli(Case):
    def test_reports_as_annotations_in_actions(self):
        self.gh.publish(ALPHA)
        out = io.StringIO()
        os.environ["GITHUB_ACTIONS"] = "true"
        try:
            from contextlib import redirect_stdout, redirect_stderr
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = V.main(["--tag", ALPHA, "--repo", REPO, "--artifacts", str(BUILT[ALPHA]),
                               "--amo-result", "success", "--amo-attempted", "true",
                               "--amo-channel", "unlisted"])
        finally:
            os.environ.pop("GITHUB_ACTIONS", None)
        self.assertEqual(code, 1)
        self.assertIn("::error title=release v1.0.0-alpha.2::", out.getvalue())

    def test_a_bad_tag(self):
        with open(os.devnull, "w") as null:
            from contextlib import redirect_stderr
            with redirect_stderr(null):
                self.assertEqual(V.main(["--tag", "nope", "--artifacts", "."]), 1)


@unittest.skipUnless(W.CAN_RUN, "runs the workflow's bash; not on Windows")
class Workflow(Case):
    """The verify-release job as release.yml declares it."""

    def test_runs_after_everything_that_touches_the_release(self):
        job = W.job_text("verify-release")
        self.assertIn("needs: [package, github-release, amo]", job)
        # Not success(): a failed amo job must still get the rest checked.
        self.assertIn("!cancelled()", job)
        self.assertIn("needs.github-release.result == 'success'", job)
        self.assertIn("github.event.inputs.dry_run != 'true'", job)

    def test_amo_outputs_are_wired_through(self):
        amo = W.job_text("amo")
        self.assertIn("attempted: ${{ steps.sign.outputs.attempted }}", amo)
        self.assertIn("channel: ${{ steps.sign.outputs.channel }}", amo)
        self.assertEqual(W.step("amo", "Sign / submit")["id"], "sign")
        env = W.step("verify-release", "Check the published release against the tag")["env"]
        self.assertEqual(env["AMO_RESULT"], "${{ needs.amo.result }}")
        self.assertEqual(env["AMO_ATTEMPTED"], "${{ needs.amo.outputs.attempted }}")
        self.assertEqual(env["AMO_CHANNEL"], "${{ needs.amo.outputs.channel }}")

    def test_the_step_as_written(self):
        work = self.tmp / "work"
        shutil.copytree(ROOT / "tools", work / "tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (work / "extension").mkdir()
        shutil.copy(ROOT / "extension" / "identity.json", work / "extension")
        shutil.copytree(BUILT[ALPHA], work / "release")
        shim = self.tmp / "bin" / "python3"
        shim.parent.mkdir()
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        shim.chmod(0o755)
        step = "Check the published release against the tag"
        env = {"GH_TOKEN": "t", "TAG": ALPHA, "AMO_RESULT": "success",
               "AMO_ATTEMPTED": "true", "AMO_CHANNEL": "unlisted"}
        allow = ("GITHUB_API_URL", "GITHUB_REPOSITORY")
        extra = {"GITHUB_API_URL": self.gh.base, "GITHUB_REPOSITORY": REPO}

        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA)])
        r = W.run_step("verify-release", step, {**env, **extra}, work, [shim.parent], allow)
        self.assertEqual(r.code, 0, r.log)
        self.assertIn("matches the tag", r.log)

        stale = self.xpi(ALPHA, when=T0, name="cb73684229b84553a7b8-1.0.0.1.xpi")
        self.gh.publish(ALPHA, extra=[self.xpi(ALPHA), stale])
        r = W.run_step("verify-release", step, {**env, **extra}, work, [shim.parent], allow)
        self.assertEqual(r.code, 1, r.log)
        self.assertIn("cb73684229b84553a7b8-1.0.0.1.xpi", r.log)


if __name__ == "__main__":
    unittest.main()
