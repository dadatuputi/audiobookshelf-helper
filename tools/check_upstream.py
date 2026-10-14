#!/usr/bin/env python3
"""
Watch Audiobookshelf for releases that might break this extension.

The failure mode being guarded against is quiet: if ABS renames a metadata
field or reshapes a book's page, the extension shows an empty book list or a
download that fails, with nothing pointing at the cause. Upstream has no reason
to consider us, so the only defence is noticing when they ship.

    python3 tools/check_upstream.py                    # anything newer than the newest supported?
    python3 tools/check_upstream.py --set-newest v2.38.0   # it passed; support it

The supported range is in extension/abs-versions.json, and its "newest" is
the only record of what has been seen: a release newer than it is news. (This
used to keep its own record, which the workflow only saved when there was news
- so it never saved one, and never reported a release.)
.github/workflows/upstream.yml runs the full suite against what this finds,
and moves "newest" when it passes. "oldest" is never moved from here.

Writes a markdown report to --output when there is something newer, and exits
0 either way. GITHUB_TOKEN is used if present, purely for rate limits.
"""
import argparse, json, os, re, sys, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests" / "fixtures" / "abs" / "contract.json"
VERSIONS = ROOT / "extension" / "abs-versions.json"
API = "https://api.github.com/repos/{repo}/releases?per_page=10"

# Words in a changelog that plausibly touch what we depend on. Deliberately
# wide: a false positive costs a glance, a false negative costs a silent break.
INTERESTING = re.compile(
    r"\b(api|endpoint|token|auth|jwt|permission|apikey|api key|library items?|"
    r"libraries|download|zip|metadata|relpath|numtracks|audiofiles|series|"
    r"authorname|narrator|duration|breaking|migrat|"
    r"book ?cards?|cover ?area|item page|play button|toolbar|router|nuxt|vue|"
    r"personalized|server ?version|upload|base ?path)|/status\b", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "audiobookshelf-helper-upstream-watch",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def version_key(tag):
    """(2, 37, 1) for "v2.37.1"; None for anything that is not a plain release."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(tag or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer_than(releases, pinned):
    """Published, non-pre releases newer than `pinned`, newest first."""
    floor = version_key(pinned) or (0, 0, 0)
    out = [r for r in releases
           if not r.get("draft") and not r.get("prerelease")
           and (version_key(r.get("tag_name")) or (0, 0, 0)) > floor]
    return sorted(out, key=lambda r: version_key(r["tag_name"]), reverse=True)


def relevant_lines(body):
    """The changelog lines worth a human's attention."""
    out = []
    for line in (body or "").splitlines():
        s = line.strip(" -*\t")
        if s and INTERESTING.search(s):
            out.append(s)
    return out[:25]


def build_report(new_releases, contract, pinned=None):
    latest = new_releases[0]
    lines = [
        f"Audiobookshelf published **{latest['tag_name']}**"
        f"{' and ' + str(len(new_releases) - 1) + ' other release(s)' if len(new_releases) > 1 else ''}"
        f" since {pinned or 'the version the tests run against'}.",
        "",
        "This is a heads-up, not a known breakage. The extension talks to a"
        " small part of the ABS API; the parts it depends on are listed below"
        " so you can scan the changelog against them.",
        "",
    ]
    for rel in new_releases:
        lines.append(f"### [{rel['tag_name']}]({rel['html_url']})")
        hits = relevant_lines(rel.get("body"))
        if hits:
            lines.append("")
            lines.append("Changelog lines mentioning something we use:")
            lines += [f"- {h}" for h in hits]
        else:
            lines.append("")
            lines.append("_Nothing in the changelog obviously touches what we use._")
        lines.append("")

    lines += ["---", "", "### What this extension depends on", ""]
    for e in contract["endpoints"]:
        lines.append(f"**`{e['method']} {e['path']}`** — {e['used_for']}")
        lines += [f"  - `{f}`" for f in e["fields"]]
        if e.get("note"):
            lines.append(f"  - _{e['note']}_")
        lines.append("")
    if contract.get("page"):
        lines += ["### What the page buttons depend on", ""]
        for p in contract["page"]:
            lines.append(f"**{p['what']}** — {p['used_for']}")
            lines.append(f"  - {p['relies_on']}")
        lines.append("")

    lines += [
        "### If something did change",
        "",
        "`TestContract` in `tests/python/test_absh.py` asserts the API shapes"
        " against recorded fixtures in `tests/fixtures/abs/`; the failing run's"
        " real-server suite shows what the page lost. Re-record the fixtures"
        " from the new server and fix the code until they pass — do not loosen"
        " the assertions.",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", default=str(VERSIONS),
                    help="the file naming the oldest and newest supported releases")
    ap.add_argument("--output", default="upstream-report.md")
    ap.add_argument("--set-newest", metavar="TAG",
                    help="record TAG as the newest supported release, and stop")
    a = ap.parse_args()

    path = Path(a.versions)
    versions = json.loads(path.read_text())
    if a.set_newest:
        if not version_key(a.set_newest):
            print(f"not a release tag: {a.set_newest}", file=sys.stderr)
            return 2
        versions["newest"] = a.set_newest
        path.write_text(json.dumps(versions, indent=2) + "\n")
        print(f"newest supported: {a.set_newest} (oldest: {versions['oldest']})")
        return 0

    contract = json.loads(CONTRACT.read_text())
    repo = contract["upstream"]
    pinned = versions["newest"]

    try:
        releases = fetch(API.format(repo=repo))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        # A watcher that fails the build when GitHub hiccups is a watcher that
        # gets turned off. Report and move on.
        print(f"::warning title=upstream watch::could not reach GitHub: {e}")
        return 0

    new = newer_than(releases, pinned)
    latest = new[0]["tag_name"] if new else pinned
    print(f"tested against: {pinned}; upstream latest: {latest}; newer: {len(new)}")

    if new:
        Path(a.output).write_text(build_report(new, contract, pinned) + "\n")
        print(f"wrote {a.output}")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"newer={'true' if new else 'false'}\n")
            fh.write(f"latest={latest}\n")
            fh.write(f"pinned={pinned}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
