"""The Chrome Web Store publisher, against a stand-in for the store.

This path has never run for real: its job is skipped for every prerelease and
its secrets are not set, so the first stable tag would be its first contact
with Google. So it runs here against a real HTTP server on localhost that
answers the way the store's v2 API and Google's token endpoint are documented
to - every way the publish can fail has to end in exit 1 and a sentence, never
a traceback and never a "success" that published nothing.

What the stand-in cannot prove is that the store really answers like this; see
the module docstring of tools/publish_cws.py for what is known and what is not.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_steps as W  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "publish_cws.py"

CREDS = {
    "CWS_CLIENT_ID": "client.apps.googleusercontent.com",
    "CWS_CLIENT_SECRET": "client-secret",
    "CWS_REFRESH_TOKEN": "1//refresh",
    "CWS_PUBLISHER_ID": "pub-123",
    "CWS_ITEM_ID": "abcdefghijklmnopabcdefghijklmnop",
}
ITEM = f"/publishers/{CREDS['CWS_PUBLISHER_ID']}/items/{CREDS['CWS_ITEM_ID']}"
UPLOAD = f"/upload/v2{ITEM}:upload"
PUBLISH = f"/v2{ITEM}:publish"
STATUS = f"/v2{ITEM}:fetchStatus"

TOKEN_OK = (200, {"access_token": "ya29.token", "expires_in": 3599, "token_type": "Bearer"})
UPLOADED = (200, {"name": ITEM[1:], "itemId": CREDS["CWS_ITEM_ID"],
                  "crxVersion": "1.0.0", "uploadState": "SUCCEEDED"})
IN_REVIEW = (200, {"name": ITEM[1:], "itemId": CREDS["CWS_ITEM_ID"],
                   "state": "PENDING_REVIEW"})


class Store:
    """Google's token endpoint and the store's item endpoints, scripted.

    `routes` maps "METHOD /path" to a list of (status, body) answers, used in
    order and the last one repeated. Every request is recorded.
    """

    def __init__(self, routes):
        outer = self
        self.routes = {k: list(v) for k, v in routes.items()}
        self.seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                path = urllib.parse.urlsplit(self.path).path
                key = f"{self.command} {path}"
                outer.seen.append({"key": key, "query": urllib.parse.urlsplit(self.path).query,
                                   "headers": dict(self.headers), "body": body})
                answers = outer.routes.get(key)
                if not answers:
                    status, payload = 404, {"error": {"code": 404, "message": f"no {key}",
                                                      "status": "NOT_FOUND"}}
                else:
                    status, payload = answers[0] if len(answers) == 1 else answers.pop(0)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = _answer

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def keys(self):
        return [s["key"] for s in self.seen]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.zip = self.tmp / "audiobookshelf-helper-chrome-1.0.0.zip"
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("manifest.json", json.dumps({"manifest_version": 3,
                                                    "name": "x", "version": "1.0.0"}))
            z.writestr("background.js", "// stand-in\n")
        self.stores = []

    def tearDown(self):
        for s in self.stores:
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self, **routes):
        table = {"POST /token": [TOKEN_OK], f"POST {UPLOAD}": [UPLOADED],
                 f"POST {PUBLISH}": [IN_REVIEW]}
        names = {"token": "POST /token", "upload": f"POST {UPLOAD}",
                 "publish": f"POST {PUBLISH}", "status": f"GET {STATUS}"}
        for k, v in routes.items():
            table[names[k]] = v
        s = Store(table)
        self.stores.append(s)
        return s

    def env(self, store=None, creds=CREDS, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CWS_")}
        env.update(creds)
        env["CWS_POLL_INTERVAL"] = "0.01"
        if store:
            env["CWS_OAUTH_BASE"] = store.base
            env["CWS_API_BASE"] = store.base
        env.update(extra)
        return env

    def run_it(self, *args, store=None, creds=CREDS, **extra):
        p = subprocess.run([sys.executable, str(SCRIPT), "--zip", str(self.zip), *args],
                           env=self.env(store, creds, **extra),
                           capture_output=True, text=True, timeout=60)
        self.assertNotIn("Traceback", p.stderr, "failed with a traceback, not a sentence")
        return p


class Publishes(Case):
    def test_upload_then_publish(self):
        s = self.store()
        p = self.run_it(store=s)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(s.keys(), ["POST /token", f"POST {UPLOAD}", f"POST {PUBLISH}"])
        self.assertIn("publish PENDING_REVIEW", p.stdout)

    def test_requests_are_shaped_as_the_api_wants(self):
        s = self.store()
        self.assertEqual(self.run_it(store=s).returncode, 0)
        token, upload, publish = s.seen
        form = urllib.parse.parse_qs(token["body"].decode())
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], [CREDS["CWS_REFRESH_TOKEN"]])
        self.assertEqual(form["client_id"], [CREDS["CWS_CLIENT_ID"]])
        for r in (upload, publish):
            self.assertEqual(r["headers"].get("Authorization"), "Bearer ya29.token")
        self.assertEqual(upload["body"], self.zip.read_bytes(), "uploaded other bytes")
        self.assertEqual(upload["headers"].get("Content-Type"), "application/zip")
        # The old script declared Content-Length: 0 and then sent a body anyway.
        self.assertEqual(int(publish["headers"]["Content-Length"]), len(publish["body"]))
        self.assertEqual(json.loads(publish["body"]), {"publishType": "DEFAULT_PUBLISH"})

    def test_staged(self):
        s = self.store(publish=[(200, {"state": "STAGED"})])
        self.assertEqual(self.run_it("--staged", store=s).returncode, 0)
        self.assertEqual(json.loads(s.seen[-1]["body"]), {"publishType": "STAGED_PUBLISH"})

    def test_waits_for_an_upload_the_store_is_still_processing(self):
        """Publishing before processing ends would publish the previous draft."""
        s = self.store(upload=[(200, {"uploadState": "IN_PROGRESS"})],
                       status=[(200, {"lastAsyncUploadState": "IN_PROGRESS"}),
                               (200, {"lastAsyncUploadState": "SUCCEEDED"})])
        p = self.run_it(store=s)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(s.keys(), ["POST /token", f"POST {UPLOAD}", f"GET {STATUS}",
                                    f"GET {STATUS}", f"POST {PUBLISH}"])

    def test_accepts_either_spelling_of_the_upload_state(self):
        # Google's prose says UPLOAD_IN_PROGRESS; other sources say IN_PROGRESS.
        s = self.store(upload=[(200, {"uploadState": "UPLOAD_SUCCEEDED"})])
        self.assertEqual(self.run_it(store=s).returncode, 0)


class Refuses(Case):
    def assertFailsWith(self, p, *needles):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        for n in needles:
            self.assertIn(n, p.stderr)

    def test_a_revoked_refresh_token(self):
        s = self.store(token=[(400, {"error": "invalid_grant",
                                     "error_description": "Token has been expired or revoked."})])
        p = self.run_it(store=s)
        self.assertFailsWith(p, "invalid_grant", "expired or revoked", "CWS_REFRESH_TOKEN")
        self.assertEqual(s.keys(), ["POST /token"], "went on without a token")

    def test_a_wrong_client(self):
        s = self.store(token=[(401, {"error": "invalid_client",
                                     "error_description": "Unauthorized"})])
        self.assertFailsWith(self.run_it(store=s), "CWS_CLIENT_ID")

    def test_upload_rejected_with_item_errors(self):
        s = self.store(upload=[(200, {"uploadState": "FAILED", "itemError": [
            {"error_code": "PKG_MANIFEST_PARSE_ERROR",
             "error_detail": "Manifest is not valid JSON."}]})])
        p = self.run_it(store=s)
        self.assertFailsWith(p, "rejected the upload", "PKG_MANIFEST_PARSE_ERROR",
                             "Manifest is not valid JSON.", "Nothing was published")
        self.assertNotIn(f"POST {PUBLISH}", s.keys(), "published after a failed upload")

    def test_upload_rejected_with_an_http_error(self):
        s = self.store(upload=[(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                "message": "Version must be greater"}})])
        p = self.run_it(store=s)
        self.assertFailsWith(p, "uploading refused", "HTTP 400", "INVALID_ARGUMENT",
                             "Version must be greater")
        self.assertNotIn(f"POST {PUBLISH}", s.keys())

    def test_upload_that_fails_while_processing(self):
        s = self.store(upload=[(200, {"uploadState": "IN_PROGRESS"})],
                       status=[(200, {"lastAsyncUploadState": "FAILED"})])
        self.assertFailsWith(self.run_it(store=s), "FAILED")
        self.assertNotIn(f"POST {PUBLISH}", s.keys())

    def test_upload_that_never_finishes_processing(self):
        s = self.store(upload=[(200, {"uploadState": "IN_PROGRESS"})],
                       status=[(200, {"lastAsyncUploadState": "IN_PROGRESS"})])
        p = self.run_it(store=s, CWS_POLL_LIMIT="3")
        self.assertFailsWith(p, "still processing")
        self.assertNotIn(f"POST {PUBLISH}", s.keys())

    def test_the_store_reads_a_different_version(self):
        s = self.store(upload=[(200, {"uploadState": "SUCCEEDED", "crxVersion": "0.9.0"})])
        self.assertFailsWith(self.run_it(store=s), "0.9.0", "1.0.0")
        self.assertNotIn(f"POST {PUBLISH}", s.keys())

    def test_publish_rejected_by_state(self):
        """The old script printed the status and returned 0 whatever it was."""
        s = self.store(publish=[(200, {"state": "REJECTED"})])
        self.assertFailsWith(self.run_it(store=s), "did not accept", "REJECTED")

    def test_publish_refused_outright(self):
        # e.g. the previous version is still in review.
        s = self.store(publish=[(400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                                 "message": "Item is pending review."}})])
        self.assertFailsWith(self.run_it(store=s), "publishing refused",
                             "FAILED_PRECONDITION", "pending review")

    def test_an_answer_that_is_not_json(self):
        s = self.store(publish=[(200, b"<html>gateway</html>")])
        self.assertFailsWith(self.run_it(store=s), "not JSON")

    def test_the_store_unreachable(self):
        """The old script let URLError escape as a traceback."""
        p = self.run_it(CWS_OAUTH_BASE="http://127.0.0.1:1", CWS_API_BASE="http://127.0.0.1:1")
        self.assertFailsWith(p, "could not reach 127.0.0.1:1")

    def test_a_zip_with_no_manifest_at_the_root(self):
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("chrome/manifest.json", "{}")
        s = self.store()
        self.assertFailsWith(self.run_it(store=s), "manifest.json at its root")
        self.assertEqual(s.keys(), [], "contacted the store with a bundle it will refuse")


class Configuration(Case):
    def test_no_credentials_is_a_skip(self):
        s = self.store()
        p = self.run_it(store=s, creds={})
        self.assertEqual(p.returncode, 2)
        self.assertEqual(s.keys(), [])

    def test_some_credentials_is_a_mistake_not_a_skip(self):
        """The old script reported a half-configured store as "not configured"."""
        s = self.store()
        partial = {k: v for k, v in CREDS.items() if k != "CWS_PUBLISHER_ID"}
        p = self.run_it(store=s, creds=partial)
        self.assertEqual(p.returncode, 1)
        self.assertIn("CWS_PUBLISHER_ID", p.stderr)
        self.assertEqual(s.keys(), [])

    def test_dry_run_proves_the_credentials_and_changes_nothing(self):
        s = self.store(status=[(200, {"name": ITEM[1:], "itemId": CREDS["CWS_ITEM_ID"]})])
        p = self.run_it("--dry-run", store=s)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(s.keys(), ["POST /token", f"GET {STATUS}"])
        self.assertIn("dry run, nothing uploaded", p.stdout)

    def test_dry_run_fails_on_bad_credentials(self):
        s = self.store(token=[(400, {"error": "invalid_grant"})])
        p = self.run_it("--dry-run", store=s)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(s.keys(), ["POST /token"])


@unittest.skipUnless(W.CAN_RUN, "runs the workflow's bash; not on Windows")
class WorkflowJob(Case):
    """The chrome-web-store job's own shell, as release.yml has it."""

    JOB, STEP = "chrome-web-store", "Upload and publish"

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "work"
        shutil.copytree(ROOT / "tools", self.work / "tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.work / "release").mkdir()
        shutil.copy(self.zip, self.work / "release" / self.zip.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        shim = self.bin / "python3"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        shim.chmod(0o755)

    def job(self, store, secrets, dry_run=""):
        env = {k: secrets.get(k, "") for k in CREDS}   # Actions sets unset secrets to ""
        env["DRY_RUN"] = dry_run
        env.update({"CWS_OAUTH_BASE": store.base, "CWS_API_BASE": store.base,
                    "CWS_POLL_INTERVAL": "0.01"})
        return W.run_step(self.JOB, self.STEP, env, self.work, [self.bin],
                          allow=("CWS_OAUTH_BASE", "CWS_API_BASE", "CWS_POLL_INTERVAL"))

    def test_declares_every_credential_the_script_needs(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import publish_cws
        self.assertLessEqual(set(publish_cws.REQUIRED), set(W.step(self.JOB, self.STEP)["env"]))

    def test_only_runs_for_stable_tags(self):
        self.assertIn("needs.package.outputs.prerelease == 'false'", W.job_text(self.JOB))

    def test_missing_secrets_is_a_notice_and_never_a_publish(self):
        s = self.store()
        r = self.job(s, {})
        self.assertEqual(r.code, 0, r.log)
        self.assertIn("::notice title=Chrome Web Store::skipped", r.log)
        self.assertEqual(s.keys(), [], "contacted the store with no credentials")

    def test_half_configured_secrets_fail_the_job(self):
        s = self.store()
        r = self.job(s, {"CWS_CLIENT_ID": "x", "CWS_ITEM_ID": "y"})
        self.assertEqual(r.code, 1, r.log)
        self.assertNotIn("::notice", r.log)
        self.assertEqual(s.keys(), [])

    def test_dispatch_dry_run_uploads_nothing(self):
        s = self.store(status=[(200, {})])
        r = self.job(s, CREDS, dry_run="true")
        self.assertEqual(r.code, 0, r.log)
        self.assertEqual(s.keys(), ["POST /token", f"GET {STATUS}"])

    def test_publishes_with_secrets(self):
        s = self.store()
        r = self.job(s, CREDS)
        self.assertEqual(r.code, 0, r.log)
        self.assertIn(f"POST {PUBLISH}", s.keys())

    def test_a_failed_publish_fails_the_job(self):
        s = self.store(publish=[(200, {"state": "REJECTED"})])
        self.assertEqual(self.job(s, CREDS).code, 1)


if __name__ == "__main__":
    unittest.main()
