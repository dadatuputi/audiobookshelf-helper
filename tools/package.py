#!/usr/bin/env python3
"""
Build the release artefacts.

    python3 tools/package.py                      # local build, stamped v0.0.0
    python3 tools/package.py --tag v1.0.0-alpha.1  # exactly what that tag ships

Produces, under release/:

    audiobookshelf-helper-firefox-<ver>.zip   manifest.json AT THE ROOT
    audiobookshelf-helper-chrome-<ver>.zip    manifest.json AT THE ROOT
    audiobookshelf-helper-native-<ver>.zip    the host + installer
    audiobookshelf-helper-source-<ver>.zip    for AMO's source review

The root placement is the whole point: `zip -r out.zip firefox` puts the
manifest at firefox/manifest.json, and both stores reject that with a message
that does not mention nesting. It cost a review cycle once; the test in
tests/python/test_package.py exists so it cannot cost another.
"""
import argparse, importlib.util, json, shutil, subprocess, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release"

_spec = importlib.util.spec_from_file_location("relver", ROOT / "tools" / "release_version.py")
RV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RV)

NATIVE_FILES = ["native/absh_host.py", "native/install.py", "extension/identity.json"]

SOURCE_INCLUDE = ["extension", "native", "tools", "tests",
                  "README.md", "LICENSE", "package.json", "package-lock.json",
                  "playwright.config.js", "vitest.config.js"]
SOURCE_SKIP = {"dist", "node_modules", "__pycache__", ".pytest_cache", "release"}


def zip_dir_contents(src: Path, dest: Path):
    """Zip what is *inside* src, so the manifest lands at the archive root."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src).as_posix())
    return dest


def zip_files(pairs, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in pairs:
            z.write(src, arc)
    return dest


def zip_source(dest: Path):
    """AMO asks for source when a build step is involved. Ours is a file copy,
    but shipping it is cheap and removes a round trip with the reviewer."""
    pairs = []
    for name in SOURCE_INCLUDE:
        p = ROOT / name
        if p.is_file():
            pairs.append((p, name))
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not (SOURCE_SKIP & set(f.relative_to(ROOT).parts)):
                    pairs.append((f, f.relative_to(ROOT).as_posix()))
    return zip_files(pairs, dest)


def build(target: str, version: str):
    subprocess.run([sys.executable, str(ROOT / "extension" / "build.py"),
                    "--target", target, "--version", version, "--quiet"], check=True)
    return ROOT / "extension" / "dist" / target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v0.0.0",
                    help="version to stamp (default v0.0.0, a placeholder for local builds)")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    info = RV.parse_tag(a.tag)
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    made = []
    for target in ("firefox", "chrome"):
        dist = build(target, info[target])
        manifest = json.loads((dist / "manifest.json").read_text())
        assert manifest["version"] == info[target], "version stamp did not take"
        made.append(zip_dir_contents(
            dist, out / f"audiobookshelf-helper-{target}-{info['semver']}.zip"))

    made.append(zip_files(
        [(ROOT / f, Path(f).name) for f in NATIVE_FILES],
        out / f"audiobookshelf-helper-native-{info['semver']}.zip"))

    made.append(zip_source(out / f"audiobookshelf-helper-source-{info['semver']}.zip"))

    (out / "release-info.json").write_text(json.dumps(info, indent=2) + "\n")

    try:
        where = out.resolve().relative_to(Path.cwd())
    except ValueError:
        where = out.resolve()
    print(f"\nWrote {len(made)} archives to {where}/ "
          f"(firefox={info['firefox']} chrome={info['chrome']} "
          f"prerelease={info['prerelease']}):")
    for p in made:
        print(f"  {p.stat().st_size:>9,} bytes  {p.name}")


if __name__ == "__main__":
    main()
