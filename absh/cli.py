"""The command line. This is the product; the extension is a front-end for it.

    absh config --url http://media.local:13378 --key abc --device /Volumes/PLAYER
    absh status                 what is where
    absh ls                     what is on the device
    absh pull redwall           server -> device
    absh push --all             device -> server
    absh rm "Brian Jacques - Redwall.m4a"
    absh tui                    the full-screen picker
"""
import argparse
import sys
from pathlib import Path

from . import config as config_mod
from . import device as device_mod
from . import devices as devices_mod
from . import sync as sync_mod
from . import tags as tags_mod
from .abs_api import AbsError, Client
from .naming import human

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")


def _plain():
    return not sys.stdout.isatty()


def paint(s, colour):
    return s if _plain() else f"{colour}{s}{RESET}"


def die(msg, code=1):
    print(paint("error: ", RED) + msg, file=sys.stderr)
    raise SystemExit(code)


def client_for(cfg):
    gaps = config_mod.missing(cfg, need_device=False)
    if gaps:
        die("not configured yet - missing " + ", ".join(gaps) +
            "\n       run:  absh config --url ... --key ...")
    return Client(cfg["absUrl"], cfg["apiKey"])


def require_device(cfg):
    if not cfg.get("devicePath"):
        die("no device path set - run:  absh config --device <path or volume name>")
    if not Path(cfg["devicePath"]).is_dir():
        die(f"device not mounted at {cfg['devicePath']}")


def pick_library(client, cfg):
    """The configured library, or the only one, or ask."""
    libs = client.libraries()
    if not libs:
        die("the server reports no book libraries")
    if cfg.get("libraryId"):
        for l in libs:
            if l["id"] == cfg["libraryId"]:
                return l
        print(paint(f"warning: configured library {cfg['libraryId']} is gone; "
                    f"using {libs[0]['name']}", YELLOW), file=sys.stderr)
    if len(libs) > 1 and not cfg.get("libraryId"):
        print("several book libraries; using the first. Pin one with "
              "`absh config --library <id>`:", file=sys.stderr)
        for l in libs:
            print(f"  {l['id']}  {l['name']}", file=sys.stderr)
    return libs[0]


def matches(text, needles):
    if not needles:
        return True
    hay = text.lower()
    return any(n.lower() in hay for n in needles)


def progress_printer(quiet=False):
    """Live progress on a terminal, one plain line per book anywhere else.

    Rewriting with \r and \033[K into a pipe or a log just emits the escapes
    as text and runs every book together on one line.
    """
    live = sys.stdout.isatty()
    state = {"line": "", "open": False}

    def emit(ev):
        if quiet:
            return
        if ev.get("event") == "item":
            if state["open"] and live:
                print()
            op = ev.get("op", "sync")
            state["line"] = (f"  {op} {ev.get('index', '?')}/{ev.get('count', '?')}"
                             f"  {ev.get('title', '')}")
            state["open"] = True
            if live:
                sys.stdout.write(state["line"])
                sys.stdout.flush()
            else:
                print(state["line"], flush=True)
                state["open"] = False
        elif ev.get("event") == "progress" and live and state["open"]:
            total, done = ev.get("total") or 0, ev.get("done") or 0
            bar = f" [{done}/{total}]" if total > 1 else ""
            sys.stdout.write("\r" + state["line"] + bar + "\033[K")
            sys.stdout.flush()

    def finish():
        if state["open"] and live and not quiet:
            print()
        state["open"] = False

    emit.finish = finish
    return emit


def show_report(rep, verb):
    bits = []
    if rep.get("copied"):
        bits.append(f"copied {rep['copied']} file(s)")
    if rep.get("uploaded"):
        bits.append(f"uploaded {rep['uploaded']} book(s)")
    if rep.get("removed"):
        bits.append(f"removed {len(rep['removed'])}")
    if rep.get("skipped"):
        bits.append(f"skipped {rep['skipped']} already present")
    if rep.get("freed"):
        bits.append(f"freed {human(rep['freed'])}")
    print(paint(verb + ": " + (", ".join(bits) or "nothing to do"),
                GREEN if not rep.get("errors") else YELLOW))
    for e in rep.get("errors", []):
        print(paint("  ! " + e, RED))
    return 1 if rep.get("errors") else 0


