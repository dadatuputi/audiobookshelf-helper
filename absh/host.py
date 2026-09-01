"""Native messaging host - the extension's door into the same engine as the CLI.

Protocol: 4-byte little-endian length prefix + UTF-8 JSON, on stdin/stdout.

    ping     -> version, which tag reader is in play, whether config is usable
    config   -> the stored settings (never the API key)
    devices  -> plugged-in volumes that could be the player
    watch    -> push a devices-changed event whenever a volume comes or goes
    status   -> the three-way diff: on both, server only, device only
    libraries/folders -> for choosing an upload target
    pull     -> server to device        (streams progress)
    push     -> device to server        (streams progress)
    remove   -> delete from the device

Settings come from the request when the extension supplies them, and from the
shared config file otherwise, so the extension and `absh` on the command line
behave identically against the same device.

Progress is opt-in per request: a caller that sends one message and reads one
reply must not find progress events queued ahead of its answer.
"""
import json
import struct
import sys
import threading
import traceback

from . import config as config_mod
from . import device as device_mod
from . import devices as devices_mod
from . import mounts as mounts_mod
from . import sync as sync_mod
from . import tags as tags_mod
from .abs_api import AbsError, Client

VERSION = "2.0.0"


# ---------------------------------------------------------------- transport
def _read_exactly(n, stream=None):
    """A pipe read may return short; that would desynchronise every message
    after it, so keep going until we have what the frame promised."""
    stream = stream or sys.stdin.buffer
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_msg(stream=None):
    raw = _read_exactly(4, stream)
    if raw is None:
        return None
    body = _read_exactly(struct.unpack("<I", raw)[0], stream)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


# The mount watcher writes from its own thread, and a reply interleaved with an
# event would be two half-messages on one pipe.
_WRITE_LOCK = threading.Lock()


def write_msg(obj, stream=None):
    stream = stream or sys.stdout.buffer
    b = json.dumps(obj).encode("utf-8")
    with _WRITE_LOCK:
        stream.write(struct.pack("<I", len(b)))
        stream.write(b)
        stream.flush()


# ------------------------------------------------------------------ helpers
def settings(msg):
    """Request settings win; the config file fills the gaps."""
    supplied = {k: msg[k] for k in config_mod.DEFAULTS if k in msg and msg[k] not in (None, "")}
    return config_mod.load(supplied)


def client_for(cfg):
    gaps = config_mod.missing(cfg, need_device=False)
    if gaps:
        raise ValueError("not configured: missing " + ", ".join(gaps))
    return Client(cfg["absUrl"], cfg["apiKey"])


def require_device(cfg):
    from pathlib import Path
    if not cfg.get("devicePath"):
        raise ValueError("no device path is set")
    if not Path(cfg["devicePath"]).is_dir():
        raise ValueError(f"device not mounted at {cfg['devicePath']}")


def library_id(client, cfg):
    if cfg.get("libraryId"):
        return cfg["libraryId"]
    libs = client.libraries()
    if not libs:
        raise ValueError("the server reports no book libraries")
    return libs[0]["id"]


def _serialisable(st):
    """Trim the status for the wire - the extension does not need file paths."""
    def item(i):
        return {k: i.get(k) for k in ("id", "title", "author", "series", "size", "numTracks")}

    def entry(e):
        return {k: e.get(k) for k in
                ("name", "kind", "bytes", "files", "title", "author", "series",
                 "itemId", "source", "matchedBy")}

    return {
        "both": [{**entry(b), "item": item(b["item"])} for b in st["both"]],
        "serverOnly": [item(i) for i in st["serverOnly"]],
        "deviceOnly": [entry(e) for e in st["deviceOnly"]],
        "free": st.get("free", {}),
        "onDeviceBytes": st.get("onDeviceBytes", 0),
    }


# ----------------------------------------------------------------- commands
def cmd_ping(msg, emit):
    cfg = settings(msg)
    return {"ok": True, "version": VERSION, "python": sys.version.split()[0],
            "tags": tags_mod.available(),
            "configured": not config_mod.missing(cfg),
            "missing": config_mod.missing(cfg)}


def cmd_config(msg, emit):
    return {"ok": True, "config": config_mod.redacted(settings(msg)),
            "path": str(config_mod.config_path())}


def cmd_libraries(msg, emit):
    cfg = settings(msg)
    return {"ok": True, "libraries": client_for(cfg).libraries()}


