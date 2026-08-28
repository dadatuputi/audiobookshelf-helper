#!/usr/bin/env python3
"""
Build per-browser extension bundles from a single source tree.

Firefox and Chrome disagree on two MV3 details:

  background   Firefox 115+ wants  {"scripts": [...]}
               Chrome         wants {"service_worker": "..."}
  id           Firefox needs browser_specific_settings.gecko.id for
               native messaging allowed_extensions; Chrome derives an id
               from the key/CRX instead.

Everything else is shared. Usage:

    python3 build.py                 # -> dist/firefox, dist/chrome
    python3 build.py --target chrome
"""
import argparse, json, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
DIST = HERE / "dist"

GECKO_ID = "abs-helper@local"

SHARED_FILES = [
    "background.js", "browser-polyfill.js", "lib.js", "content.js", "content.css",
    "popup.html", "popup.css", "popup.js", "options.html", "options.js",
]


def manifest_for(target: str) -> dict:
    m = json.loads((SRC / "manifest.base.json").read_text())
    if target == "firefox":
        m["background"] = {"scripts": ["browser-polyfill.js", "lib.js", "background.js"]}
        m["browser_specific_settings"] = {
            "gecko": {"id": GECKO_ID, "strict_min_version": "115.0"}
        }
    elif target == "chrome":
        # MV3 service workers are modules; importScripts is unavailable, so the
        # shim is pulled in with a static import from background.js instead.
        m["background"] = {"service_worker": "background.js", "type": "module"}
        m["minimum_chrome_version"] = "112"
    else:
        raise SystemExit(f"unknown target {target!r}")
    return m


def build(target: str) -> Path:
    out = DIST / target
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for name in SHARED_FILES:
        src = SRC / name
        if not src.exists():
            raise SystemExit(f"missing source file: {src}")
        shutil.copy2(src, out / name)

    if target == "chrome":
        # background.js is loaded as an ES module here, so it must import the
        # shim itself rather than rely on a second "scripts" entry.
        bg = out / "background.js"
        bg.write_text('import "./browser-polyfill.js";\nimport "./lib.js";\n' + bg.read_text())

    (out / "manifest.json").write_text(
        json.dumps(manifest_for(target), indent=2) + "\n"
    )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["firefox", "chrome", "all"], default="all")
    a = ap.parse_args()
    targets = ["firefox", "chrome"] if a.target == "all" else [a.target]
    for t in targets:
        p = build(t)
        n = len(list(p.iterdir()))
        print(f"  built {t:8s} -> {p.relative_to(HERE)}  ({n} files)")


if __name__ == "__main__":
    main()
