#!/usr/bin/env python3
"""
Check that what a release actually published matches the tag it is for.

Packaging is tested; publishing was not, and both release failures of the
alpha cycle lived there. A re-cut tag got new zips beside an xpi from the
previous build, both labelled the same version, and nothing noticed until a
person looked. Every job that touches the release can report success while
the release itself is wrong, so this asks GitHub what it is now serving and
compares that with what this run built.

    python3 tools/verify_release.py --tag v1.0.0-alpha.2 --artifacts release \\
        --amo-result success --amo-attempted true --amo-channel unlisted

It checks:

  - a release exists for the tag, is not a draft, and is marked prerelease
    exactly when release_version.py says the tag is one;
  - the assets are exactly the ones the tag should carry (ASSET RULES below),
    and nothing named for another version or left under AMO's internal name;
  - each asset this run built is published byte-for-byte: GitHub's digest
    equals the sha256 of the file in --artifacts;
  - the native helper inside that zip is stamped with the tag's version, and
    the extension zips carry the store versions for it;
  - a signed xpi, when there is one, is signed, for this add-on, at this
    version, and was uploaded after this build's zips.

Every problem is reported, not just the first, each with what to do about it.
Exit 0 when the release matches, 1 when it does not or could not be read.

Standard library only. The API base comes from GITHUB_API_URL (which Actions
sets) so tests can point it at a stand-in; the token from GH_TOKEN or
GITHUB_TOKEN, sent to the API only - never to the asset download host.
"""
import argparse, hashlib, importlib.util, io, json, os, re, sys, urllib.error, urllib.parse, urllib.request, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "dadatuputi/audiobookshelf-helper"
PROJECT = "audiobookshelf-helper"

_spec = importlib.util.spec_from_file_location("relver", ROOT / "tools" / "release_version.py")
RV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RV)


def api_base():
    return os.environ.get("GITHUB_API_URL") or "https://api.github.com"


# --- ASSET RULES -------------------------------------------------------------
#
# The one place that says what a release may carry. Anything not named here is
# reported as foreign. A rule is (need, after):
#
#   need   REQUIRED - must be published. OPTIONAL - allowed, not required.
#          An OPTIONAL asset that this run built (it is in --artifacts) is
#          promoted to REQUIRED: if the build made it, the release must carry
#          it, and carry these bytes.
#   after  another asset this one must have been uploaded after. Some assets
#          are made from others later in the pipeline, so one older than its
#          source was made from a previous build's source.

REQUIRED, OPTIONAL = "required", "optional"
ZIP_KINDS = ("chrome", "firefox", "native", "source")


def zip_name(kind, semver):
    return f"{PROJECT}-{kind}-{semver}.zip"


def xpi_name(semver):
    return f"{PROJECT}-firefox-{semver}.xpi"


def asset_rules(semver, xpi_need):
    rules = {zip_name(k, semver): (REQUIRED, None) for k in ZIP_KINDS}
    # A checksum manifest the build may emit, and a signature over it that
    # the maintainer uploads by hand afterwards. A signature older than the
    # manifest signs a previous build's manifest.
    rules["SHA256SUMS"] = (OPTIONAL, None)
    rules["SHA256SUMS.sig"] = (OPTIONAL, "SHA256SUMS")
    if xpi_need:
        # The amo job runs after github-release, and the replace path drops
        # any xpi already on the tag, so a genuine one always postdates this
        # build's zips. One that does not is the alpha.1 failure exactly.
        rules[xpi_name(semver)] = (xpi_need, zip_name("firefox", semver))
    return rules