# ------------------------------------------------------------- commands
def cmd_config(args, cfg):
    changed = {}
    if getattr(args, "device", None):
        # A volume name is easier to remember than its full path, and on Linux
        # nobody remembers whether it is /media/you or /run/media/you.
        found = devices_mod.resolve(args.device, cfg.get("subdir", "AUDIOBOOKS"))
        if found:
            args.device = found
        elif not Path(args.device).is_absolute():
            die(f"no mounted volume called {args.device!r}. "
                f"Run `absh devices` to see what is plugged in.")

    for flag, key in (("url", "absUrl"), ("key", "apiKey"), ("device", "devicePath"),
                      ("subdir", "subdir"), ("template", "folderTemplate"),
                      ("library", "libraryId"), ("folder", "folderId")):
        v = getattr(args, flag, None)
        if v is not None:
            changed[key] = v
    if args.rename_m4b is not None:
        changed["renameM4b"] = args.rename_m4b

    if changed:
        cfg.update(changed)
        p = config_mod.save(cfg)
        print(f"wrote {p}")
    for k, v in sorted(config_mod.redacted(cfg).items()):
        print(f"  {k:16s} {v}")
    if not changed:
        print(f"\n({config_mod.config_path()})")
    return 0


def cmd_devices(args, cfg):
    """List what is plugged in, so nobody has to guess the path."""
    found = devices_mod.candidates(cfg.get("subdir", "AUDIOBOOKS"))
    if not found:
        print("no removable volumes are mounted.")
        print("Plug the player in. Some players need a setting to mount as a\n"
              "drive at all - look for USB Mode -> MSC.")
        return 1

    current = cfg.get("devicePath")
    print(f"{'':2}{'VOLUME':<22}{'SIZE':>9}{'FREE':>9}  PATH")
    for d in found:
        mark = "*" if current and Path(current) == Path(d["path"]) else " "
        note = ""
        if d["hasSubdir"]:
            note = paint(f"  {cfg.get('subdir', 'AUDIOBOOKS')}/ ({d['books']} items)", GREEN)
        print(f"{mark} {d['name'][:21]:<22}{human(d['total']):>9}{human(d['free']):>9}  "
              f"{d['path']}{note}")

    if not current:
        best = found[0]
        print(f"\nSet one with:  absh config --device {best['name']}")
    return 0


def cmd_status(args, cfg):
    require_device(cfg)
    client = client_for(cfg)
    lib = pick_library(client, cfg)
    items = client.items(lib["id"])
    st = device_mod.status(cfg["devicePath"], cfg["subdir"], items,
                           cfg["folderTemplate"], read_tags=not args.no_tags)

    both, srv, dev = st["both"], st["serverOnly"], st["deviceOnly"]
    print(f"{BOLD if not _plain() else ''}{lib['name']}{RESET if not _plain() else ''}"
          f"  {len(items)} on server, {len(both) + len(dev)} on device"
          f"  ({human(st['onDeviceBytes'])} used"
          + (f", {human(st['free'].get('free', 0))} free)" if st.get("free") else ")"))

    if both and not args.only:
        print(paint(f"\non both ({len(both)})", GREEN))
        for b in sorted(both, key=lambda x: (x.get("author") or "", x.get("title") or "")):
            how = "" if b["matchedBy"] == "id" else paint(f"  [matched by {b['matchedBy']}]", DIM)
            print(f"  {b.get('title','?')}  {paint(human(b['bytes']), DIM)}{how}")

    if srv and args.only in (None, "server"):
        print(paint(f"\non the server only ({len(srv)})  -> absh pull", YELLOW))
        for i in sorted(srv, key=lambda x: (x.get("author") or "", x.get("title") or ""))[:args.limit]:
            print(f"  {i.get('title','?')}  {paint(i.get('author',''), DIM)}")
        if len(srv) > args.limit:
            print(paint(f"  ... and {len(srv) - args.limit} more", DIM))

    if dev and args.only in (None, "device"):
        print(paint(f"\non the device only ({len(dev)})  -> absh push", YELLOW))
        for e in dev:
            who = e.get("author") or "unknown author"
            print(f"  {e.get('title','?')}  {paint(who + ' - ' + human(e['bytes']), DIM)}")

    if not (both or srv or dev):
        print("nothing on either side")
    return 0


