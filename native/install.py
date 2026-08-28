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

HOST_NAME = "io.github.abshelper"
GECKO_ID = "abs-helper@local"
HOST_PY = Path(__file__).resolve().parent / "absh_host.py"

# Chrome identifies callers by extension id; unpacked builds get a new id each
# load, so this is filled in by the user after loading (see --chrome-id).
DEFAULT_CHROME_IDS: list[str] = []


def manifest_dirs(system: str, browser: str) -> list[Path]:
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
        # Path is recorded in the registry; the file itself can live anywhere.
        base = Path(os.environ.get("APPDATA", home)) / "abs-helper"
        return [base]
    raise SystemExit(f"unsupported OS: {system}")


def build_manifest(browser: str, chrome_ids: list[str]) -> dict:
    m = {
        "name": HOST_NAME,
        "description": "Audiobookshelf Helper native host",
        "path": str(HOST_PY),
        "type": "stdio",
    }
    if browser == "firefox":
        m["allowed_extensions"] = [GECKO_ID]
    else:
        m["allowed_origins"] = [f"chrome-extension://{i}/" for i in chrome_ids]
    return m


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


def install(browser: str, chrome_ids: list[str], dry: bool, remove: bool) -> int:
    system = platform.system()
    dirs = manifest_dirs(system, browser)
    manifest = build_manifest(browser, chrome_ids)
    wrote = 0
    for d in dirs:
        target = d / f"{HOST_NAME}.json"
        if remove:
            if dry:
                print(f"  would remove {target}")
            elif target.exists():
                target.unlink()
                print(f"  removed {target}")
        else:
            if dry:
                print(f"  would write {target}")
            else:
                d.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(manifest, indent=2) + "\n")
                print(f"  wrote {target}")
            wrote += 1
        if system == "Windows" and not dry:
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
            print("  chrome: skipped - pass --chrome-id <id> "
                  "(unpacked extensions get a fresh id on each load)")
            continue
        print(f"  [{b}]")
        install(b, ids, a.dry_run, a.uninstall)
    if not a.uninstall:
        print("\nNext: load the built extension")
        print("  Firefox  about:debugging#/runtime/this-firefox -> extension/dist/firefox/manifest.json")
        print("  Chrome   chrome://extensions -> Developer mode -> Load unpacked -> extension/dist/chrome")


if __name__ == "__main__":
    main()
