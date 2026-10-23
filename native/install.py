#!/usr/bin/env python3
"""
Install Audiobookshelf Helper's helper app, and register it with the browsers.

A downloaded release copies itself to a folder of its own (see app_home) and
registers that copy, so the download can be deleted afterwards. It used to
register itself wherever it was unzipped - usually Downloads - and the helper
stopped starting the moment that folder was tidied away, with nothing but
"disconnected" to show for it. A git checkout still runs where it is.

Every browser/OS combination puts the host manifest somewhere different, and
Windows uses the registry rather than a well-known directory. This handles all
six combinations.

    python3 install.py                      # all detected browsers
    python3 install.py --browser firefox
    python3 install.py --uninstall
    python3 install.py --dry-run
"""
import argparse, json, os, platform, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# identity.json sits under extension/ in a checkout and beside this file in the
# released archive; the installer has to work from either.
for _candidate in (HERE.parent / "extension", HERE):
    if (_candidate / "identity.json").is_file():
        sys.path.insert(0, str(_candidate))
        _IDENTITY_DIR = _candidate
        break
else:  # pragma: no cover
    raise SystemExit("cannot find identity.json next to " + str(HERE))

try:
    import identity as IDENT  # noqa: E402  (path set immediately above)
    IDENTITY = IDENT.load()
except ImportError:
    # The archive ships identity.json without the helper module.
    IDENT = None
    IDENTITY = json.loads((_IDENTITY_DIR / "identity.json").read_text())
HOST_NAME = IDENTITY["hostName"]
GECKO_ID = IDENTITY["geckoId"]
HOST_PY = HERE / "absh_host.py"

# Chrome identifies callers by extension id. The manifest pins a public key, so
# the id is knowable in advance rather than changing on every unpacked load -
# which is what used to make --chrome-id mandatory.
DEFAULT_CHROME_IDS = (IDENT.chrome_ids(IDENTITY) if IDENT
                      else list(IDENTITY.get("chromeIds") or []))

# Windows cannot exec a .py from CreateProcess the way the browsers do on
# macOS and Linux, so the manifest points at a .bat that calls the interpreter.
LAUNCHER_NAME = "absh_host.bat"
POSIX_LAUNCHER_NAME = "absh_host.sh"


def app_home(system: str = None) -> Path:
    """Where a downloaded release lives once installed: per user, and nowhere
    anyone tidies by hand. `absh update` replaces files wherever the running
    helper is, so updates land here too."""
    system = system or platform.system()
    home = Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Audiobookshelf Helper"
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(home)
        return Path(base) / "Audiobookshelf Helper"
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(base) / "audiobookshelf-helper"


def is_checkout() -> bool:
    return _IDENTITY_DIR == HERE.parent / "extension"