def cmd_ls(args, cfg):
    require_device(cfg)
    entries = device_mod.scan(cfg["devicePath"], cfg["subdir"], cfg["folderTemplate"],
                              read_tags=not args.no_tags)
    if not entries:
        print("device is empty")
        return 0
    for e in entries:
        files = f"{e['files']} files" if e["files"] > 1 else "1 file"
        print(f"  {human(e['bytes']):>8}  {files:>9}  {e['name']}")
        if e.get("title") and e["title"] != Path(e["name"]).stem:
            print(paint(f"            {e['title']} - {e.get('author','')}", DIM))
    total = sum(e["bytes"] for e in entries)
    free = device_mod.free_space(cfg["devicePath"]).get("free")
    print(f"\n  {len(entries)} book(s), {human(total)}"
          + (f", {human(free)} free" if free else ""))
    return 0


def cmd_pull(args, cfg):
    require_device(cfg)
    client = client_for(cfg)
    lib = pick_library(client, cfg)
    items = client.items(lib["id"])
    st = device_mod.status(cfg["devicePath"], cfg["subdir"], items,
                           cfg["folderTemplate"], read_tags=False)
    # --force re-downloads books already on the device; otherwise only the
    # ones that are missing are even considered.
    pool = items if args.force else st["serverOnly"]
    chosen = [i for i in pool if matches(f"{i.get('title')} {i.get('author')} {i.get('series')}",
                                         args.query)]
    if not args.query and not args.all:
        die("name something to pull, or pass --all")
    if not chosen:
        print("nothing matches" if args.query else "device is already up to date")
        return 0

    print(f"pulling {len(chosen)} book(s) to {cfg['devicePath']}")
    if args.dry_run:
        for i in chosen:
            print(f"  would pull  {i.get('title')}")
        return 0
    emit = progress_printer(args.quiet)
    rep = sync_mod.pull(client, chosen, dict(cfg, force=args.force), emit)
    emit.finish()
    return show_report(rep, "pull")


def cmd_push(args, cfg):
    require_device(cfg)
    client = client_for(cfg)
    lib = pick_library(client, cfg)
    items = client.items(lib["id"])
    st = device_mod.status(cfg["devicePath"], cfg["subdir"], items, cfg["folderTemplate"])
    chosen = [e for e in st["deviceOnly"]
              if matches(f"{e.get('title')} {e.get('author')} {e['name']}", args.query)]
    if not args.query and not args.all:
        die("name something to push, or pass --all")
    if not chosen:
        print("nothing on the device that the server does not already have")
        return 0

    folder_id = cfg.get("folderId")
    if not folder_id:
        folders = client.library_folders(lib["id"])
        if not folders:
            die(f"library {lib['name']} has no folders to upload into")
        folder_id = folders[0]["id"]
        if len(folders) > 1:
            print(paint(f"note: uploading into {folders[0]['fullPath']}; pin another "
                        f"with `absh config --folder <id>`", DIM))

    opts = dict(cfg, libraryId=lib["id"], folderId=folder_id)
    print(f"pushing {len(chosen)} book(s) to {lib['name']}")
    if args.dry_run:
        for e in chosen:
            print(f"  would push  {e.get('title')}  ({e['files']} file(s), {human(e['bytes'])})")
        return 0
    emit = progress_printer(args.quiet)
    rep = sync_mod.push(client, chosen, opts, emit)
    emit.finish()
    return show_report(rep, "push")


def cmd_rm(args, cfg):
    require_device(cfg)
    entries = device_mod.scan(cfg["devicePath"], cfg["subdir"], cfg["folderTemplate"])
    chosen = [e for e in entries
              if matches(f"{e['name']} {e.get('title')} {e.get('author')}", args.query)]
    if not args.query and not args.all:
        die("name something to remove, or pass --all")
    if not chosen:
        print("nothing matches")
        return 0

    print(f"removing {len(chosen)} book(s) from {cfg['devicePath']}:")
    for e in chosen:
        print(f"  {e['name']}  ({human(e['bytes'])})")
    if args.dry_run:
        return 0
    if not args.yes:
        try:
            if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                print("cancelled")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled")
            return 0
    rep = sync_mod.remove([e["name"] for e in chosen], cfg)
    return show_report(rep, "remove")


