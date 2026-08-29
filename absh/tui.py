"""Full-screen picker over the three-way status.

curses, from the standard library, so `absh tui` works everywhere the CLI does
and needs no install. The row model is separated from the drawing so it can be
tested without a terminal.

    up/down j/k  move          space  select        a  select all shown
    /            filter        enter  act on the selection
    p pull   u push   d delete   r refresh   s settings   q quit
"""
import curses
from pathlib import Path

from . import config as config_mod
from . import device as device_mod
from . import devices as devices_mod
from . import sync as sync_mod
from .abs_api import AbsError, Client
from .naming import human

BOTH, SERVER, DEVICE = "both", "server", "device"

MARK = {BOTH: "=", SERVER: "v", DEVICE: "^"}
LEGEND = "= on both   v server only (pull)   ^ device only (push)"


def build_rows(status):
    """Flatten the status into display rows, in a stable, useful order.

    Server-only first: that is the list you act on most, and burying it under
    two hundred already-synced books would make the screen useless.
    """
    rows = []
    for i in sorted(status["serverOnly"], key=lambda x: (x.get("author") or "", x.get("title") or "")):
        rows.append({"kind": SERVER, "id": i.get("id"), "name": None,
                     "title": i.get("title") or "?", "author": i.get("author") or "",
                     "bytes": i.get("size") or 0, "item": i})
    for e in sorted(status["deviceOnly"], key=lambda x: (x.get("author") or "", x.get("title") or "")):
        rows.append({"kind": DEVICE, "id": None, "name": e["name"],
                     "title": e.get("title") or e["name"], "author": e.get("author") or "",
                     "bytes": e.get("bytes") or 0, "entry": e})
    for b in sorted(status["both"], key=lambda x: (x.get("author") or "", x.get("title") or "")):
        rows.append({"kind": BOTH, "id": b.get("itemId"), "name": b["name"],
                     "title": b.get("title") or b["name"], "author": b.get("author") or "",
                     "bytes": b.get("bytes") or 0, "entry": b})
    return rows


def filter_rows(rows, query):
    q = (query or "").strip().lower()
    if not q:
        return rows
    return [r for r in rows
            if q in f"{r['title']} {r['author']} {r.get('name') or ''}".lower()]


def summarise(rows, selected):
    """What the footer says about the current selection."""
    chosen = [r for r in rows if _key(r) in selected]
    if not chosen:
        return "nothing selected"
    by = {}
    for r in chosen:
        by[r["kind"]] = by.get(r["kind"], 0) + 1
    total = sum(r["bytes"] for r in chosen)
    bits = [f"{n} {k}" for k, n in sorted(by.items())]
    return f"{len(chosen)} selected ({', '.join(bits)})  {human(total)}"


def _key(row):
    return (row["kind"], row.get("id") or row.get("name"))


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = Client(cfg["absUrl"], cfg["apiKey"])
        self.rows = []
        self.selected = set()
        self.query = ""
        self.cursor = 0
        self.top = 0
        self.message = "loading..."
        self.busy = False
        self.status = {"both": [], "serverOnly": [], "deviceOnly": [], "free": {}}

    # ------------------------------------------------------------- data
    def refresh(self):
        self.message = "refreshing..."
        try:
            lib = self.cfg.get("libraryId")
            if not lib:
                libs = self.client.libraries()
                if not libs:
                    self.message = "no book libraries on the server"
                    return
                lib = libs[0]["id"]
                self.cfg["libraryId"] = lib
            items = self.client.items(lib)

            # No device yet, or it is unplugged: still show the library, and
            # say what to press. Pressing "s" is how you set it.
            path = self.cfg.get("devicePath")
            if not path or not Path(path).is_dir():
                self.status = {"both": [], "serverOnly": items, "deviceOnly": [], "free": {}}
                self.rows = build_rows(self.status)
                self.message = (("no device set" if not path
                                 else f"device not mounted at {path}")
                                + " - press s to choose one")
                return

            self.status = device_mod.status(path, self.cfg["subdir"],
                                            items, self.cfg["folderTemplate"])
            self.rows = build_rows(self.status)
            free = self.status.get("free", {}).get("free")
            self.message = (f"{len(self.status['both'])} on both, "
                            f"{len(self.status['serverOnly'])} to pull, "
                            f"{len(self.status['deviceOnly'])} to push"
                            + (f"  -  {human(free)} free" if free else ""))
        except (AbsError, OSError) as e:
            self.message = f"error: {e}"

    def visible(self):
        return filter_rows(self.rows, self.query)

    # ---------------------------------------------------------- actions
    def _run(self, fn, label):
        """Run an operation, feeding progress into the footer."""
        self.busy = True

        def emit(ev):
            if ev.get("event") == "item":
                self.message = (f"{label} {ev.get('index','?')}/{ev.get('count','?')}: "
                                f"{ev.get('title','')}")
            elif ev.get("event") == "progress" and (ev.get("total") or 0) > 1:
                self.message = (f"{label} {ev.get('title','')}  "
                                f"[{ev.get('done')}/{ev.get('total')}]")
        try:
            rep = fn(emit)
        except (AbsError, ValueError, OSError) as e:
            self.message = f"error: {e}"
            self.busy = False
            return
        bits = []
        for k, word in (("copied", "copied"), ("uploaded", "uploaded"),
                        ("skipped", "skipped")):
            if rep.get(k):
                bits.append(f"{word} {rep[k]}")
        if rep.get("removed"):
            bits.append(f"removed {len(rep['removed'])}")
        if rep.get("errors"):
            bits.append(f"{len(rep['errors'])} error(s): {rep['errors'][0]}")
        self.selected.clear()
        self.busy = False
        self.refresh()
        if bits:
            self.message = " - ".join(bits)

    def act(self, kinds=None):
        chosen = [r for r in self.rows if _key(r) in self.selected]
        if kinds:
            chosen = [r for r in chosen if r["kind"] in kinds]
        if not chosen:
            self.message = "nothing selected for that action"
            return
        pulls = [r["item"] for r in chosen if r["kind"] == SERVER]
        pushes = [r["entry"] for r in chosen if r["kind"] == DEVICE]
        removes = [r["name"] for r in chosen if r["kind"] == BOTH and r.get("name")]

        if pulls:
            self._run(lambda emit: sync_mod.pull(self.client, pulls, self.cfg, emit), "pull")
        elif pushes:
            folder = self.cfg.get("folderId")
            if not folder:
                folders = self.client.library_folders(self.cfg["libraryId"])
                if not folders:
                    self.message = "that library has no folder to upload into"
                    return
                folder = folders[0]["id"]
            opts = dict(self.cfg, folderId=folder)
            self._run(lambda emit: sync_mod.push(self.client, pushes, opts, emit), "push")
        elif removes:
            self._run(lambda emit: sync_mod.remove(removes, self.cfg, emit), "remove")


