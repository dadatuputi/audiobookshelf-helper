#!/usr/bin/env python3
"""
Upload and publish a bundle to the Chrome Web Store, through its v2 API.

Standard library only - CI should not need a toolchain to ship.

    python3 tools/publish_cws.py --zip release/...-chrome-1.0.0.zip --dry-run

Credentials come from the environment (set them as repository secrets):

    CWS_CLIENT_ID  CWS_CLIENT_SECRET  CWS_REFRESH_TOKEN
    CWS_PUBLISHER_ID  CWS_ITEM_ID

Getting them is a one-time chore: create an OAuth client of type "Desktop app"
in a Google Cloud project with the Chrome Web Store API enabled, then exchange
an authorisation code (scope https://www.googleapis.com/auth/chromewebstore)
for a refresh token. If the OAuth consent screen is left in "Testing", Google
expires that refresh token after seven days - publish the consent screen.
CWS_ITEM_ID is the extension id from the developer dashboard URL;
CWS_PUBLISHER_ID is on the dashboard's publisher settings page. The item has
to exist already: the first upload of a new item is done in the dashboard.

None of them set means "not configured": skipped, exit 2. Some but not all
set is a mistake, not a choice, and fails.

A dry run signs in and reads the item's status without uploading anything,
which proves the secrets and the ids before a stable tag depends on them.

Exit codes: 0 published (or submitted for review), 2 skipped, 1 failed.

Why v2: Google supports the v1.1 API only until 15 October 2026. This script
was written against v1.1 and had never run, so the first stable tag would have
been its first contact with an API about to be switched off. What is known of
v2 (from Google's reference pages, which this environment could not fetch
whole): the endpoint paths below, the publish request's publishType, the
publish response's `state` (an ItemState such as PENDING_REVIEW or PUBLISHED),
and the upload response's `uploadState`, which fetchStatus reports as
`lastAsyncUploadState` when processing is asynchronous. The exact spelling of
the UploadState values is not confirmed - SUCCEEDED / IN_PROGRESS / FAILED,
possibly prefixed UPLOAD_ - so both spellings are accepted. Nothing here has
been run against the real store; tests/python/test_publish_cws.py runs it
against a stand-in built from the same understanding.

Endpoints can be redirected for tests with CWS_OAUTH_BASE and CWS_API_BASE.
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request, zipfile

REQUIRED = ("CWS_CLIENT_ID", "CWS_CLIENT_SECRET", "CWS_REFRESH_TOKEN",
            "CWS_PUBLISHER_ID", "CWS_ITEM_ID")

TOKEN = "/token"
UPLOAD = "/upload/v2/publishers/{pub}/items/{item}:upload?uploadType=media"
PUBLISH = "/v2/publishers/{pub}/items/{item}:publish"
STATUS = "/v2/publishers/{pub}/items/{item}:fetchStatus"

# A publish that leaves the item in one of these did what was asked. A new
# version normally sits in review; that is success as far as this step goes.
PUBLISHED = {"PENDING_REVIEW", "STAGED", "PUBLISHED", "PUBLISHED_TO_TESTERS"}


class Failed(Exception):
    """A sentence to print before exiting 1."""


def oauth_base():
    return os.environ.get("CWS_OAUTH_BASE") or "https://oauth2.googleapis.com"


def api_base():
    return os.environ.get("CWS_API_BASE") or "https://chromewebstore.googleapis.com"


def creds():
    """(credentials, missing). credentials is None unless all are set."""
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    return ({k: os.environ[k] for k in REQUIRED} if not missing else None), missing


def _explain(body):
    """The human part of a Google error body, whichever of its shapes it is."""
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:500] or "(empty body)"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):                    # Google API: {"error": {...}}
        parts = [err.get("status"), err.get("message")]
        return ": ".join(str(p) for p in parts if p) or json.dumps(err)
    if isinstance(err, str):                     # OAuth: {"error": "invalid_grant"}
        return ": ".join(p for p in (err, data.get("error_description")) if p)
    return json.dumps(data)[:500]


def request(what, method, url, data=None, headers=None, timeout=300):
    """JSON from the store, or Failed naming `what` went wrong and how."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise Failed(f"{what} refused: HTTP {e.code}: "
                     f"{_explain(e.read().decode('utf-8', 'replace'))}") from None
    except (urllib.error.URLError, OSError) as e:
        host = urllib.parse.urlsplit(url).netloc
        reason = getattr(e, "reason", e)
        raise Failed(f"{what}: could not reach {host} ({reason}). Nothing was "
                     f"changed by this step; re-run it.") from None
    try:
        out = json.loads(body)
    except ValueError:
        raise Failed(f"{what}: the store answered with something that is not "
                     f"JSON: {body.strip()[:300]!r}") from None
    if not isinstance(out, dict):
        raise Failed(f"{what}: unexpected answer: {body.strip()[:300]!r}")
    return out


