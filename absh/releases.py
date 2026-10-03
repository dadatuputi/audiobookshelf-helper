"""Which release is the latest one, and whether it is newer than this copy.

GitHub has an endpoint for "the latest release", and it is the wrong answer
here: it skips prereleases by definition, and every release this project has
published so far is one. Asked for the latest, it answers 404, which reads as
"could not reach the release feed" to someone who reached it fine. So this
lists the releases and picks the newest itself.

Which ones count depends on what is installed. A copy of a prerelease is
following prereleases, so it is offered the next one; a copy of a stable
release is offered only stable releases, which is the promise GitHub's own
"latest" makes and the reason someone installs a stable release at all. A copy
with no version ("dev", "unknown") cannot be behind anything; it is told what
the newest release of any kind is, for information.

Comparison is semantic versioning, not string order: alpha.10 comes after
alpha.9, and 1.0.0 after every 1.0.0-something. extension/src/lib.js carries
the same rule for the page, which decides from a cached answer whether to say
an update is waiting.
"""
import json
import re
import urllib.request

from . import update as update_mod
from . import version as version_mod

_SEMVER = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")

# What a tag passed in from outside may look like before it is put in a URL.
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def parse(v):
    """(major, minor, patch, prerelease identifiers) or None if not a version.

    None is the honest answer for "dev" and "unknown": they are not older or
    newer than anything, and treating them as 0.0.0 would offer a checkout an
    "update" to a release older than the code in it.
    """
    m = _SEMVER.match(str(v or "").strip())
    if not m:
        return None
    pre = tuple(int(p) if p.isdigit() else p for p in m.group(4).split(".")) \
        if m.group(4) else ()
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre


def _cmp_pre(a, b):
    # No prerelease outranks any prerelease: 1.0.0-rc.1 < 1.0.0.
    if not a or not b:
        return (not a) - (not b)
    for x, y in zip(a, b):
        if x == y:
            continue
        xn, yn = isinstance(x, int), isinstance(y, int)
        if xn and yn:
            return -1 if x < y else 1
        if xn != yn:
            return -1 if xn else 1      # numeric identifiers sort first
        return -1 if x < y else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare(a, b):
    """-1, 0 or 1 as a is older, the same as, or newer than b; None if either
    is not a version at all."""
    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        return None
    if pa[:3] != pb[:3]:
        return -1 if pa[:3] < pb[:3] else 1
    return _cmp_pre(pa[3], pb[3])


def newer(candidate, installed):
    """Whether installing `candidate` would move this copy forward."""
    return compare(candidate, installed) == 1


def is_prerelease(v):
    p = parse(v)
    return bool(p and p[3])


def valid_tag(tag):
    return isinstance(tag, str) and bool(_TAG.match(tag))


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"absh/{version_mod.release()}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def latest(installed):
    """The newest release this copy should be offered, as find_release has it.

    Two requests: the listing, to choose, and the chosen release by name, so
    that what is reported has passed the same checks an update would make -
    a release with no helper archive in it is not "available".
    """
    # Read at call time, so ABSH_UPDATE_API and a test pointing update.API at
    # a local feed reach this request as well as the ones update.py makes.
    url = f"{update_mod.API}/repos/{update_mod.REPO}/releases?per_page=50"
    try:
        listing = _get_json(url)
    except Exception as e:                      # network, HTTP error, bad JSON
        raise update_mod.UpdateError(f"could not reach the release feed: {e}")
    if not isinstance(listing, list):
        raise update_mod.UpdateError("the release feed answered with something "
                                     "that is not a list of releases")

    stable_only = parse(installed) is not None and not is_prerelease(installed)
    best = None
    for rel in listing:
        tag = rel.get("tag_name") if isinstance(rel, dict) else None
        if not tag or rel.get("draft") or parse(tag) is None:
            continue
        if stable_only and (rel.get("prerelease") or is_prerelease(tag)):
            continue
        if best is None or compare(tag, best) == 1:
            best = tag
    if best is None:
        raise update_mod.UpdateError(
            "no stable release has been published yet" if stable_only
            else "no release has been published yet")
    return update_mod.find_release(best)