def xpi_expectation(result, attempted, channel):
    """Whether the release should carry a signed xpi, from what the amo job did.

    Returns (need, why, warning, problem). need is REQUIRED, OPTIONAL or None
    (forbidden). The inputs are the amo job's result and its sign step's
    outputs; when they do not say what happened, this says so rather than
    guessing, because a verifier that passes by assuming is worse than none.
    """
    if result == "success":
        if attempted == "false":
            return (None, "no AMO credentials are configured, so nothing was signed",
                    None, None)
        if attempted == "true" and channel == "unlisted":
            return (REQUIRED, "AMO signed this prerelease on the unlisted channel, "
                    "which hands the xpi back in the same run", None, None)
        if attempted == "true" and channel == "listed":
            # Signed after human review, and published by AMO itself.
            return (OPTIONAL, "a listed submission is signed after review, "
                    "not in this run", None, None)
        return (OPTIONAL, "", None,
                f"the amo job succeeded but its sign step reported "
                f"attempted={attempted!r} channel={channel!r}, which is not an "
                f"outcome it can produce. The outputs wiring between the amo job "
                f"and this one is broken (release.yml: amo.outputs, and this "
                f"job's env); fix it, or this check cannot tell whether an xpi "
                f"belongs here.")
    return (OPTIONAL, "", (
        f"the amo job ended '{result or 'unknown'}', so whether this release "
        f"should carry a signed xpi is not known. An xpi is accepted if present "
        f"and correct, and not required. See the amo job for what happened."),
        None)


def classify_foreign(name, semver):
    """Why an asset with no rule is there, as far as its name says."""
    m = re.match(rf"^{re.escape(PROJECT)}-(chrome|firefox|native|source)-(.+)\.(zip|xpi)$", name)
    if m and m.group(2) != semver:
        return (f"is named for version {m.group(2)}, not {semver}: it was left "
                f"by another build")
    if re.match(r"^[0-9a-f]{8,}-\d+(\.\d+)*\.xpi$", name):
        return ("is named by AMO's internal id, so the rename in the amo job "
                "never ran on it: it was left by another build")
    return "is not an asset this release produces"


# --- GitHub ------------------------------------------------------------------

class Unreadable(Exception):
    pass