def cmd_folders(msg, emit):
    cfg = settings(msg)
    client = client_for(cfg)
    return {"ok": True, "folders": client.library_folders(msg.get("libraryId") or library_id(client, cfg))}


def cmd_devices(msg, emit):
    """What is plugged in, so the options page can offer a choice."""
    cfg = settings(msg)
    return {"ok": True, "devices": devices_mod.candidates(cfg.get("subdir") or "AUDIOBOOKS")}


_WATCHING = None


def cmd_watch(msg, emit):
    """Start telling the extension when volumes come and go.

    Answers immediately and keeps watching in the background for the life of
    the connection, pushing an unsolicited {"event": "devices-changed"} on each
    change. The point is that the page stops asking: plugging a player in is an
    event the OS already knows about, and the extension was rediscovering it on
    a timer.

    Idempotent - the extension may call it again after a reconnect, and one
    watcher per host process is enough.
    """
    global _WATCHING
    if _WATCHING is None:
        _WATCHING = mounts_mod.watch_in_background(
            lambda: write_msg({"event": "devices-changed"}))
    return {"ok": True, "watching": True, "polls": mounts_mod.is_polling()}


def cmd_status(msg, emit):
    cfg = settings(msg)
    require_device(cfg)
    client = client_for(cfg)
    items = client.items(msg.get("libraryId") or library_id(client, cfg))
    st = device_mod.status(cfg["devicePath"], cfg["subdir"], items,
                           cfg["folderTemplate"], read_tags=msg.get("readTags", True))
    return {"ok": True, **_serialisable(st)}


def cmd_pull(msg, emit):
    cfg = settings(msg)
    require_device(cfg)
    client = client_for(cfg)
    lib = msg.get("libraryId") or library_id(client, cfg)
    wanted = set(msg.get("ids") or [])
    items = [i for i in client.items(lib) if not wanted or i["id"] in wanted]
    if not items:
        raise ValueError("no matching books to pull")
    return {"ok": True, **sync_mod.pull(client, items, cfg, emit)}


def cmd_push(msg, emit):
    cfg = settings(msg)
    require_device(cfg)
    client = client_for(cfg)
    lib = msg.get("libraryId") or library_id(client, cfg)
    folder = msg.get("folderId") or cfg.get("folderId")
    if not folder:
        folders = client.library_folders(lib)
        if not folders:
            raise ValueError("that library has no folder to upload into")
        folder = folders[0]["id"]

    entries = device_mod.scan(cfg["devicePath"], cfg["subdir"], cfg["folderTemplate"])
    names = set(msg.get("names") or [])
    chosen = [e for e in entries if not names or e["name"] in names]
    if not chosen:
        raise ValueError("no matching books on the device to push")
    opts = dict(cfg, libraryId=lib, folderId=folder)
    return {"ok": True, **sync_mod.push(client, chosen, opts, emit)}


def cmd_remove(msg, emit):
    cfg = settings(msg)
    require_device(cfg)
    names = msg.get("names") or []
    if not names:
        raise ValueError("nothing to remove")
    return {"ok": True, **sync_mod.remove(names, cfg, emit)}


COMMANDS = {
    "ping": cmd_ping, "config": cmd_config, "libraries": cmd_libraries,
    "devices": cmd_devices,
    "folders": cmd_folders, "status": cmd_status, "pull": cmd_pull,
    "push": cmd_push, "remove": cmd_remove, "watch": cmd_watch,
}


def handle(msg, emit=None):
    emit = emit or (lambda _ev: None)
    fn = COMMANDS.get(msg.get("cmd"))
    if not fn:
        return {"ok": False, "error": f"unknown cmd {msg.get('cmd')!r}"}
    try:
        return fn(msg, emit)
    except (ValueError, AbsError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:                      # never take the host down
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-800:]}


def main():
    while True:
        try:
            msg = read_msg()
        except (struct.error, ValueError, UnicodeDecodeError) as e:
            write_msg({"ok": False, "error": f"malformed message: {e}", "event": "done"})
            break
        if msg is None:
            break

        rid = msg.get("rid")

        def send(ev):
            write_msg({**ev, "rid": rid} if rid is not None else ev)

        emit = send if msg.get("progress") else None
        send({**handle(msg, emit), "event": "done"})


if __name__ == "__main__":
    main()
