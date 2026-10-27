"""Settings, shared by the CLI, the TUI and the native host.

Precedence, lowest first: built-in defaults, the config file, environment
variables, explicit arguments. That order is what lets the extension pass its
own settings down to the host per-request while the CLI reads them from disk.

The file lives at ~/.config/absh/config.json (or $ABSH_CONFIG). It holds an API
key, so it is written with owner-only permissions.
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    "absUrl": "",
    "apiKey": "",
    "libraryId": "",
    "folderId": "",
    "devicePath": "",
    "subdir": "AUDIOBOOKS",
    "folderTemplate": "{author} - {title}",
    "renameM4b": True,
    "restoreM4b": True,
    # Write Audiobookshelf's own tags into books as they are copied:
    # "ask" each time, "always" or "never".
    "writeTags": "ask",
}


def writes_tags(cfg):
    """Whether a copy writes tags. A request may carry a plain yes or no -
    the answer to "ask" - which wins over the setting."""
    v = cfg.get("writeTags")
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("always", "yes", "true", "1", "on")

# Everything is settable from the environment, which is how you drive this in
# a script or a container without writing a config file.
ENV = {k: "ABSH_" + "".join("_" + c.lower() if c.isupper() else c for c in k).upper()
       for k in DEFAULTS}

SECRET_KEYS = {"apiKey"}


def config_path():
    if os.environ.get("ABSH_CONFIG"):
        return Path(os.environ["ABSH_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "absh" / "config.json"


def load(overrides=None, path=None):
    """Merge defaults, file, environment and explicit overrides."""
    cfg = dict(DEFAULTS)
    p = Path(path) if path else config_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass

    for key, env in ENV.items():
        if os.environ.get(env) is not None:
            raw = os.environ[env]
            cfg[key] = _coerce(DEFAULTS[key], raw)

    for k, v in (overrides or {}).items():
        if v is not None and k in DEFAULTS:
            cfg[k] = v
    return cfg


def _coerce(default, raw):
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return raw


def save(cfg, path=None):
    p = Path(path) if path else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    p.write_text(json.dumps(keep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)   # it contains an API key
    except OSError:
        pass
    return p


def remember(key, value, path=None):
    """Change one setting in the file, leaving the rest of the file as it was
    - not the environment or the flags this run happened to be given."""
    p = Path(path) if path else config_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = value
    return save({**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}, path=p)


def redacted(cfg):
    """A copy safe to print or log."""
    out = dict(cfg)
    for k in SECRET_KEYS:
        if out.get(k):
            out[k] = "***"
    return out


def missing(cfg, need_server=True, need_device=True):
    """What still has to be set before an operation can run."""
    gaps = []
    if need_server:
        if not cfg.get("absUrl"):
            gaps.append("absUrl (your Audiobookshelf URL)")
        if not cfg.get("apiKey"):
            gaps.append("apiKey (Settings -> API Keys)")
    if need_device and not cfg.get("devicePath"):
        gaps.append("devicePath (where the player mounts)")
    return gaps