def _get(url, auth=True, timeout=60):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "audiobookshelf-helper-release-verify"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if auth and token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_release(repo, tag):
    """The release object, or None if GitHub has none for the tag."""
    url = f"{api_base()}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    try:
        return json.loads(_get(url).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise Unreadable(f"GitHub returned HTTP {e.code} for {url}")
    except Exception as e:
        raise Unreadable(f"could not reach GitHub: {e}")


# --- checks ------------------------------------------------------------------

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_zip_text(path, member):
    with zipfile.ZipFile(path) as z:
        return z.read(member).decode("utf-8")


def check_contents(info, artifacts, problems, notes):
    """What is inside the archives, which the digest ties to what is published."""
    semver = info["semver"]
    native = artifacts / zip_name("native", semver)
    if native.is_file():
        try:
            text = read_zip_text(native, "absh/version.py")
        except (KeyError, zipfile.BadZipFile) as e:
            problems.append(f"{native.name} has no readable absh/version.py ({e}). "
                            f"tools/package.py did not package the helper; fix the "
                            f"build, then cut a new tag.")
        else:
            m = re.search(r'^RELEASE\s*=\s*"([^"]*)"', text, re.M)
            got = m.group(1) if m else None
            if got != semver:
                problems.append(
                    f"{native.name} carries absh/version.py with RELEASE={got!r}, "
                    f"not {semver!r}. `absh update` would install it and then "
                    f"offer the same update forever. tools/package.py's "
                    f"stamp_version did not take; fix it, then cut a new tag.")
            else:
                notes.append(f"native helper is stamped {semver}")
    for kind in ("firefox", "chrome"):
        z = artifacts / zip_name(kind, semver)
        if not z.is_file():
            continue
        try:
            manifest = json.loads(read_zip_text(z, "manifest.json"))
        except (KeyError, ValueError, zipfile.BadZipFile) as e:
            problems.append(f"{z.name} has no readable manifest.json at its root ({e}); "
                            f"the store will refuse it. Fix tools/package.py, then cut a new tag.")
            continue
        if manifest.get("version") != info[kind]:
            problems.append(f"{z.name} has manifest version {manifest.get('version')!r}, "
                            f"but {info['tag']} maps to {info[kind]!r} for {kind}. "
                            f"Fix tools/package.py, then cut a new tag.")
        else:
            notes.append(f"{kind} manifest is {info[kind]}")


def check_xpi(asset, info, problems, notes):
    """Download the published xpi and look inside: right add-on, right version,
    actually signed. Its bytes are not in this run's artefacts - AMO made them -
    so this is the only way to know what it is."""
    before = len(problems)
    url = asset.get("browser_download_url")
    try:
        blob = _get(url, auth=False, timeout=120)
    except Exception as e:
        problems.append(f"could not download {asset['name']} to check it ({e}). "
                        f"Re-run this job; if it persists, check the asset by hand.")
        return
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json").decode("utf-8"))
    except (KeyError, ValueError, zipfile.BadZipFile) as e:
        problems.append(f"{asset['name']} is not a readable add-on ({e}). Delete it: "
                        f"gh release delete-asset {info['tag']} {asset['name']} --yes")
        return
    ident = json.loads((ROOT / "extension" / "identity.json").read_text())
    gecko = (manifest.get("browser_specific_settings") or {}).get("gecko") or {}
    if gecko.get("id") != ident["geckoId"]:
        problems.append(f"{asset['name']} is for add-on {gecko.get('id')!r}, not "
                        f"{ident['geckoId']!r}. Delete it: gh release delete-asset "
                        f"{info['tag']} {asset['name']} --yes")
    if manifest.get("version") != info["firefox"]:
        problems.append(f"{asset['name']} is add-on version {manifest.get('version')!r}, "
                        f"but {info['tag']} maps to {info['firefox']!r}. It is from another "
                        f"build. Delete it: gh release delete-asset {info['tag']} "
                        f"{asset['name']} --yes")
    # AMO's signature: the COSE pair since 2019, the PKCS#7 one before it, and
    # currently both. Either is evidence it went through signing at all.
    if not names & {"META-INF/cose.sig", "META-INF/mozilla.rsa"}:
        problems.append(f"{asset['name']} carries no AMO signature (no META-INF/cose.sig "
                        f"or mozilla.rsa), so Firefox will refuse to install it. Delete "
                        f"it: gh release delete-asset {info['tag']} {asset['name']} --yes")
    if len(problems) == before:
        notes.append(f"{asset['name']} is signed, for {ident['geckoId']}, at {info['firefox']}")


def verify(tag, repo, artifacts, amo_result, amo_attempted, amo_channel):
    """Returns (problems, warnings, notes). Empty problems means the release matches."""
    problems, warnings, notes = [], [], []
    info = RV.parse_tag(tag)
    semver = info["semver"]
    artifacts = Path(artifacts)

    xpi_need, why, warn, wiring = xpi_expectation(amo_result, amo_attempted, amo_channel)
    if warn:
        warnings.append(warn)
    if wiring:
        problems.append(wiring)
    rules = asset_rules(semver, xpi_need)
    if why:
        notes.append(f"xpi {'not expected' if xpi_need is None else xpi_need}: {why}")

    try:
        rel = fetch_release(repo, tag)
    except Unreadable as e:
        problems.append(f"{e}. Nothing was checked; re-run this job.")
        return problems, warnings, notes
    if rel is None:
        problems.append(f"GitHub has no published release for {tag} in {repo}. The "
                        f"github-release job reported success without one; check its "
                        f"log, then re-run it.")
        return problems, warnings, notes

    if rel.get("draft"):
        problems.append(f"the release for {tag} is a draft, so nobody can see it. "
                        f"Publish it: gh release edit {tag} --draft=false")
    if bool(rel.get("prerelease")) != info["prerelease"]:
        want = "a prerelease" if info["prerelease"] else "not a prerelease"
        problems.append(f"the release for {tag} is marked prerelease="
                        f"{str(bool(rel.get('prerelease'))).lower()}, but {tag} is {want}. "
                        f"Fix it: gh release edit {tag} "
                        f"--prerelease={str(info['prerelease']).lower()}")

    assets = {a["name"]: a for a in rel.get("assets", [])}

    for name in sorted(assets):
        if name not in rules:
            if name == xpi_name(semver) and why:
                reason = f"is attached, but {why}: it was left by another build"
            else:
                reason = classify_foreign(name, semver)
            problems.append(f"{name} {reason}. Delete it: "
                            f"gh release delete-asset {tag} {name} --yes")

    for name, (need, after) in sorted(rules.items()):
        local = artifacts / name
        built = local.is_file()
        a = assets.get(name)
        if a is None:
            if need == REQUIRED or built:
                if name.endswith(".xpi"):
                    why_needed = why
                elif built and need != REQUIRED:
                    why_needed = "this run built it"
                else:
                    why_needed = "every release carries it"
                fix = ("the amo job's attach step did not upload it; see its log"
                       if name.endswith(".xpi") else
                       "re-run the github-release job, which uploads with --clobber")
                problems.append(f"{name} is missing from the release ({why_needed}). "
                                f"Fix: {fix}.")
            continue
        if a.get("state", "uploaded") != "uploaded":
            problems.append(f"{name} is published in state {a.get('state')!r}: its upload "
                            f"never completed. Delete it (gh release delete-asset {tag} "
                            f"{name} --yes) and re-run the job that uploads it.")
            continue
        if after and after in assets:
            if (a.get("updated_at") or "") < (assets[after].get("updated_at") or ""):
                problems.append(
                    f"{name} was uploaded at {a.get('updated_at')}, before {after} "
                    f"({assets[after].get('updated_at')}), so it was made from a "
                    f"previous build's {after} and does not belong to this one. Delete "
                    f"it: gh release delete-asset {tag} {name} --yes")
                continue
        if built:
            published = (a.get("digest") or "").lower()
            mine = sha256(local)
            if not published.startswith("sha256:"):
                problems.append(f"GitHub reports no sha256 digest for {name} "
                                f"(got {a.get('digest')!r}), so there is no way to tell "
                                f"it is this build's. `absh update` relies on that "
                                f"digest too. Re-run the github-release job.")
            elif published != f"sha256:{mine}":
                problems.append(f"{name} on the release has {published}, but this run "
                                f"built sha256:{mine}: the release is serving bytes from "
                                f"another build. Re-run the github-release job, which "
                                f"uploads with --clobber.")
            else:
                notes.append(f"{name} matches this build ({mine[:12]})")
        elif need == REQUIRED and not name.endswith(".xpi"):
            problems.append(f"{name} is required but is not in the build artefacts at "
                            f"{artifacts}, so its bytes cannot be checked. The "
                            f"release-artifacts download is incomplete; re-run this job.")
        if name.endswith(".xpi"):
            check_xpi(a, info, problems, notes)

    check_contents(info, artifacts, problems, notes)
    return problems, warnings, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO)
    ap.add_argument("--artifacts", required=True,
                    help="directory holding exactly what this run built (release-artifacts)")
    ap.add_argument("--amo-result", default="",
                    help="the amo job's result: success, failure, cancelled, skipped")
    ap.add_argument("--amo-attempted", default="", help="the sign step's attempted output")
    ap.add_argument("--amo-channel", default="", help="the sign step's channel output")
    a = ap.parse_args(argv)

    try:
        RV.parse_tag(a.tag)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    problems, warnings, notes = verify(a.tag, a.repo, a.artifacts, a.amo_result,
                                       a.amo_attempted, a.amo_channel)
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for n in notes:
        print(f"ok: {n}")
    for w in warnings:
        print(f"::warning title=release {a.tag}::{w}" if actions else f"warning: {w}")
    for p in problems:
        print(f"::error title=release {a.tag}::{p}" if actions else f"error: {p}")
    if problems:
        print(f"\n{a.tag}: {len(problems)} problem(s); the published release does not "
              f"match the tag.", file=sys.stderr)
        return 1
    print(f"\n{a.tag}: the published release matches the tag.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