def _draw(stdscr, app):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    rows = app.visible()
    body = max(1, h - 4)

    if app.cursor >= len(rows):
        app.cursor = max(0, len(rows) - 1)
    if app.cursor < app.top:
        app.top = app.cursor
    if app.cursor >= app.top + body:
        app.top = app.cursor - body + 1

    header = f" absh  {LEGEND}"
    stdscr.addnstr(0, 0, header.ljust(w - 1), w - 1, curses.A_REVERSE)

    for i, row in enumerate(rows[app.top:app.top + body]):
        y = i + 1
        sel = "x" if _key(row) in app.selected else " "
        size = human(row["bytes"]).rjust(7)
        line = f" [{sel}] {MARK[row['kind']]} {size}  {row['title']}"
        if row["author"]:
            line += f"  -  {row['author']}"
        attr = curses.A_BOLD if (app.top + i) == app.cursor else curses.A_NORMAL
        if (app.top + i) == app.cursor:
            attr |= curses.A_REVERSE
        stdscr.addnstr(y, 0, line.ljust(w - 1), w - 1, attr)

    filt = f" /{app.query}" if app.query else ""
    stdscr.addnstr(h - 2, 0, (" " + summarise(rows, app.selected) + filt).ljust(w - 1),
                   w - 1, curses.A_REVERSE)
    stdscr.addnstr(h - 1, 0, (" " + app.message)[:w - 1], w - 1)
    stdscr.refresh()


def _loop(stdscr, app):
    curses.curs_set(0)
    stdscr.timeout(-1)
    app.refresh()
    while True:
        _draw(stdscr, app)
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            return
        rows = app.visible()

        if ch in (ord("q"), 27):
            return
        elif ch in (curses.KEY_DOWN, ord("j")):
            app.cursor = min(app.cursor + 1, max(0, len(rows) - 1))
        elif ch in (curses.KEY_UP, ord("k")):
            app.cursor = max(app.cursor - 1, 0)
        elif ch == curses.KEY_NPAGE:
            app.cursor = min(app.cursor + 10, max(0, len(rows) - 1))
        elif ch == curses.KEY_PPAGE:
            app.cursor = max(app.cursor - 10, 0)
        elif ch == ord(" ") and rows:
            k = _key(rows[app.cursor])
            app.selected.symmetric_difference_update({k})
        elif ch == ord("a"):
            keys = {_key(r) for r in rows}
            if keys <= app.selected:
                app.selected -= keys
            else:
                app.selected |= keys
        elif ch == ord("/"):
            app.query = _prompt(stdscr, "filter: ", app.query)
            app.cursor = app.top = 0
        elif ch == ord("r"):
            app.refresh()
        elif ch == ord("s"):
            _settings(stdscr, app)
        elif ch == ord("p"):
            app.act({SERVER})
        elif ch == ord("u"):
            app.act({DEVICE})
        elif ch == ord("d"):
            if _confirm(stdscr, app):
                app.act({BOTH})
        elif ch in (10, 13, curses.KEY_ENTER):
            app.act()


def _prompt(stdscr, label, initial=""):
    h, w = stdscr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    stdscr.addnstr(h - 1, 0, (label + initial).ljust(w - 1), w - 1)
    stdscr.move(h - 1, min(len(label) + len(initial), w - 2))
    try:
        s = stdscr.getstr(h - 1, len(label), 80).decode("utf-8", "replace")
    except Exception:
        s = initial
    curses.noecho()
    curses.curs_set(0)
    return s.strip()


