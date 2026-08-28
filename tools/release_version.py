#!/usr/bin/env python3
"""
Turn a git tag into the version strings each store will actually accept.

The two browsers disagree about what a version may look like, and neither
accepts semver prereleases as written:

  Chrome   1 to 4 dot-separated integers, 0-65535. "1.0.0-alpha.1" is rejected
           outright, so the prerelease is folded into a fourth component.
  Firefox  each dotted part may be <number><string><number>, and a part
           carrying a string sorts *below* the same part without one. So
           1.0.0a1 < 1.0.0, which is the ordering a prerelease should have.

    python3 tools/release_version.py v1.0.0-alpha.1
    python3 tools/release_version.py v1.0.0-alpha.1 --field chrome
"""
import argparse, json, re, sys

TAG = re.compile(r"""
    ^v?
    (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)
    (?:-(?P<kind>alpha|beta|rc)\.?(?P<num>\d+)?)?
    $""", re.X)

# Keeps prereleases ordered among themselves inside Chrome's fourth component,
# and leaves room for a generous number of each.
OFFSET = {"alpha": 0, "beta": 100, "rc": 200}
SUFFIX = {"alpha": "a", "beta": "b", "rc": "rc"}


def parse_tag(tag: str) -> dict:
    m = TAG.match((tag or "").strip())
    if not m:
        raise ValueError(
            f"tag {tag!r} is not vMAJOR.MINOR.PATCH[-alpha|beta|rc[.N]]")
    major, minor, patch = m.group("major"), m.group("minor"), m.group("patch")
    kind, num = m.group("kind"), int(m.group("num") or 1)
    base = f"{major}.{minor}.{patch}"

    if not kind:
        return {"tag": tag, "semver": base, "base": base, "prerelease": False,
                "kind": "", "firefox": base, "chrome": base}

    ordinal = OFFSET[kind] + num
    if ordinal > 65535:
        raise ValueError(f"prerelease number {num} is too large for Chrome")
    return {
        "tag": tag,
        "semver": f"{base}-{kind}.{num}",
        "base": base,
        "prerelease": True,
        "kind": kind,
        # 1.0.0a1 sorts below 1.0.0 for Firefox, which is what we want.
        "firefox": f"{base}{SUFFIX[kind]}{num}",
        # Chrome has no prerelease concept; the fourth part just keeps builds
        # distinct. Prereleases are never uploaded to the Web Store, so the
        # fact that 1.0.0.1 sorts *above* 1.0.0 never affects a real update.
        "chrome": f"{base}.{ordinal}",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--field", help="print just this field")
    a = ap.parse_args()
    try:
        info = parse_tag(a.tag)
    except ValueError as e:
        sys.exit(str(e))
    if a.field:
        v = info[a.field]
        print(json.dumps(v) if isinstance(v, bool) else v)
    else:
        print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
