#!/usr/bin/env python3
"""
Register the native messaging host for Audiobookshelf Helper.

Every browser/OS combination puts the host manifest somewhere different, and
Windows uses the registry rather than a well-known directory. This handles all
six combinations.

    python3 install.py                      # all detected browsers
    python3 install.py --browser firefox
    python3 install.py --uninstall
    python3 install.py --dry-run
"""
import argparse, json, os, platform, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "extension"))
import identity as IDENT  # noqa: E402  (path set immediately above)

IDENTITY = IDENT.load()
HOST_NAME = IDENTITY["hostName"]
GECKO_ID = IDENTITY["geckoId"]
HOST_PY = HERE / "absh_host.py"

# Chrome identifies callers by extension id. The manifest pins a public key, so
# the id is knowable in advance rather than changing on every unpacked load -
# which is what used to make --chrome-id mandatory.
DEFAULT_CHROME_IDS = IDENT.chrome_ids(IDENTITY)

# Windows cannot exec a .py from CreateProcess the way the browsers do on
# macOS and Linux, so the manifest points at a .bat that calls the interpreter.
LAUNCHER_NAME = "absh_host.bat"


def manifest_dirs(system: str, browser: str):
    home = Path.home()
    if system == "Darwin":
        base = home / "Library" / "Application Support"
        return {
            "firefox": [base / "Mozilla" / "NativeMessagingHosts"],
            "chrome": [
                base / "Google" / "Chrome" / "NativeMessagingHosts",
                base / "Chromium" / "NativeMessagingHosts",
            ],
        }[browser]
    if system == "Linux":
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        return {
            "firefox": [home / ".mozilla" / "native-messaging-hosts"],
            "chrome": [
                cfg / "google-chrome" / "NativeMessagingHosts",
                cfg / "chromium" / "NativeMessagingHosts",
            ],
        }[browser]
    if system == "Windows":
        # The path is recorded in the registry, so the file itself can live
        # anywhere - but it must live somewhere *per browser*. Firefox's
        # manifest carries allowed_extensions and Chrome's carries
        # allowed_origins; sharing one path meant installing both browsers
        # wrote Chrome's manifest over Firefox's and broke Firefox silently.
        base = Path(os.environ.get("APPDATA", home)) / "abs-helper"
        return [base / browser]
    raise SystemExit(f"unsupported OS: {system}")


def host_command_path(system: str, manifest_dir: Path) -> Path:
    """What the manifest's "path" should point at on this OS."""
    if system == "Windows":
        return manifest_dir / LAUNCHER_NAME
    return HOST_PY


def build_manifest(browser: str, chrome_ids, path: Path = None) -> dict:
    m = {
        "name": HOST_NAME,
        "description": "Audiobookshelf Helper native host",
        "path": str(path or HOST_PY),
        "type": "stdio",
    }
    if browser == "firefox":
        m["allowed_extensions"] = [GECKO_ID]
    else:
        m["allowed_origins"] = [f"chrome-extension://{i}/" for i in chrome_ids]
    return m


def write_windows_launcher(target: Path):
    """A .bat that hands stdio straight through to the interpreter."""
    target.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" "{HOST_PY}" %*\r\n'
    )
    return target


def registry_key(browser: str) -> str:
    vendor = "Mozilla" if browser == "firefox" else "Google\\Chrome"
    return rf"Software\{vendor}\NativeMessagingHosts\{HOST_NAME}"


def write_windows_registry(browser: str, manifest_path: Path, remove: bool):
    import winreg  # noqa: available only on Windows
    key = registry_key(browser)
    if remove:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            print(f"  removed HKCU\\{key}")
        except FileNotFoundError:
            print(f"  (not present) HKCU\\{key}")
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(manifest_path))
    print(f"  registry HKCU\\{key} -> {manifest_path}")


def install(browser: str, chrome_ids, dry: bool, remove: bool, system: str = None) -> int:
    system = system or platform.system()
    dirs = manifest_dirs(system, browser)
    wrote = 0
    for d in dirs:
        target = d / f"{HOST_NAME}.json"
        launcher = host_command_path(system, d)
        manifest = build_manifest(browser, chrome_ids, launcher)
        if remove:
            if dry:
                print(f"  would remove {target}")
            else:
                if target.exists():
                    target.unlink()
                    print(f"  removed {target}")
                if system == "Windows" and launcher.exists():
                    launcher.unlink()
                    print(f"  removed {launcher}")
        else:
            if dry:
                print(f"  would write {target}")
                if system == "Windows":
                    print(f"  would write {launcher}")
            else:
                d.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(manifest, indent=2) + "\n")
                print(f"  wrote {target}")
                if system == "Windows":
                    write_windows_launcher(launcher)
                    print(f"  wrote {launcher}")
            wrote += 1
        # Guarded on the *running* OS, not the target: tests exercise the
        # Windows file layout on Linux, where winreg does not exist.
        if system == "Windows" and not dry and platform.system() == "Windows":
            write_windows_registry(browser, target, remove)
    return wrote


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", choices=["firefox", "chrome", "all"], default="all")
    ap.add_argument("--chrome-id", action="append", default=[],
                    help="Chrome extension id (repeatable). Required for Chrome; "
                         "find it at chrome://extensions with Developer mode on.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()

    if not HOST_PY.exists():
        raise SystemExit(f"host script not found: {HOST_PY}")
    if platform.system() != "Windows":
        HOST_PY.chmod(HOST_PY.stat().st_mode | 0o111)

    browsers = ["firefox", "chrome"] if a.browser == "all" else [a.browser]
    ids = a.chrome_id or DEFAULT_CHROME_IDS
    print(f"{'Uninstalling' if a.uninstall else 'Installing'} {HOST_NAME} "
          f"on {platform.system()}")
    for b in browsers:
        if b == "chrome" and not ids and not a.uninstall:
            print("  chrome: skipped - no id. Set chromeKey in "
                  "extension/identity.json, or pass --chrome-id <id>.")
            continue
        print(f"  [{b}]")
        install(b, ids, a.dry_run, a.uninstall)
    if not a.uninstall:
        print("\nNext: load the built extension")
        print("  Firefox  about:debugging#/runtime/this-firefox -> extension/dist/firefox/manifest.json")
        print("  Chrome   chrome://extensions -> Developer mode -> Load unpacked -> extension/dist/chrome")


if __name__ == "__main__":
    main()
