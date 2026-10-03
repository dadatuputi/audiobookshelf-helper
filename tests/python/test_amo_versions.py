"""The pre-flight check that stops a release asking AMO for a version it has.

The point of this check is to fail *closed* on one answer only - AMO says the
version exists - and to get out of the way for every other outcome, because a
network blip must not block a release that would have signed fine. So most of
what is here is the ways AMO can decline to answer.

The API is a real HTTP server on localhost rather than a patched urlopen, so
the JWT header, the paging and the unlisted filter are exercised as written.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import amo_versions as A  # noqa: E402

ADDON = "audiobookshelf-helper@dadatuputi.github.io"
ISSUER, SECRET = "user:1:2", "s3cret"


class Fake:
    """AMO's versions endpoint, as much of it as this check uses."""

    def __init__(self, pages=None, status=None):
        outer = self
        self.seen_auth = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen_auth.append(self.headers.get("Authorization", ""))
                if status:
                    self.send_response(status)
                    self.end_headers()
                    return
                n = int(self.path.split("page=")[-1]) if "page=" in self.path else 1
                body = json.dumps(pages[n - 1]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.api = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def page(versions, nxt=None):
    return {"results": [{"version": v} for v in versions], "next": nxt}


class Case(unittest.TestCase):
    def setUp(self):
        self._api = A.API
        self.servers = []
        os.environ["AMO_JWT_ISSUER"] = ISSUER
        os.environ["AMO_JWT_SECRET"] = SECRET

    def tearDown(self):
        for s in self.servers:
            s.close()
        A.API = self._api
        os.environ.pop("AMO_JWT_ISSUER", None)
        os.environ.pop("AMO_JWT_SECRET", None)

    def serve(self, pages=None, status=None):
        f = Fake(pages, status)
        self.servers.append(f)
        A.API = f.api + "/api/v5"
        return f


class Token(unittest.TestCase):
    def test_is_a_jwt_amo_would_accept(self):
        """Hand-rolled, so the shape is worth asserting rather than assuming."""
        tok = A.jwt(ISSUER, SECRET)
        head, body, sig = tok.split(".")

        def unpad(s):
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

        self.assertEqual(json.loads(unpad(head)), {"alg": "HS256", "typ": "JWT"})
        claims = json.loads(unpad(body))
        self.assertEqual(claims["iss"], ISSUER)
        # AMO rejects a token valid for more than five minutes.
        self.assertLessEqual(claims["exp"] - claims["iat"], 300)
        self.assertTrue(claims["jti"], "jti is required and must be single-use")
        expect = hmac.new(SECRET.encode(), f"{head}.{body}".encode(),
                          hashlib.sha256).digest()
        self.assertEqual(unpad(sig), expect, "signature is not HS256 over head.body")

    def test_each_call_is_a_different_token(self):
        # jti is single-use, so reusing one across pages would start failing.
        self.assertNotEqual(A.jwt(ISSUER, SECRET), A.jwt(ISSUER, SECRET))


class Reads(Case):
    def test_asks_for_unlisted_versions_too(self):
        """An alpha is unlisted, and the default filter hides those - which
        would make every already-taken alpha version look free."""
        self.assertIn("filter=all_with_unlisted", A.VERSIONS)

    def test_sends_the_token_as_a_jwt_header(self):
        f = self.serve([page(["1.0.0.1"])])
        A.versions(ADDON, ISSUER, SECRET)
        self.assertTrue(f.seen_auth[0].startswith("JWT "), f.seen_auth)

    def test_follows_paging(self):
        """A version on page two is still taken."""
        pages = [page(["1.0.0.1"]), page(["1.0.0.2"])]
        f = self.serve(pages)
        pages[0]["next"] = f.api + "/api/v5/versions/?page=2"
        self.assertEqual(sorted(A.versions(ADDON, ISSUER, SECRET)),
                         ["1.0.0.1", "1.0.0.2"])
        self.assertEqual(len(f.seen_auth), 2, "did not request the second page")
        self.assertNotEqual(f.seen_auth[0], f.seen_auth[1],
                            "reused a single-use jti across pages")


class FailsClosed(Case):
    def test_exits_one_when_amo_has_the_version(self):
        self.serve([page(["1.0.0.1", "1.0.0.2"])])
        self.assertEqual(A.main(["--conflicts", "1.0.0.2"]), 1)

    def test_exits_zero_when_it_does_not(self):
        self.serve([page(["1.0.0.1", "1.0.0.2"])])
        self.assertEqual(A.main(["--conflicts", "1.0.0.3"]), 0)


class FailsOpen(Case):
    """Everything that is not a definite "that version exists".

    A release that would have signed fine must not be blocked because AMO was
    briefly unreachable, so each of these has to come back 0.
    """

    def test_no_credentials(self):
        os.environ.pop("AMO_JWT_ISSUER", None)
        os.environ.pop("AMO_JWT_SECRET", None)
        self.assertEqual(A.main(["--conflicts", "1.0.0.3"]), 0)

    def test_an_addon_amo_has_never_seen(self):
        # 404 is a real answer - nothing is taken - so it is not undetermined.
        self.serve(status=404)
        self.assertEqual(A.versions(ADDON, ISSUER, SECRET), [])
        self.assertEqual(A.main(["--conflicts", "1.0.0.3"]), 0)

    def test_an_api_error(self):
        self.serve(status=500)
        with self.assertRaises(A.Undetermined):
            A.versions(ADDON, ISSUER, SECRET)
        self.assertEqual(A.main(["--conflicts", "1.0.0.3"]), 0)

    def test_amo_being_unreachable(self):
        A.API = "http://127.0.0.1:1/api/v5"
        with self.assertRaises(A.Undetermined):
            A.versions(ADDON, ISSUER, SECRET)
        self.assertEqual(A.main(["--conflicts", "1.0.0.3"]), 0)


class Identity(Case):
    def test_defaults_to_the_addon_id_the_build_uses(self):
        """A check against the wrong add-on would pass while being meaningless."""
        self.serve([page(["1.0.0.2"])])
        ident = json.loads((ROOT / "extension" / "identity.json").read_text())
        self.assertEqual(ident["geckoId"], ADDON)
        self.assertEqual(A.main(["--conflicts", "1.0.0.2"]), 1)


if __name__ == "__main__":
    unittest.main()
