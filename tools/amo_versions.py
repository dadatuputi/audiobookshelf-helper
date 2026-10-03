#!/usr/bin/env python3
"""
Ask AMO which versions of the add-on it already has, before trying to add one.

AMO's version space is append-only: it will not sign a version number twice,
and deleting a GitHub release does not give the number back. So re-pushing a
tag over a new commit asks AMO for something it can never do, and the release
job used to find that out the expensive way - a full verify, a package, a
rebuild and a multi-minute signing round-trip, ending in a Conflict.

Asking first turns that into one request. It is a pre-flight check, so it
fails closed only on a definite answer: if AMO says the version is there, the
tag has to move. Anything else - no credentials, an add-on AMO has never seen,
a network or API error - is not evidence of a conflict, so it reports what
happened and lets the real submission be the judge.

    AMO_JWT_ISSUER=... AMO_JWT_SECRET=... python3 tools/amo_versions.py --list
    ... python3 tools/amo_versions.py --conflicts 1.0.0.3   # exit 1 if taken
"""
import argparse, base64, hashlib, hmac, json, os, sys, time, urllib.error, urllib.request

API = os.environ.get("AMO_API", "https://addons.mozilla.org/api/v5")

# Unlisted versions are the ones a prerelease creates, and they are invisible
# to the default filter - which would make every alpha look available.
#
# What is known and what is not, since only the unlisted path has run:
#
# - Known, from AMO's API documentation: with no filter the list holds only
#   public versions, so a listed version still awaiting review - the state a
#   stable tag leaves behind - would be invisible and look free.
#   all_with_unlisted is documented as "all versions (including unlisted)" and
#   needs a developer's credentials, which these are. So the same query covers
#   the listed channel; there is no listed-only filter to switch to.
# - Known gap: deleted versions. Only all_with_deleted shows them, and that
#   needs admin rights. AMO is believed to refuse a deleted version's number as
#   well, so a tag whose version was signed and then deleted on AMO passes this
#   check and is refused by the submission instead. Whether that refusal says
#   "already exists" - which the sign step greps for - has not been seen.
# - Unverified: that rejected or disabled listed versions appear under
#   all_with_unlisted. The documentation says "all"; it has not been observed.
VERSIONS = "/addons/addon/{addon}/versions/?filter=all_with_unlisted"


class Undetermined(Exception):
    """AMO did not answer the question. Not the same as "no conflict"."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def jwt(issuer: str, secret: str, lifetime=60) -> str:
    """An AMO API token.

    Hand-rolled rather than pulling in PyJWT: it is two base64 segments and
    one HMAC, and this runs in a release job where every dependency is another
    thing that can break a release.
    """
    now = int(time.time())
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    # jti makes the token single-use, which AMO requires; exp must be close -
    # AMO rejects anything more than five minutes out.
    body = _b64(json.dumps({
        "iss": issuer,
        "jti": _b64(os.urandom(16)),
        "iat": now,
        "exp": now + lifetime,
    }).encode())
    signing_input = f"{head}.{body}".encode()
    sig = _b64(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def _get(url: str, token: str, timeout=30) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"JWT {token}",
        "Accept": "application/json",
        "User-Agent": "audiobookshelf-helper-release",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def versions(addon: str, issuer: str, secret: str) -> list:
    """Every version AMO holds for this add-on, listed and unlisted."""
    if not issuer or not secret:
        raise Undetermined("AMO credentials are not set")
    url = API + VERSIONS.format(addon=addon)
    found, seen = [], 0
    while url:
        try:
            # A fresh token per page: jti is single-use and a page can take
            # long enough for a 60s token to expire mid-walk.
            page = _get(url, jwt(issuer, secret))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Never submitted. Nothing is taken, which is a real answer.
                return []
            raise Undetermined(f"AMO returned HTTP {e.code}")
        except Exception as e:
            raise Undetermined(f"could not reach AMO: {e}")
        found += [v["version"] for v in page.get("results", []) if v.get("version")]
        url = page.get("next")
        seen += 1
        if seen > 20:                   # a runaway "next" is not worth hanging on
            break
    return found


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--addon", help="defaults to geckoId from extension/identity.json")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="print the versions AMO has")
    g.add_argument("--conflicts", metavar="VERSION",
                   help="exit 1 if AMO already has this version")
    args = p.parse_args(argv)

    addon = args.addon
    if not addon:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "extension", "identity.json")) as f:
            addon = json.load(f)["geckoId"]

    try:
        have = versions(addon, os.environ.get("AMO_JWT_ISSUER", ""),
                        os.environ.get("AMO_JWT_SECRET", ""))
    except Undetermined as e:
        # Not a conflict, so not a failure: say so and let the submission decide.
        print(f"could not check AMO ({e}); proceeding", file=sys.stderr)
        return 0

    if args.list:
        print("\n".join(have) if have else "(AMO has no versions of this add-on)")
        return 0

    if args.conflicts in have:
        print(f"AMO already has version {args.conflicts}", file=sys.stderr)
        return 1
    print(f"AMO does not have version {args.conflicts} "
          f"({len(have)} existing)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