def cmd_doctor(args, cfg):
    """Check every moving part and say which one is broken."""
    ok = True
    print(f"config      {config_mod.config_path()}")
    print(f"tags        {tags_mod.available()}"
          + ("" if tags_mod.available() == "mutagen" else
             "  (pip install mutagen for better coverage)"))

    if cfg.get("devicePath"):
        mounted = Path(cfg["devicePath"]).is_dir()
        print(f"device      {cfg['devicePath']}  "
              f"{paint('mounted', GREEN) if mounted else paint('NOT MOUNTED', RED)}")
        if not mounted:
            print(paint("            run `absh devices` to see what is plugged in", DIM))
        ok = ok and mounted
    else:
        print(paint("device      not set - run `absh devices`, then "
                    "`absh config --device <name>`", YELLOW))
        ok = False

    if cfg.get("absUrl") and cfg.get("apiKey"):
        try:
            who = Client(cfg["absUrl"], cfg["apiKey"]).ping()
            print(f"server      {cfg['absUrl']}  {paint('ok', GREEN)} (as {who['user']})")
        except AbsError as e:
            print(f"server      {cfg['absUrl']}  {paint('FAILED', RED)}\n            {e}")
            ok = False
    else:
        print(paint("server      not configured", YELLOW))
        ok = False
    return 0 if ok else 1


def cmd_tui(args, cfg):
    from . import tui
    return tui.run(cfg)


# ---------------------------------------------------------------- parser
def build_parser():
    ap = argparse.ArgumentParser(prog="absh", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="use a different config file")
    ap.add_argument("--device", dest="o_devicePath", help="override the device path")
    ap.add_argument("--url", dest="o_absUrl", help="override the server URL")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, query=True):
        if query:
            p.add_argument("query", nargs="*", help="substrings to match (title, author, series)")
            p.add_argument("--all", action="store_true", help="everything, no filter")
        p.add_argument("-n", "--dry-run", action="store_true")
        p.add_argument("-q", "--quiet", action="store_true")

    c = sub.add_parser("config", help="show or change settings")
    for flag, helptext in (("--url", "Audiobookshelf URL"), ("--key", "API key"),
                           ("--device", "player mount path"), ("--subdir", "folder on the device"),
                           ("--template", "folder template"),
                           ("--library", "library id to pin"),
                           ("--folder", "upload folder id to pin")):
        c.add_argument(flag, help=helptext)
    c.add_argument("--rename-m4b", dest="rename_m4b", action="store_true", default=None,
                   help="rename .m4b to .m4a on the device (default)")
    c.add_argument("--no-rename-m4b", dest="rename_m4b", action="store_false",
                   help="keep .m4b as-is")
    c.set_defaults(fn=cmd_config)

    s = sub.add_parser("status", help="what is on the server, the device, or both")
    s.add_argument("--only", choices=["server", "device"], help="just one side")
    s.add_argument("--limit", type=int, default=25, help="cap the server-only list")
    s.add_argument("--no-tags", action="store_true", help="skip reading tags (faster)")
    s.set_defaults(fn=cmd_status)

    l = sub.add_parser("ls", help="list what is on the device")
    l.add_argument("--no-tags", action="store_true")
    l.set_defaults(fn=cmd_ls)

    p = sub.add_parser("pull", help="copy books from the server to the device")
    common(p)
    p.add_argument("--force", action="store_true", help="include books already on the device")
    p.set_defaults(fn=cmd_pull)

    u = sub.add_parser("push", help="upload device-only books to the server")
    common(u)
    u.set_defaults(fn=cmd_push)

    r = sub.add_parser("rm", help="delete books from the device")
    common(r)
    r.add_argument("-y", "--yes", action="store_true", help="do not ask")
    r.set_defaults(fn=cmd_rm)

    dv = sub.add_parser("devices", help="list plugged-in volumes and their paths")
    dv.set_defaults(fn=cmd_devices)

    d = sub.add_parser("doctor", help="check the configuration and connections")
    d.set_defaults(fn=cmd_doctor)

    t = sub.add_parser("tui", help="full-screen picker")
    t.set_defaults(fn=cmd_tui)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    overrides = {k[2:]: v for k, v in vars(args).items()
                 if k.startswith("o_") and v is not None}
    cfg = config_mod.load(overrides, path=args.config)
    try:
        return args.fn(args, cfg)
    except AbsError as e:
        die(str(e))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
