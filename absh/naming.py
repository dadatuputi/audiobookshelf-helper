"""How a book is named on the device.

Extracted from the native host so the CLI, the TUI, the host and the tests all
agree. If sync, status and remove ever disagreed about what a book is called on
the device, the shelf would show phantom entries and remove would miss.
"""
import re
import unicodedata
from pathlib import Path

AUDIO_EXT = {".m4b", ".m4a", ".mp3", ".flac", ".wav", ".ogg", ".opus"}

# The device is usually FAT32 and the player's firmware is usually fussy.
_RESERVED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(s):
    """ASCII-ish, filesystem-safe. Simple player firmware dislikes exotic
    characters, and FAT32 forbids several outright."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = _RESERVED.sub("", s)
    return re.sub(r"\s+", " ", s).strip() or "Untitled"


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" or n >= 10 else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.0f}PB"


def out_ext(path, rename_m4b=True):
    """The extension a file should have on the device.

    The whole point of the tool: an .m4b IS an MP4/AAC container, and a player
    that rejects one is refusing the extension, not the codec.
    """
    e = Path(path).suffix.lower()
    return ".m4a" if (rename_m4b and e == ".m4b") else e


def source_ext(name):
    """Undo out_ext when pushing a file back to the server."""
    return ".m4b" if Path(name).suffix.lower() == ".m4a" else Path(name).suffix.lower()


def safe_subdir(s):
    """Reduce a configured subdir to a relative path that cannot escape."""
    parts = []
    for raw in re.split(r"[\\/]+", str(s or "")):
        if not raw or raw in (".", ".."):
            continue
        p = clean(raw)
        if p and p != "Untitled":
            parts.append(p)
    return Path(*parts) if parts else Path("AUDIOBOOKS")


def target_name(book, folder_template="{author} - {title}"):
    """The on-device folder/file stem for a book.

    `book` is anything with title/author/series/narrator keys - a server item
    or a device entry, so a round trip lands on the same name.
    """
    name = (folder_template or "{author} - {title}")
    for key in ("author", "title", "series", "narrator"):
        name = name.replace("{%s}" % key, str(book.get(key) or ""))
    return clean(name).strip(" -") or "Untitled"


def free_name(name, item, taken):
    """`name`, unless another book already holds it on the device.

    Two editions of one book - a full-cast recording and a single narrator -
    have the same author and title, so the template gives them the same
    name, and copying the second used to overwrite or merge into the first.
    The second gets the narrator added instead, or a number when that does
    not tell them apart.

    `taken` maps each name in use to the library item that holds it, or to
    None for something on the device this tool did not put there. That is
    left as it always was: most likely it is this same book, copied by hand.
    Names are compared ignoring case, as FAT32 and exFAT players do.
    """
    me = item.get("id")
    taken = {k.lower(): v for k, v in taken.items()}
    if taken.get(name.lower(), me) in (me, None):
        return name
    # clean() says "Untitled" when nothing survives it - a narrator written
    # in another script - and that tells two editions apart no better than a number.
    narrator = clean(item.get("narrator")) if item.get("narrator") else ""
    if narrator == "Untitled":
        narrator = ""
    tries = []
    if narrator and narrator.lower() not in name.lower():
        tries.append(f"{name} ({narrator})")
    tries += [f"{name} ({n})" for n in range(2, 1000)]
    for t in (clean(t) for t in tries):
        if taken.get(t.lower(), me) == me:
            return t
    raise ValueError(f"no free name for {name!r} on the device")


def _fold(s):
    """Lowercase, ASCII, punctuation-free, articles dropped."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    # Drop a trailing article *before* punctuation goes, so the comma in
    # "Hobbit, The" is still there to anchor on.
    s = re.sub(r",\s*(the|a|an)\s*$", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(the|a|an) ", "", s)
    return re.sub(r"\s+(the|a|an)$", "", s)


def norm_key(title, author=""):
    """A loose key for matching a side-loaded book against the server.

    Only used when the sidecar index has no record of a book - which means
    someone put it on the device by hand, so the only evidence is its tags.

    The author half drops spaces entirely: initials and apostrophes are written
    every which way ("J.R.R. Tolkien", "JRR Tolkien", "L'Engle", "LEngle") and
    the author is only ever a tie-breaker alongside the title anyway.
    """
    return (_fold(title), _fold(author).replace(" ", ""))