def place(dry: bool, system: str = None) -> Path:
    """Put this release where it will stay, and return the host to register.

    Copied whole into a fresh folder and swapped in, so a reinstall replaces
    the old copy rather than layering over it. A checkout, and a copy already
    running from its home, stay where they are.
    """
    dest = app_home(system)
    if is_checkout() or (dest.exists() and HERE == dest.resolve()):
        return HOST_PY
    if dry:
        print(f"  would copy {HERE} -> {dest}")
        return dest / HOST_PY.name
    fresh = dest.with_name(dest.name + ".new")
    old = dest.with_name(dest.name + ".old")
    for leftover in (fresh, old):
        shutil.rmtree(leftover, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(HERE, fresh, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    try:
        if dest.exists():
            dest.rename(old)
        fresh.rename(dest)
    except OSError:
        # Windows will not rename a folder with a file open in it - a helper
        # the browser is running, or a shell sitting there. Copy over it
        # instead, and drop what the new release no longer has.
        _copy_over(fresh, dest)
        shutil.rmtree(fresh, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    host = dest / HOST_PY.name
    if (system or platform.system()) != "Windows":
        host.chmod(host.stat().st_mode | 0o111)
    print(f"  copied to {dest}")
    return host


def _copy_over(src: Path, dest: Path):
    """Make dest hold exactly src's files, in place."""
    keep = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    if dest.exists():
        for p in sorted(dest.rglob("*"), reverse=True):
            rel = p.relative_to(dest)
            if p.is_file() and rel not in keep:
                try:
                    p.unlink()
                except OSError:
                    pass
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
    for rel in keep:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dest / rel)


def unplace(dry: bool, system: str = None):
    """Remove the installed copy, if there is one and it is not a checkout."""
    dest = app_home(system)
    if not dest.exists():
        return
    if dry:
        print(f"  would remove {dest}")
        return
    shutil.rmtree(dest, ignore_errors=True)
    print(f"  removed {dest}" if not dest.exists()
          else f"  could not remove {dest} - delete it by hand")


# ------------------------------------------------- sandboxed browsers (Linux)
#
# A Snap or Flatpak browser cannot start a program outside its sandbox itself.
# Ubuntu's Firefox Snap asks the desktop portal to do it: the portal reads the
# same ~/.mozilla/native-messaging-hosts manifest this writes, asks the user
# once, and starts the helper outside the sandbox. Elsewhere there is usually
# no such portal, and nothing here can make one - so this says what to expect
# rather than trying. See "Sandboxed browsers on Linux" in the README.

# Where each package shows up. Paths starting "~" are under the user's home.
SANDBOXES = [
    ("firefox", "snap", ["/snap/firefox/current"]),
    ("firefox", "flatpak", ["~/.var/app/org.mozilla.firefox",
                            "~/.local/share/flatpak/app/org.mozilla.firefox",
                            "/var/lib/flatpak/app/org.mozilla.firefox"]),
    ("chrome", "snap", ["/snap/chromium/current"]),
    ("chrome", "flatpak", ["~/.var/app/org.chromium.Chromium",
                           "~/.var/app/com.google.Chrome",
                           "~/.local/share/flatpak/app/org.chromium.Chromium",
                           "~/.local/share/flatpak/app/com.google.Chrome",
                           "/var/lib/flatpak/app/org.chromium.Chromium",
                           "/var/lib/flatpak/app/com.google.Chrome"]),
]

PORTAL = ("--dest", "org.freedesktop.portal.Desktop",
          "--object-path", "/org/freedesktop/portal/desktop")


def sandboxed_browsers(root: Path = Path("/"), home: Path = None):
    """[(browser, "snap" | "flatpak")] installed here, in SANDBOXES order."""
    home = home or Path.home()
    found = []
    for browser, kind, paths in SANDBOXES:
        for p in paths:
            where = home / p[2:] if p.startswith("~/") else root / p.lstrip("/")
            if where.exists():
                found.append((browser, kind))
                break
    return found


def portal_has_webextensions(run=None):
    """True, False, or None when there is no way to ask (no gdbus, no bus)."""
    import subprocess
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    try:
        r = run(["gdbus", "introspect", "--session", *PORTAL])
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return "org.freedesktop.portal.WebExtensions" in r.stdout


# Whether Firefox asks the portal to start a helper: 0 never, 1 always,
# 2 when it finds one. Ubuntu's Snap ships it on; Flathub's Firefox does not.
PORTAL_PREF = "widget.use-xdg-desktop-portal.native-messaging"

# How the portal names each sandboxed Firefox when it records a choice.
PORTAL_APP = {"snap": "snap.firefox", "flatpak": "org.mozilla.firefox"}


def allow_command(kind: str = "snap", host_name: str = None) -> str:
    """Re-allow the helper for a sandboxed Firefox after "Don't allow". gdbus
    is on every Ubuntu desktop; the flatpak tool, which can do the same, is
    not always. The portal keys the choice by app, so a Snap and a Flatpak
    each need their own."""
    return ("gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore "
            "--object-path /org/freedesktop/impl/portal/PermissionStore "
            "--method org.freedesktop.impl.portal.PermissionStore.SetPermission "
            f"webextensions true {host_name or HOST_NAME} {PORTAL_APP[kind]} \"['yes']\"")


def sandbox_advice(found, portal=None) -> str:
    """What to expect from each sandboxed browser found, or "" if none."""
    out = []
    for browser, kind in found:
        if (browser, kind) == ("firefox", "snap"):
            lines = ["Firefox here is a Snap. It reaches the helper through Ubuntu's "
                     "desktop portal: the first time the extension connects, Ubuntu "
                     "asks whether to allow it - choose Allow."]
            if portal is False:
                lines.append("  But this system's desktop portal has no WebExtensions "
                             "support, so the Snap cannot start the helper. Update "
                             "xdg-desktop-portal, or use Firefox from Mozilla's .deb.")
            lines += ["  If you chose Don't allow, run this, then restart Firefox:",
                      "    " + allow_command()]
            out.append("\n".join(lines))
        elif (browser, kind) == ("firefox", "flatpak") and portal is True:
            # Where the portal exists - Ubuntu's - a Flatpak uses it as a Snap
            # does, and the choice it remembers is keyed to the Flatpak's id.
            # But Flathub's Firefox does not use the portal unless told to:
            # its default leaves the pref off, where Ubuntu's Snap turns it on.
            out.append("\n".join([
                "Firefox here is a Flatpak, and this system's desktop portal can "
                "start the helper for it - but Flathub's Firefox does not use the "
                "portal until you tell it to. In Firefox, open about:config, set",
                f"    {PORTAL_PREF} = 1",
                "  and restart Firefox. The first time the extension connects, you "
                "are asked whether to allow it - choose Allow. This opens nothing "
                "else in Firefox's sandbox.",
                "  If you chose Don't allow, run this, then restart Firefox:",
                "    " + allow_command("flatpak")]))
        elif (browser, kind) == ("firefox", "flatpak"):
            out.append("Firefox here is a Flatpak. A Flatpak Firefox can start the helper "
                       "only through a desktop portal most systems do not have yet "
                       "(Ubuntu's does); without one, the extension will say the helper "
                       "is not installed. Use Firefox from your distribution or Mozilla "
                       "instead - or see \"Sandboxed browsers on Linux\" in the README, "
                       "which explains a workaround and what it costs.")
        elif browser == "chrome":
            name = "a Snap" if kind == "snap" else "a Flatpak"
            out.append(f"Chromium or Chrome here is {name}, which cannot start the "
                       "helper. Use the extension's folder mode instead: Settings -> "
                       "Folder, no helper needed.")
    return "\n\n".join(out)


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
    """What the manifest's "path" should point at on this OS.

    Always a generated launcher, never the .py itself: the browser starts the
    host with its own environment, so a shebang that says "env python3" is a
    coin flip. See write_posix_launcher.
    """
    return manifest_dir / (LAUNCHER_NAME if system == "Windows"
                           else POSIX_LAUNCHER_NAME)


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


def write_windows_launcher(target: Path, host: Path = None):
    """A .bat that hands stdio straight through to the interpreter.

    The same guards as the POSIX launcher: the Python launcher `py -3` if the
    recorded interpreter has gone, and a line on stderr when the helper's
    files are missing rather than an exit with nothing said.
    """
    host = host or HOST_PY
    # Labels, not ( ) blocks: a ")" in a path - a user folder named
    # "Smith (Work)" - would close a block early.
    # Bytes, not text: written as text on Windows, every \r\n became \r\r\n.
    # UTF-8, with cmd told so first: it reads a batch file in the console's
    # code page, which garbles a path with an accented user name in it.
    target.write_bytes("\r\n".join([
        "@echo off",
        "chcp 65001 >nul",
        "rem generated by install.py - absolute interpreter, no PATH lookup",
        f'if not exist "{host}" goto gone',
        f'if not exist "{sys.executable}" goto py',
        f'"{sys.executable}" "{host}" %*',
        "exit /b %errorlevel%",
        ":py",
        f'py -3 "{host}" %*',
        "exit /b %errorlevel%",
        ":gone",
        "echo Audiobookshelf Helper: its files are gone; download the helper app"
        " and run install.py again 1>&2",
        "exit /b 1",
        "",
    ]).encode("utf-8"))
    return target


def write_posix_launcher(target: Path, host: Path = None):
    """A tiny sh wrapper that execs an absolute interpreter.

    Pointing the manifest straight at absh_host.py and trusting its
    "#!/usr/bin/env python3" shebang is what broke: the browser launches a
    native host with its own minimal environment, not your shell's. If
    python3 only exists on your PATH - a conda or homebrew install, and on a
    Mac with no Command Line Tools there may be no /usr/bin/python3 at all -
    then env finds nothing, the host never starts, and the extension can only
    report that the port disconnected.

    Windows already worked this way because it has no shebangs. Now both do.

    If that interpreter goes - a conda environment removed - the system's own
    python3 is tried before giving up, and either way stderr says why, which
    Firefox shows in its Browser Console.
    """
    host = host or HOST_PY
    target.write_text(
        "#!/bin/sh\n"
        "# generated by install.py - absolute interpreter, no PATH lookup\n"
        f'PY="{sys.executable}"\n'
        '[ -x "$PY" ] || PY=/usr/bin/python3\n'
        f'HOST="{host}"\n'
        '[ -f "$HOST" ] || { echo "Audiobookshelf Helper: $HOST is gone;'
        ' download the helper app and run install.py again" >&2; exit 1; }\n'
        'exec "$PY" "$HOST" "$@"\n'
    )
    target.chmod(0o755)
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


def install(browser: str, chrome_ids, dry: bool, remove: bool, system: str = None,
            host: Path = None) -> int:
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
                if launcher.exists():
                    launcher.unlink()
                    print(f"  removed {launcher}")
        else:
            if dry:
                print(f"  would write {target}")
                print(f"  would write {launcher}")
            else:
                d.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(manifest, indent=2) + "\n")
                print(f"  wrote {target}")
                if system == "Windows":
                    write_windows_launcher(launcher, host)
                else:
                    write_posix_launcher(launcher, host)
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
    print(f"{'Uninstalling' if a.uninstall else 'Installing'} the helper app "
          f"({HOST_NAME}) on {platform.system()}")
    host = None if a.uninstall else place(a.dry_run)
    for b in browsers:
        if b == "chrome" and not ids and not a.uninstall:
            print("  chrome: skipped - no id. Set chromeKey in "
                  "extension/identity.json, or pass --chrome-id <id>.")
            continue
        print(f"  [{b}]")
        install(b, ids, a.dry_run, a.uninstall, host=host)
    if a.uninstall:
        if a.browser == "all" and not is_checkout():
            unplace(a.dry_run)
    else:
        print()
        print(next_steps(host))
        if platform.system() == "Linux":
            found = [f for f in sandboxed_browsers() if a.browser in ("all", f[0])]
            portal = (portal_has_webextensions()
                      if any(b == "firefox" for b, _ in found) else None)
            advice = sandbox_advice(found, portal)
            if advice:
                print()
                print(advice)


def release_version():
    """The release this copy came from, or None in a checkout."""
    import re
    try:
        text = (HERE / "absh" / "version.py").read_text()
    except OSError:
        return None
    m = re.search(r'^RELEASE\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m and m.group(1) != "dev" else None


def next_steps(host: Path = None):
    """What to do after installing - for a downloaded release, or a checkout."""
    if is_checkout():                                   # a checkout: build and load
        return ("Next: restart the browser, then load the built extension\n"
                "  Firefox  about:debugging#/runtime/this-firefox -> "
                "extension/dist/firefox/manifest.json\n"
                "  Chrome   chrome://extensions -> Developer mode -> Load unpacked -> "
                "extension/dist/chrome")
    v = release_version()
    name = (lambda kind, ext: f"audiobookshelf-helper-{kind}-{v}.{ext}") if v else \
           (lambda kind, ext: f"audiobookshelf-helper-{kind}-<version>.{ext}")
    kept = ""
    if host and host.parent != HERE:
        kept = (f"Installed in {host.parent}.\n"
                f"You can delete {HERE} - nothing uses it now.\n\n")
    return (kept + "Next:\n"
            "  1. Quit and reopen your browser.\n"
            "  2. If the extension is not installed yet, from the same release:\n"
            f"     Firefox  open {name('firefox-extension', 'xpi')}\n"
            f"     Chrome   unzip {name('chrome-extension', 'zip')}, then\n"
            "              chrome://extensions -> Developer mode -> Load unpacked")


if __name__ == "__main__":
    main()
