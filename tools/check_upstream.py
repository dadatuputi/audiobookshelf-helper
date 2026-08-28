#!/usr/bin/env python3
"""
Watch Audiobookshelf for releases that might break this extension.

The failure mode being guarded against is quiet: if ABS renames a metadata
field or stops accepting ?token=, the extension shows an empty book list or a
download that fails, with nothing pointing at the cause. Upstream has no reason
to consider us, so the only defence is noticing when they ship.

    python3 tools/check_upstream.py                 # report to stdout
    python3 tools/check_upstream.py --state .github/abs-watch.json --update

Writes a markdown report to --output when there is something new to say, and
exits 0 either way. GITHUB_TOKEN is used if present, purely for rate limits.
"""
import argparse, json, os, re, sys, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests" / "fixtures" / "abs" / "contract.json"
API = "https://api.github.com/repos/{repo}/releases?per_page=10"

# Words in a changelog that plausibly touch what we depend on. Deliberately
# wide: a false positive costs a glance, a false negative costs a silent break.
INTERESTING = re.compile(
    r"\b(api|endpoint|token|auth|jwt|permission|apikey|api key|library items?|"
    r"libraries|download|zip|metadata|relpath|numtracks|audiofiles|series|"
    r"authorname|breaking|migrat)", re.I)


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


def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"lastSeen": None}


def relevant_lines(body):
    """The changelog lines worth a human's attention."""
    out = []
    for line in (body or "").splitlines():
        s = line.strip(" -*\t")
        if s and INTERESTING.search(s):
            out.append(s)
    return out[:25]


def build_report(new_releases, contract):
    latest = new_releases[0]
    lines = [
        f"Audiobookshelf published **{latest['tag_name']}**"
        f"{' and ' + str(len(new_releases) - 1) + ' other release(s)' if len(new_releases) > 1 else ''}"
        " since this was last checked.",
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

    lines += [
        "### If something did change",
        "",
        "`tests/js/contract.test.js` asserts these shapes against recorded"
        " fixtures in `tests/fixtures/abs/`. Re-record the fixtures from the new"
        " server and fix `extension/src/lib.js` until they pass — do not loosen"
        " the assertions.",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(ROOT / ".github" / "abs-watch.json"))
    ap.add_argument("--output", default="upstream-report.md")
    ap.add_argument("--update", action="store_true", help="record what was seen")
    a = ap.parse_args()

    contract = json.loads(CONTRACT.read_text())
    repo = contract["upstream"]
    state = load_state(a.state)

    try:
        releases = [r for r in fetch(API.format(repo=repo)) if not r.get("draft")]
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        # A watcher that fails the build when GitHub hiccups is a watcher that
        # gets turned off. Report and move on.
        print(f"::warning title=upstream watch::could not reach GitHub: {e}")
        return 0

    if not releases:
        print("no releases found upstream")
        return 0

    last = state.get("lastSeen")
    if last is None:
        new = []          # first run: record where we are, do not shout
    else:
        new = []
        for r in releases:
            if r["tag_name"] == last:
                break
            new.append(r)

    latest_tag = releases[0]["tag_name"]
    print(f"upstream latest: {latest_tag}; last seen: {last}; new: {len(new)}")

    if new:
        Path(a.output).write_text(build_report(new, contract) + "\n")
        print(f"wrote {a.output}")

    if a.update:
        Path(a.state).write_text(json.dumps(
            {"upstream": repo, "lastSeen": latest_tag,
             "lastCheckedRelease": releases[0]["html_url"]}, indent=2) + "\n")

    # Consumed by the workflow to decide whether to open an issue.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"has_news={'true' if new else 'false'}\n")
            fh.write(f"latest={latest_tag}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
