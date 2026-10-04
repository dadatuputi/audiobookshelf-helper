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
    SHA256SUMS                                every archive's sha256, and the tag

SHA256SUMS is what the release workflow signs, with the RELEASE_SIGNING_KEY
secret (tools/sign_release.py ci-sign). `absh update` installs nothing without
that signature.

The root placement is the whole point: `zip -r out.zip firefox` puts the
manifest at firefox/manifest.json, and both stores reject that with a message
that does not mention nesting. It cost a review cycle once; the test in
tests/python/test_package.py exists so it cannot cost another.
"""
import argparse, importlib.util, json, os, shutil, subprocess, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release"

_spec = importlib.util.spec_from_file_location("relver", ROOT / "tools" / "release_version.py")
RV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RV)

sys.path.insert(0, str(ROOT))
from absh import signing  # noqa: E402  (stdlib-only, like the rest of absh)

NATIVE_FILES = ["native/absh_host.py", "native/install.py",
                # identity.py derives the Chrome extension id from the pinned
                # key; without it the archive cannot register the Chrome host.
                "extension/identity.json", "extension/identity.py"]
# The host is a thin shim over the absh package now, so the package has to
# travel with it or the browser launches a helper that cannot import itself.
NATIVE_PACKAGE = "absh"

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


def stamped_version(src: str, semver: str) -> str:
    """version.py's text with the release filled in. Shared with
    tools/sign_release.py, which rebuilds it from the tag to check the archive
    it is about to sign."""
    stamped = src.replace('RELEASE = "dev"', f'RELEASE = "{semver}"')
    assert f'RELEASE = "{semver}"' in stamped, "version stamp did not take"
    return stamped


def stamp_version(out: Path, semver: str) -> Path:
    """A copy of absh/version.py that knows which release it is.

    The checkout says "dev" and must keep saying it - `absh update` compares
    this against what GitHub reports, and a checkout claiming to be a release
    would offer to overwrite itself with one. Written beside the archives
    rather than over the source, so building never dirties the tree.
    """
    stamped = stamped_version((ROOT / NATIVE_PACKAGE / "version.py").read_text(), semver)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "version.py"
    dest.write_text(stamped)
    return dest


def build_native(out: Path, semver: str) -> Path:
    """The helper archive - what `absh update` installs, and so the one the
    signature matters most for."""
    native_pairs = [(ROOT / f, Path(f).name) for f in NATIVE_FILES]
    stamped = stamp_version(out, semver)
    for f in sorted((ROOT / NATIVE_PACKAGE).glob("*.py")):
        # The stamped copy stands in for the checkout's, which says "dev".
        src = stamped if f.name == "version.py" else f
        native_pairs.append((src, f"{NATIVE_PACKAGE}/{f.name}"))
    return zip_files(native_pairs, out / f"audiobookshelf-helper-native-{semver}.zip")


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

    made.append(build_native(out, info["semver"]))

    made.append(zip_source(out / f"audiobookshelf-helper-source-{info['semver']}.zip"))

    # Over exactly what CI uploads, named by the tag it uploads them to, so a
    # signature over this cannot be reattached to a different release.
    (out / signing.MANIFEST_NAME).write_bytes(signing.manifest_for(a.tag, made))

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
    print(f"and {signing.MANIFEST_NAME}, which the release workflow signs "
          f"(tools/sign_release.py ci-sign).")

    if not signing.read_pinned(ROOT / NATIVE_PACKAGE / "release_keys.py"):
        # Not fatal, so the pipeline and its tests run before a key exists -
        # but a helper shipped like this refuses every update after it, so
        # whoever is cutting the release should hear about it here first.
        msg = ("absh/release_keys.py pins no signing key: the helper in this "
               "build will refuse every future update. Add the RELEASE_SIGNING_KEY "
               "secret and run the \"Pin release-signing key\" workflow.")
        print(("::warning title=No release-signing key::" if os.environ.get("GITHUB_ACTIONS")
               else "\nWARNING: ") + msg)


if __name__ == "__main__":
    main()