def access_token(c):
    body = urllib.parse.urlencode({
        "client_id": c["CWS_CLIENT_ID"],
        "client_secret": c["CWS_CLIENT_SECRET"],
        "refresh_token": c["CWS_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    try:
        tok = request("signing in to Google", "POST", oauth_base() + TOKEN, body,
                      {"Content-Type": "application/x-www-form-urlencoded"}, timeout=60)
    except Failed as e:
        hint = ""
        if "invalid_grant" in str(e):
            hint = (" CWS_REFRESH_TOKEN has expired or been revoked; mint a new one "
                    "(and publish the OAuth consent screen, or it will expire again "
                    "in seven days).")
        elif "invalid_client" in str(e) or "unauthorized_client" in str(e):
            hint = " CWS_CLIENT_ID / CWS_CLIENT_SECRET are not a valid OAuth client."
        raise Failed(str(e) + hint) from None
    if not tok.get("access_token"):
        raise Failed(f"signing in to Google returned no access_token: {json.dumps(tok)[:300]}")
    return tok["access_token"]


def _state(value):
    """SUCCEEDED from either SUCCEEDED or UPLOAD_SUCCEEDED; see the docstring."""
    s = str(value or "").upper()
    return s[len("UPLOAD_"):] if s.startswith("UPLOAD_") else s


def _details(resp):
    """Whatever a failed upload says about why, readably."""
    lines = []
    for e in resp.get("itemError") or []:
        lines.append(f"  {e.get('error_code', '?')}: {e.get('error_detail', '')}".rstrip())
    if not lines:
        rest = {k: v for k, v in resp.items() if k not in ("name", "itemId", "kind")}
        lines.append("  " + json.dumps(rest))
    return "\n".join(lines)


def manifest_version(path):
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("manifest.json").decode("utf-8")).get("version")
    except (KeyError, ValueError, zipfile.BadZipFile) as e:
        raise Failed(f"{path} is not an extension bundle with manifest.json at its "
                     f"root ({e}); the store would refuse it.") from None


def wait_for_upload(c, auth, poll_interval, poll_limit):
    """Poll an upload the store is processing asynchronously until it settles."""
    url = api_base() + STATUS.format(pub=c["CWS_PUBLISHER_ID"], item=c["CWS_ITEM_ID"])
    for _ in range(poll_limit):
        time.sleep(poll_interval)
        st = request("checking the upload", "GET", url, headers=auth, timeout=60)
        state = _state(st.get("lastAsyncUploadState") or st.get("uploadState"))
        if state != "IN_PROGRESS":
            return state, st
    raise Failed(f"the store was still processing the upload after "
                 f"{poll_limit * poll_interval:.0f}s. Nothing was published; check the "
                 f"item in the developer dashboard, then re-run this job.")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--staged", action="store_true",
                    help="stage the version after approval instead of publishing it")
    ap.add_argument("--dry-run", action="store_true",
                    help="sign in and read the item, but upload and publish nothing")
    a = ap.parse_args(argv)

    c, missing = creds()
    if not c:
        if len(missing) == len(REQUIRED):
            print("chrome web store: skipped, no credentials configured")
            return 2
        print(f"chrome web store: half configured - missing {', '.join(missing)}. "
              f"Set all of {', '.join(REQUIRED)} as repository secrets, or none of "
              f"them to skip the store.", file=sys.stderr)
        return 1

    poll_interval = float(os.environ.get("CWS_POLL_INTERVAL") or 5)
    poll_limit = int(os.environ.get("CWS_POLL_LIMIT") or 60)
    pub, item = c["CWS_PUBLISHER_ID"], c["CWS_ITEM_ID"]

    try:
        version = manifest_version(a.zip)
        payload = open(a.zip, "rb").read()
        print(f"chrome web store: item {item}, version {version}, "
              f"{len(payload):,} bytes from {a.zip}")

        token = access_token(c)
        auth = {"Authorization": f"Bearer {token}"}

        if a.dry_run:
            st = request("reading the item", "GET",
                         api_base() + STATUS.format(pub=pub, item=item), headers=auth,
                         timeout=60)
            print(f"chrome web store: signed in; item status {json.dumps(st)}")
            print("chrome web store: dry run, nothing uploaded")
            return 0

        up = request("uploading", "POST", api_base() + UPLOAD.format(pub=pub, item=item),
                     payload, {**auth, "Content-Type": "application/zip"})
        state = _state(up.get("uploadState"))
        if state == "IN_PROGRESS":
            print("chrome web store: upload accepted, waiting for the store to process it")
            state, up = wait_for_upload(c, auth, poll_interval, poll_limit)
        print(f"chrome web store: upload {state or '(no state)'}")
        if state not in ("SUCCEEDED", "SUCCESS"):
            raise Failed(f"the store rejected the upload ({state or 'no uploadState'}):\n"
                         f"{_details(up)}\nNothing was published. Fix what it names, "
                         f"then cut a new tag.")
        if up.get("crxVersion") and up["crxVersion"] != version:
            raise Failed(f"the store reports version {up['crxVersion']} for the upload, "
                         f"but {a.zip} is {version}. Nothing was published.")

        pub_resp = request(
            "publishing", "POST", api_base() + PUBLISH.format(pub=pub, item=item),
            json.dumps({"publishType": "STAGED_PUBLISH" if a.staged
                        else "DEFAULT_PUBLISH"}).encode(),
            {**auth, "Content-Type": "application/json"})
        result = str(pub_resp.get("state") or "")
        print(f"chrome web store: publish {result or '(no state)'}")
        if pub_resp.get("warningInfo"):
            # Its shape is not documented anywhere this was written from, so
            # it is shown whole rather than picked apart.
            print(f"  warnings: {json.dumps(pub_resp['warningInfo'])}")
        if result not in PUBLISHED:
            raise Failed(f"the store did not accept the version for publishing "
                         f"(state {result or 'missing'}): {json.dumps(pub_resp)[:500]}. "
                         f"The upload is in the item's draft; check the developer "
                         f"dashboard.")
        return 0
    except Failed as e:
        print(f"chrome web store: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"chrome web store: cannot read {a.zip}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