def _settings(stdscr, app):
    """Change the device and the folder on it, without leaving the picker.

    The device path is the one setting nobody can type from memory, so it is
    offered as a list of what is actually plugged in.
    """
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        cfg = app.cfg
        stdscr.addnstr(0, 0, " absh settings".ljust(w - 1), w - 1, curses.A_REVERSE)
        lines = [
            ("d", "device", cfg.get("devicePath") or "(not set)"),
            ("f", "folder on device", cfg.get("subdir") or "AUDIOBOOKS"),
            ("t", "folder template", cfg.get("folderTemplate") or ""),
            ("", "server", cfg.get("absUrl") or "(not set)"),
        ]
        for i, (key, label, value) in enumerate(lines):
            prefix = f" [{key}] " if key else "     "
            stdscr.addnstr(i + 2, 0, f"{prefix}{label:<18} {value}"[:w - 1], w - 1)
        stdscr.addnstr(h - 2, 0,
                       " d device   f folder   t template   q back".ljust(w - 1),
                       w - 1, curses.A_REVERSE)
        stdscr.addnstr(h - 1, 0, (" " + app.message)[:w - 1], w - 1)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (ord("q"), 27, 10, 13):
            return
        if ch == ord("d"):
            _pick_device(stdscr, app)
        elif ch == ord("f"):
            v = _prompt(stdscr, "folder on device: ", app.cfg.get("subdir") or "")
            if v:
                app.cfg["subdir"] = v
                _save(app, f"folder set to {v}")
        elif ch == ord("t"):
            v = _prompt(stdscr, "folder template: ", app.cfg.get("folderTemplate") or "")
            if v:
                app.cfg["folderTemplate"] = v
                _save(app, "template updated - already-synced books may look absent")


def _save(app, note):
    config_mod.save(app.cfg)
    app.message = note
    app.refresh()


def _pick_device(stdscr, app):
    """Choose from what is actually mounted, or type a path."""
    found = devices_mod.candidates(app.cfg.get("subdir") or "AUDIOBOOKS")
    h, w = stdscr.getmaxyx()
    cursor = 0
    while True:
        stdscr.erase()
        stdscr.addnstr(0, 0, " choose a device".ljust(w - 1), w - 1, curses.A_REVERSE)
        if not found:
            stdscr.addnstr(2, 0, " nothing removable is mounted."[:w - 1], w - 1)
            stdscr.addnstr(3, 0, " On a Sansa Clip: Settings -> USB Mode -> MSC."[:w - 1], w - 1)
        for i, d in enumerate(found[:max(1, h - 6)]):
            mark = "*" if app.cfg.get("devicePath") == d["path"] else " "
            note = f"  {app.cfg.get('subdir', 'AUDIOBOOKS')}/" if d["hasSubdir"] else ""
            line = (f"{mark} {d['name'][:20]:<21} {human(d['free']):>8} free  "
                    f"{d['path']}{note}")
            attr = curses.A_REVERSE if i == cursor else curses.A_NORMAL
            stdscr.addnstr(i + 2, 0, line.ljust(w - 1), w - 1, attr)
        stdscr.addnstr(h - 2, 0,
                       " enter choose   t type a path   q cancel".ljust(w - 1),
                       w - 1, curses.A_REVERSE)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (ord("q"), 27):
            return
        if ch in (curses.KEY_DOWN, ord("j")):
            cursor = min(cursor + 1, max(0, len(found) - 1))
        elif ch in (curses.KEY_UP, ord("k")):
            cursor = max(cursor - 1, 0)
        elif ch == ord("t"):
            v = _prompt(stdscr, "device path: ", app.cfg.get("devicePath") or "")
            resolved = devices_mod.resolve(v, app.cfg.get("subdir") or "AUDIOBOOKS")
            if resolved:
                app.cfg["devicePath"] = resolved
                _save(app, f"device set to {resolved}")
                return
            app.message = f"no volume called {v!r} - is it plugged in?"
            return
        elif ch in (10, 13, curses.KEY_ENTER) and found:
            app.cfg["devicePath"] = found[cursor]["path"]
            _save(app, f"device set to {found[cursor]['path']}")
            return


def _confirm(stdscr, app):
    """Deleting from the device is the one irreversible key, so it asks."""
    n = len([r for r in app.rows if _key(r) in app.selected and r["kind"] == BOTH])
    if not n:
        app.message = "select books that are on the device to delete"
        return False
    return _prompt(stdscr, f"delete {n} book(s) from the device? [y/N] ").lower() in ("y", "yes")


def run(cfg):
    # The server is required to show anything at all; the device is not,
    # because pressing "s" is how you set it.
    gaps = config_mod.missing(cfg, need_device=False)
    if gaps:
        print("not configured yet - missing " + ", ".join(gaps))
        print("run:  absh config --url ... --key ...")
        return 1
    app = App(cfg)
    try:
        curses.wrapper(_loop, app)
    except curses.error as e:
        print(f"the terminal is too small or does not support curses: {e}")
        return 1
    return 0
