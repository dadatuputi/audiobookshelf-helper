#!/usr/bin/env python3
"""
Upload and publish a bundle to the Chrome Web Store.

Standard library only - CI should not need a toolchain to ship.

    python3 tools/publish_cws.py --zip release/...-chrome-1.0.0.zip --dry-run

Credentials come from the environment (set them as repository secrets):

    CWS_CLIENT_ID  CWS_CLIENT_SECRET  CWS_REFRESH_TOKEN  CWS_ITEM_ID

Getting them is a one-time chore: create an OAuth client of type "Desktop app"
in a Google Cloud project with the Chrome Web Store API enabled, then exchange
an authorisation code for a refresh token. CWS_ITEM_ID is the extension id from
the developer dashboard URL.

Exit codes: 0 published, 2 skipped (no credentials), 1 failed.
"""
import argparse, json, os, sys, urllib.error, urllib.parse, urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/chromewebstore/v1.1/items/{id}?uploadType=media"
PUBLISH_URL = "https://www.googleapis.com/chromewebstore/v1.1/items/{id}/publish"

REQUIRED = ("CWS_CLIENT_ID", "CWS_CLIENT_SECRET", "CWS_REFRESH_TOKEN", "CWS_ITEM_ID")


def creds():
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    return ({k: os.environ[k] for k in REQUIRED} if not missing else None), missing


def post(url, data, headers=None, method="POST"):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {url.split('?')[0]} failed: {e.code}\n{detail}")
    return json.loads(body) if body.strip().startswith("{") else {"raw": body}


def access_token(c):
    body = urllib.parse.urlencode({
        "client_id": c["CWS_CLIENT_ID"],
        "client_secret": c["CWS_CLIENT_SECRET"],
        "refresh_token": c["CWS_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    tok = post(TOKEN_URL, body,
               {"Content-Type": "application/x-www-form-urlencoded"})
    if "access_token" not in tok:
        raise SystemExit(f"no access_token in token response: {tok}")
    return tok["access_token"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--target", default="default",
                    help="'default' publishes to everyone; 'trustedTesters' to testers only")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    c, missing = creds()
    if not c:
        print(f"chrome web store: skipped, missing {', '.join(missing)}")
        return 2

    payload = open(a.zip, "rb").read()
    print(f"chrome web store: item {c['CWS_ITEM_ID']}, {len(payload):,} bytes from {a.zip}")
    if a.dry_run:
        print("chrome web store: dry run, nothing uploaded")
        return 0

    token = access_token(c)
    auth = {"Authorization": f"Bearer {token}", "x-goog-api-version": "2"}

    up = post(UPLOAD_URL.format(id=c["CWS_ITEM_ID"]), payload,
              {**auth, "Content-Type": "application/zip"}, method="PUT")
    state = up.get("uploadState")
    print(f"chrome web store: upload {state}")
    if state not in ("SUCCESS", "IN_PROGRESS"):
        raise SystemExit(f"upload rejected: {json.dumps(up, indent=2)}")

    pub = post(PUBLISH_URL.format(id=c["CWS_ITEM_ID"]),
               urllib.parse.urlencode({"publishTarget": a.target}).encode(),
               {**auth, "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": "0"})
    print(f"chrome web store: publish {pub.get('status')}")
    # A new listing sits in review; that is success as far as this step goes.
    for note in pub.get("statusDetail", []) or []:
        print(f"  {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
