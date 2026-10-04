#!/usr/bin/env python3
"""Test vectors that hold the Chromium folder backend to the helper's rules.

On Chrome the extension can write to the player itself (extension/src/folder.js)
instead of asking the helper. That is a second copy of naming, matching, tag
reading and the zip layout, and a second copy drifts. So the rules stay defined
once, here in Python, and this script records what the real absh code does
with a set of inputs:

    tests/fixtures/parity/naming.json   names, extensions, normalize_item,
                                        and the files pull/push/remove produce
    tests/fixtures/parity/device.json   scan + classification of device trees
    tests/fixtures/parity/tags.json     the built-in MP4/ID3 readers

tests/python/test_parity_vectors.py regenerates these and fails if the files
differ, so a change to absh/ must be followed by --write. The JavaScript tests
(tests/js/folder.test.js, and tests/e2e/folder.spec.js against a real browser
filesystem) assert the same files, so --write then fails them until folder.js
agrees. Neither side can change alone.

    python3 tools/parity_vectors.py --write    # after changing a rule
    python3 tools/parity_vectors.py --check    # what the test does

Everything is computed by running absh itself - sync.pull on a real temporary
directory, device.scan on real files - never by restating a rule here. Tags
are read with mutagen switched off: the browser can only ever match the
built-in parsers, and the helper reports "tags: builtin" without it.
"""
import argparse
import base64
import io
import json
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from absh import device as device_mod   # noqa: E402
from absh import index as index_mod     # noqa: E402
from absh import naming, sync, tags     # noqa: E402
from absh.abs_api import normalize_item  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "parity"
B64 = lambda b: base64.b64encode(b).decode()   # noqa: E731


# ------------------------------------------------------------ file makers
def atom(name, payload):
    return struct.pack(">I", len(payload) + 8) + name + payload


def data_atom(text, raw=None):
    body = raw if raw is not None else text.encode("utf-8")
    return atom(b"data", struct.pack(">II", 1, 0) + body)


def mp4(fields, *, mdat_before=0, big_moov=False, extra=b""):
    """A real atom tree. `fields` maps atom names to text or raw bytes."""
    ilst = b"".join(atom(k, data_atom(None, v if isinstance(v, bytes) else v.encode()))
                    for k, v in fields.items())
    meta = atom(b"meta", b"\x00\x00\x00\x00" + atom(b"ilst", ilst))
    moov_body = atom(b"udta", meta)
    if big_moov:
        # A 64-bit size: size field 1, then the real size in 8 more bytes.
        moov = struct.pack(">I", 1) + b"moov" + struct.pack(">Q", len(moov_body) + 16) + moov_body
    else:
        moov = atom(b"moov", moov_body)
    head = atom(b"ftyp", b"M4A \x00\x00\x00\x00")
    if mdat_before:
        head += atom(b"mdat", b"\x07" * mdat_before)
    return head + moov + extra


def id3(frames, major=3, pad=0):
    body = b""
    for fid, payload in frames:
        size = (struct.pack(">I", len(payload)) if major < 4 else
                bytes((len(payload) >> s) & 0x7F for s in (21, 14, 7, 0)))
        body += fid + size + b"\x00\x00" + payload
    body += b"\x00" * pad
    header = b"ID3" + bytes([major, 0, 0]) + bytes(
        (len(body) >> s) & 0x7F for s in (21, 14, 7, 0))
    return header + body + b"\xff\xfb\x90\x00" * 4


def latin1(text):
    return b"\x00" + text.encode("latin-1")


def zipped(members, *, compression=zipfile.ZIP_STORED, zip64=False):
    """members: [(name or raw cp437 bytes, payload)]. Fixed timestamps, so the
    bytes - and the fixture - are the same on every run.

    A bytes name is stored the way old archivers wrote names: cp437, with no
    UTF-8 flag. zipfile only ever writes non-ASCII names as UTF-8, and the
    way round that used to be overriding its private _encodeFilenameFlags -
    which Python 3.13.15 stopped consulting for the local header, so that
    header claimed UTF-8 while the central directory did not, and the fixture
    changed with the Python patch release. So such a member is written under
    an ASCII placeholder of the same byte length, and its real bytes are
    patched into both headers afterwards: nothing private is relied on, the
    offsets do not move, and the result is checked before it is returned."""
    buf = io.BytesIO()
    legacy = {}                       # index -> raw cp437 name bytes
    with zipfile.ZipFile(buf, "w", compression=compression) as z:
        for i, (name, payload) in enumerate(members):
            if isinstance(name, bytes):
                legacy[i] = name
                fill = chr(ord("a") + i % 26)
                text = (fill * (len(name) - 1) + "/") if name.endswith(b"/") else fill * len(name)
            else:
                text = name
            info = zipfile.ZipInfo(text, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = compression
            if text.endswith("/"):
                z.writestr(info, payload)
                continue
            with z.open(info, "w", force_zip64=zip64) as fh:
                fh.write(payload)
    data = bytearray(buf.getvalue())
    if legacy:
        _patch_names(data, legacy)
    return bytes(data)


def _patch_names(data, legacy):
    """Put each legacy name's raw bytes into its local and central headers."""
    import struct
    zf = zipfile.ZipFile(io.BytesIO(bytes(data)))
    infos = zf.infolist()
    pos = zf.start_dir
    for i, info in enumerate(infos):
        if data[pos:pos + 4] != b"PK\x01\x02":
            raise SystemExit(f"parity_vectors: no central entry {i} at {pos}")
        n, m, k = struct.unpack("<HHH", data[pos + 28:pos + 34])
        if i in legacy:
            raw = legacy[i]
            local = info.header_offset
            ln = struct.unpack("<H", data[local + 26:local + 28])[0]
            if n != len(raw) or ln != len(raw):
                raise SystemExit(f"parity_vectors: name length moved for member {i}")
            data[pos + 46:pos + 46 + n] = raw
            data[local + 30:local + 30 + ln] = raw
            for hdr, flags_at in ((pos, 8), (local, 6)):
                flags = struct.unpack("<H", data[hdr + flags_at:hdr + flags_at + 2])[0]
                data[hdr + flags_at:hdr + flags_at + 2] = struct.pack("<H", flags & ~0x800)
        pos += 46 + n + m + k
    # Read it back the way absh will: every legacy name decodes as cp437 from
    # both headers, which zipfile checks against each other when it opens one.
    check = zipfile.ZipFile(io.BytesIO(bytes(data)))
    for i, info in enumerate(check.infolist()):
        if i in legacy:
            if info.flag_bits & 0x800 or info.filename != legacy[i].decode("cp437"):
                raise SystemExit(f"parity_vectors: member {i} did not come out as cp437")
            if not info.filename.endswith("/"):
                check.read(info)


def audio(n, seed=1):
    return bytes((seed * 31 + i * 7) % 251 for i in range(n))


# ------------------------------------------------------------ tree helpers
def portable(tree):
    """Refuse a tree whose sibling names sort differently ignoring case.

    pathlib sorts case-insensitively on Windows and case-sensitively
    elsewhere, so such a tree scans in a different order on a Windows runner
    and the fixture could not hold on every OS. The browser port follows the
    POSIX order; the difference for mixed-case names is real, and deliberately
    not what these vectors are about."""
    kids = {}
    for rel in tree:
        parts = rel.split("/")
        for i in range(len(parts)):
            kids.setdefault("/".join(parts[:i]), set()).add(parts[i])
    for parent, names in kids.items():
        if sorted(names) != sorted(names, key=str.lower):
            raise SystemExit(f"names under {parent or '.'!r} sort differently ignoring "
                             f"case: {sorted(names)}")
    return tree


def build_tree(base, tree):
    """tree: {"relative/path": base64 | None}. None makes a directory."""
    portable(tree)
    for rel, b64 in sorted(tree.items()):
        p = base / rel
        if b64 is None:
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(b64))


def read_tree(base):
    """Every file and directory under base, except the sidecar index, which
    is reported on its own and carries a timestamp."""
    files, dirs = {}, []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        if rel == ".absh" or rel.startswith(".absh/"):
            continue
        if p.is_dir():
            dirs.append(rel)
        else:
            files[rel] = p.stat().st_size
    return {"files": files, "dirs": dirs}


def read_index(base):
    entries = index_mod.load(str(base)).get("entries", {})
    return {k: {f: v for f, v in rec.items() if f != "syncedAt"}
            for k, rec in sorted(entries.items())}


def index_tree(entries):
    """A sidecar index as base64, for trees that start with one."""
    raw = json.dumps({"version": 1, "entries": entries}).encode()
    return {".absh/index.json": B64(raw)}


class FakeResponse:
    def __init__(self, body, headers):
        self._body = body
        self.headers = headers

    def read(self, n=-1):
        if n is None or n < 0:
            out, self._body = self._body, b""
            return out
        out, self._body = self._body[:n], self._body[n:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeClient:
    """Serves the vector's download exactly as the browser will receive it:
    the same body bytes and the same two headers."""

    def __init__(self, downloads=None):
        self.downloads = downloads or {}
        self.uploads = []

    def open_download(self, item_id, timeout=None):
        d = self.downloads[item_id]
        headers = {"Content-Type": d.get("contentType", "")}
        if d.get("disposition") is not None:
            headers["Content-Disposition"] = d["disposition"]
        return FakeResponse(base64.b64decode(d["body"]), headers)

    def upload(self, library_id, folder_id, title, author, files, series=None, timeout=None):
        self.uploads.append({
            "library": library_id, "folder": folder_id, "title": title,
            "author": author, "series": series,
            "files": [[name, Path(p).stat().st_size] for name, p in files]})
        return {"id": "li_new"}


def report_view(rep):
    return {k: rep[k] for k in ("copied", "skipped", "uploaded", "removed", "freed",
                                "errors", "books")}


# ------------------------------------------------------------ naming.json
CLEAN = [
    "Brian Jacques", "Pema Chödrön", 'a/b\\c:d*e?f"g<h>i|j', "", "   ", "///",
    "Tab\tand\nnewline", "  spaced   out  ", "Ærøskøbing", "ﬁligree ﬀ",
    "Ｆｕｌｌｗｉｄｔｈ", "日本語", "Crème brûlée – a novel", "x\x7fy",
    "über alles", "A B", "😀 emoji", "İstanbul", "naïve café", "Æsop",
    "L'Engle", "1/2 · 3", "\x1cfile\x1fsep", "trailing dot.", "..", "ﬀ́",
]

BOOKS = [
    ({"author": "Brian Jacques", "title": "Redwall"}, None),
    ({"author": "", "title": "Holes"}, "{author} - {title}"),
    ({"author": "Louis Sachar", "title": ""}, "{author} - {title}"),
    ({"author": "{title}", "title": "Echo"}, "{author} - {title}"),
    ({"author": "A", "title": "{author}"}, "{author} - {title}"),
    ({"author": "Brian Jacques", "title": "Redwall", "series": "Redwall 1"},
     "{series}/{title}"),
    ({"author": "Tolkien", "title": "The Hobbit", "series": None}, "{author}/{series}/{title}"),
    ({"author": "X", "title": "Y"}, ""),
    ({"title": "Only a title"}, "{title} ({author})"),
    ({"author": "  - - ", "title": " - "}, "{author} - {title}"),
    ({"author": "Pema Chödrön", "title": "When Things Fall Apart: Heart Advice"}, None),
    ({"author": "N", "title": "--Dashes--"}, "--{title}--"),
    ({"author": "Ann", "title": "Book", "series": "S"}, "{title}{title} [{series}]"),
    ({"author": "Ann", "title": 'What? "No" <way> | maybe*'}, "{author} - {title}"),
    ({"author": "日本", "title": "本"}, None),
    ({"author": "A", "title": "B"}, "{unknown} {title}"),
    # Only spaces and hyphens are stripped from the ends - nothing else.
    ({"author": "_", "title": "_Under_."}, None),
    ({"author": "", "title": "-.-"}, "{author}--{title}--"),
    ({"author": "'", "title": "(Brackets)"}, "{author}{title}"),
]

SUBDIRS = ["AUDIOBOOKS", "", None, "../../etc", "../..", "/etc/passwd", "Music\\Books",
           "a//b/./c", "  Audio Books  ", "Untitled", "Untitled/x", "...", "日本",
           "Bücher/Hörbücher", "a:b/c*d", "\\\\server\\share"]

PATHS = ["Book.m4b", "Book.M4B", ".hidden", "noext", "a.b.c", "dir/x.mp3",
         "Disc 1/01 - One.m4b", "./a.mp3", "a//b.mp3", ".m4b", "x.tar.gz", "..",
         "a/..", "dir/", "Café.Mp3", "a/b/c/d.flac"]

EXTS = [("Book.m4b", True), ("Book.m4b", False), ("Book.M4B", True), ("Book.m4a", True),
        ("Book.mp3", True), ("noext", True), (".m4b", True), ("dir/x.M4b", True),
        ("Book.M4A", False)]

KEYS = [("The Hobbit", "J.R.R. Tolkien"), ("Hobbit, The", "JRR Tolkien"),
        ("A Wrinkle in Time", "L'Engle"), ("Wrinkle in Time, A", "LEngle"),
        ("Redwall", "Brian Jacques"), ("redwall", "brian  jacques"), ("The", ""),
        ("A", "An"), ("", ""), ("The A Team", "B. A."), ("An Ant", "Anne, The"),
        ("Tolkien, J.R.R.", "x"), ("Crème Brûlée", "Ærø"), ("  The  End  ", None),
        ("War and Peace, the", "Tolstoy, Leo"), ("the  ", "the"), (None, None),
        ("Title — Subtitle", "Ün Ïcode"), ("A, an", "a")]

ITEMS = [
    {"id": "li_1", "relPath": "Brian Jacques/Redwall", "size": 100,
     "media": {"numTracks": 1, "metadata": {"title": "Redwall", "authorName": "Brian Jacques",
                                            "seriesName": "Redwall #1"}}},
    {"id": "li_2", "relPath": "x", "media": {"size": 55, "audioFiles": [{}, {}],
     "metadata": {"title": "", "authors": [{"name": "A"}, {"name": ""}, {"name": "B"}],
                  "series": [{"name": "S1"}, "S2", ""]}}},
    {"id": "li_3"},
    {"relPath": "No id here", "media": {"metadata": {}}},
    {"id": "li_4", "media": {"numTracks": 0, "metadata": {"title": "T", "authorName": "",
                                                           "authors": [{"name": "Late"}]}}},
]


def pull_cases():
    hobbit = {"id": "bk_h", "title": "The Hobbit", "author": "J.R.R. Tolkien", "series": ""}
    redwall = {"id": "bk_r", "title": "Redwall", "author": "Brian Jacques", "series": "Redwall"}
    odd = {"id": "bk_o", "title": 'Who? Me: "Yes" <No>', "author": "Pema Chödrön", "series": ""}
    one = audio(300)
    disc = [("The Hobbit/Disc 1/01 - Chapter One.m4b", audio(120, 2)),
            ("The Hobbit/Disc 1/02 - Chapter Two.M4B", audio(130, 3)),
            ("The Hobbit/Disc 2/10 - Chapter Ten.m4b", audio(140, 4)),
            ("The Hobbit/Disc 2/9 - Chapter Nine.m4b", audio(150, 5)),
            ("The Hobbit/cover.jpg", b"jpeg"),
            ("The Hobbit/", b"")]
    single = lambda body, name, ctype="audio/mp4": {   # noqa: E731
        "contentType": ctype, "disposition": f'attachment; filename="{name}"', "body": B64(body)}
    return [
        {"name": "single m4b renamed", "items": [redwall], "opts": {},
         "downloads": {"bk_r": single(one, "Redwall.m4b")}},
        {"name": "single m4b kept", "items": [redwall], "opts": {"renameM4b": False},
         "downloads": {"bk_r": single(one, "Redwall.m4b")}},
        {"name": "utf-8 disposition, mp3", "items": [odd], "opts": {},
         "downloads": {"bk_o": {"contentType": "audio/mpeg",
                                "disposition": "attachment; filename*=UTF-8''Caf%C3%A9.mp3",
                                "body": B64(one)}}},
        {"name": "no disposition at all", "items": [redwall], "opts": {},
         "downloads": {"bk_r": {"contentType": "audio/mp4", "disposition": None,
                                "body": B64(one)}}},
        {"name": "template and subdir", "items": [redwall],
         "opts": {"folderTemplate": "{series}/{title} by {author}", "subdir": "../Music//Books"},
         "downloads": {"bk_r": single(one, "Redwall.M4B")}},
        {"name": "zip, stored, discs and a cover", "items": [hobbit], "opts": {},
         "downloads": {"bk_h": {"contentType": "application/zip",
                                "disposition": 'attachment; filename="The Hobbit.zip"',
                                "body": B64(zipped(disc))}}},
        {"name": "zip, deflated, not renamed", "items": [hobbit], "opts": {"renameM4b": False},
         "downloads": {"bk_h": {"contentType": "application/zip",
                                "disposition": 'attachment; filename="The Hobbit.zip"',
                                "body": B64(zipped(disc, compression=zipfile.ZIP_DEFLATED))}}},
        {"name": "zip found by its name, not its type", "items": [hobbit], "opts": {},
         "downloads": {"bk_h": {"contentType": "application/octet-stream",
                                "disposition": 'attachment; filename="book.ZIP"',
                                "body": B64(zipped(disc[:2], compression=zipfile.ZIP_DEFLATED))}}},
        {"name": "zip64 records", "items": [hobbit], "opts": {},
         "downloads": {"bk_h": {"contentType": "application/zip", "disposition": None,
                                "body": B64(zipped(disc[:3], zip64=True,
                                                   compression=zipfile.ZIP_DEFLATED))}}},
        {"name": "zip names: legacy cp437 and utf-8", "items": [odd], "opts": {},
         "downloads": {"bk_o": {"contentType": "application/zip", "disposition": None,
                                "body": B64(zipped([(b"Caf\x82 \x9a.mp3", audio(90)),
                                                    ("Crème/brûlée ☕.m4b", audio(80)),
                                                    ("a/b/c/../Dup?.mp3", audio(70))]))}}},
        {"name": "zip with no audio", "items": [hobbit], "opts": {},
         "downloads": {"bk_h": {"contentType": "application/zip", "disposition": None,
                                "body": B64(zipped([("readme.txt", b"nothing")]))}}},
        {"name": "pulled twice: the index skips the second", "items": [redwall, redwall],
         "opts": {}, "downloads": {"bk_r": single(one, "Redwall.m4b")}},
        {"name": "same name and size already there", "items": [redwall], "opts": {},
         "tree": {"AUDIOBOOKS/Brian Jacques - Redwall.m4a": B64(audio(300, 9))},
         "downloads": {"bk_r": single(one, "Redwall.m4b")}},
        {"name": "same name, different size, replaced", "items": [redwall], "opts": {},
         "tree": {"AUDIOBOOKS/Brian Jacques - Redwall.m4a": B64(b"short")},
         "downloads": {"bk_r": single(one, "Redwall.m4b")}},
        {"name": "zip into a folder that has some parts", "items": [hobbit], "opts": {},
         "tree": {"AUDIOBOOKS/J.R.R. Tolkien - The Hobbit/001 - 01 - Chapter One.m4a":
                  B64(b"partial")},
         "downloads": {"bk_h": {"contentType": "application/zip", "disposition": None,
                                "body": B64(zipped(disc[:2]))}}},
    ]


def run_pull(case):
    base = Path(tempfile.mkdtemp())
    try:
        build_tree(base, case.get("tree") or {})
        opts = {"devicePath": str(base), "subdir": "AUDIOBOOKS", "renameM4b": True,
                "folderTemplate": "{author} - {title}", **case["opts"]}
        events = []
        rep = sync.pull(FakeClient(case["downloads"]), case["items"], opts, events.append)
        return {"report": report_view(rep), "tree": read_tree(base),
                "index": read_index(base), "events": events}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def push_cases():
    sil = mp4({b"\xa9nam": "The Silmarillion", b"aART": "J.R.R. Tolkien"})
    return [
        {"name": "tagged m4a, rename undone", "push": ["scruffy_rip.m4a"],
         "tree": {"AUDIOBOOKS/scruffy_rip.m4a": B64(sil)}},
        {"name": "uppercase extension", "push": ["LOUD.M4A"],
         "tree": {"AUDIOBOOKS/LOUD.M4A": B64(sil)}},
        {"name": "untagged mp3, titled from its name", "push": [],
         "tree": {"AUDIOBOOKS/Some Book.mp3": B64(b"no tags")}},
        {"name": "folder of parts, album is the title", "push": ["Big Book"],
         "tree": {"AUDIOBOOKS/Big Book/02.m4a": B64(mp4({b"\xa9nam": "Part 2",
                                                          b"\xa9alb": "Big", b"aART": "Au"})),
                  "AUDIOBOOKS/Big Book/01.m4a": B64(mp4({b"\xa9nam": "Part 1",
                                                          b"\xa9alb": "Big", b"aART": "Au"})),
                  "AUDIOBOOKS/Big Book/cover.jpg": B64(b"x"),
                  "AUDIOBOOKS/Big Book/sub/03.mp3": B64(b"tagless")}},
        {"name": "a book this tool put there keeps its index record", "push": ["Holes.m4a"],
         "tree": {"AUDIOBOOKS/Holes.m4a": B64(audio(50)),
                  **index_tree({"Holes.m4a": {"itemId": "li_x", "title": "Holes",
                                              "author": "Louis Sachar", "series": "Camp"}})}},
    ]


def run_push(case):
    base = Path(tempfile.mkdtemp())
    try:
        build_tree(base, case["tree"])
        opts = {"devicePath": str(base), "subdir": "AUDIOBOOKS", "renameM4b": True,
                "restoreM4b": True, "folderTemplate": "{author} - {title}",
                "libraryId": "lib1", "folderId": "fol1"}
        entries = device_mod.scan(str(base), "AUDIOBOOKS")
        want = set(case["push"])
        chosen = [e for e in entries if not want or e["name"] in want]
        client = FakeClient()
        events = []
        rep = sync.push(client, chosen, opts, events.append)
        return {"report": report_view(rep), "uploads": client.uploads, "events": events}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def remove_cases():
    tree = {"AUDIOBOOKS/A - One.m4a": B64(audio(10)),
            "AUDIOBOOKS/B - Two/001.m4a": B64(audio(20)),
            "AUDIOBOOKS/B - Two/sub/002.m4a": B64(audio(30)),
            "AUDIOBOOKS/B - Two/.hidden": B64(audio(5)),
            "AUDIOBOOKS/keep.mp3": B64(audio(7)),
            "outside.mp3": B64(audio(3)),
            **index_tree({"A - One.m4a": {"itemId": "i1"}, "B - Two": {"itemId": "i2"},
                          "keep.mp3": {"itemId": "i3"}})}
    return [
        {"name": "a file and a folder", "names": ["A - One.m4a", "B - Two"], "tree": tree},
        {"name": "nothing that is not a single entry",
         "names": ["..", ".", "", "a/b", "/outside.mp3", "../outside.mp3", "B - Two/001.m4a",
                   "missing.m4a", "keep.mp3"], "tree": tree},
        {"name": "no books folder at all", "names": ["anything"],
         "tree": {"other/x.mp3": B64(b"x")}},
    ]


def run_remove(case):
    base = Path(tempfile.mkdtemp())
    try:
        build_tree(base, case["tree"])
        opts = {"devicePath": str(base), "subdir": "AUDIOBOOKS"}
        events = []
        rep = sync.remove(case["names"], opts, events.append)
        return {"report": report_view(rep), "tree": read_tree(base),
                "index": read_index(base), "events": events}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def same_archive(a_b64, b_b64):
    """The same members, names, flags, methods and contents - however zlib
    chose to encode them."""
    if a_b64 == b_b64:
        return True
    try:
        za = zipfile.ZipFile(io.BytesIO(base64.b64decode(a_b64)))
        zb = zipfile.ZipFile(io.BytesIO(base64.b64decode(b_b64)))
        view = lambda z: [(i.filename, i.flag_bits & 0x800, i.compress_type, z.read(i))  # noqa: E731
                          for i in z.infolist()]
        return view(za) == view(zb)
    except (zipfile.BadZipFile, ValueError):
        return False


def pinned(cases, committed):
    """Keep the committed bytes of an archive that has not really changed.

    The zips are built by this machine's zipfile and zlib, and the bytes of a
    deflate stream are not promised to be the same across versions. They are
    inputs, not answers: what has to hold on every OS and Python is what absh
    does with them. So when a freshly built archive holds exactly what the
    committed one does, the committed bytes are used, and absh is run over
    those."""
    old = {c["name"]: c for c in (committed or [])}
    out = []
    for c in cases:
        prev = (old.get(c["name"]) or {}).get("downloads", {})
        downloads = {}
        for item_id, d in c.get("downloads", {}).items():
            p = prev.get(item_id)
            same = (p and {k: v for k, v in p.items() if k != "body"} ==
                    {k: v for k, v in d.items() if k != "body"} and
                    same_archive(p["body"], d["body"]))
            downloads[item_id] = p if same else d
        out.append({**c, "downloads": downloads})
    return out


def naming_vectors(committed=None):
    p = lambda s: naming.Path(s)   # noqa: E731
    pulls = pinned(pull_cases(), (committed or {}).get("pull"))
    return {
        "clean": [{"in": s, "out": naming.clean(s)} for s in CLEAN],
        "targetName": [{"book": b, "template": t, "out": naming.target_name(b, t)}
                       for b, t in BOOKS],
        "safeSubdir": [{"in": s, "out": list(naming.safe_subdir(s).parts)} for s in SUBDIRS],
        "path": [{"in": s, "name": p(s).name, "stem": p(s).stem, "suffix": p(s).suffix}
                 for s in PATHS],
        "outExt": [{"in": s, "rename": r, "out": naming.out_ext(s, r)} for s, r in EXTS],
        "sourceExt": [{"in": s, "out": naming.source_ext(s)}
                      for s in ("X.m4a", "X.M4A", "X.mp3", "X.m4b", "noext", "a/b.M4a")],
        "normKey": [{"title": t, "author": a, "out": list(naming.norm_key(t, a))}
                    for t, a in KEYS],
        "normalizeItem": [{"in": it, "out": normalize_item(it)} for it in ITEMS],
        "pull": [{**c, "expect": run_pull(c)} for c in pulls],
        "push": [{**c, "expect": run_push(c)} for c in push_cases()],
        "remove": [{**c, "expect": run_remove(c)} for c in remove_cases()],
    }


# ------------------------------------------------------------ device.json
SERVER = [
    {"id": "li_r", "title": "Redwall", "author": "Brian Jacques", "series": "Redwall",
     "relPath": "", "numTracks": 1, "size": 1},
    {"id": "li_h", "title": "Holes", "author": "Louis Sachar", "series": "",
     "relPath": "", "numTracks": 1, "size": 1},
    {"id": "li_t", "title": "The Hobbit", "author": "J.R.R. Tolkien", "series": "",
     "relPath": "", "numTracks": 1, "size": 1},
    {"id": "li_m", "title": "Mossflower", "author": "Brian Jacques", "series": "Redwall",
     "relPath": "", "numTracks": 1, "size": 1},
    {"id": "li_w", "title": "A Wrinkle in Time", "author": "Madeleine L'Engle", "series": "",
     "relPath": "", "numTracks": 1, "size": 1},
    {"id": "li_d1", "title": "Duplicate", "author": "Twin", "series": "",
     "relPath": "", "numTracks": 1, "size": 1},
    {"id": "li_d2", "title": "Duplicate", "author": "Twin", "series": "",
     "relPath": "", "numTracks": 1, "size": 1},
]


def device_cases():
    m = lambda **kw: B64(mp4({k: v for k, v in kw.items()}))   # noqa: E731
    mixed = {
        # Put there by this tool: the index says exactly what it is.
        "AUDIOBOOKS/Brian Jacques - Redwall.m4a": B64(audio(64)),
        # Side-loaded with a scruffy name; its tags say Holes.
        "AUDIOBOOKS/Holes_rip.m4a": B64(mp4({b"\xa9nam": "Holes", b"aART": "Louis Sachar"})),
        # Unknown to the server.
        "AUDIOBOOKS/Scruffy_rip.m4a": B64(mp4({b"\xa9nam": "The Silmarillion",
                                               b"\xa9ART": "J.R.R. Tolkien"})),
        # Right name, no tags.
        "AUDIOBOOKS/Brian Jacques - Mossflower.mp3": B64(b"no tags at all"),
        # Tags that only match loosely: articles and initials.
        "AUDIOBOOKS/Wrinkle.mp3": B64(id3([(b"TIT2", latin1("Wrinkle in Time, A")),
                                           (b"TPE2", latin1("Madeleine LEngle"))])),
        # A folder of parts: the album is the book.
        "AUDIOBOOKS/Hobbit parts/Disc 1/01.m4a": B64(mp4({b"\xa9nam": "Chapter 1",
                                                          b"\xa9alb": "Hobbit, The",
                                                          b"aART": "JRR Tolkien"})),
        "AUDIOBOOKS/Hobbit parts/Disc 1.m4a": B64(mp4({b"\xa9nam": "Sorts after the folder"})),
        "AUDIOBOOKS/Hobbit parts/Disc 10/01.m4a": B64(audio(11)),
        "AUDIOBOOKS/Hobbit parts/Disc 2/01.m4a": B64(audio(12)),
        "AUDIOBOOKS/Hobbit parts/._01.m4a": B64(audio(4)),
        "AUDIOBOOKS/Hobbit parts/Cover.JPG": B64(audio(9)),
        # Clutter the shelf ignores.
        "AUDIOBOOKS/Player.db": B64(b"db"),
        "AUDIOBOOKS/Cover.jpg": B64(b"jpg"),
        "AUDIOBOOKS/.Trashes/x.mp3": B64(b"t"),
        "AUDIOBOOKS/.Hidden.mp3": B64(b"h"),
        "AUDIOBOOKS/Empty folder": None,
        "AUDIOBOOKS/Only a cover/cover.jpg": B64(b"c"),
        # A folder whose name has a dot, recorded in the index without a title.
        "AUDIOBOOKS/Vol.2 of Something": None,
        "AUDIOBOOKS/Vol.2 of Something/a.OPUS": B64(audio(20)),
        "AUDIOBOOKS/Twin - Duplicate.flac": B64(audio(13)),
        "AUDIOBOOKS/Untagged.WAV": B64(audio(14)),
        "elsewhere.mp3": B64(audio(15)),
        **index_tree({
            "Brian Jacques - Redwall.m4a": {"itemId": "li_r", "title": "Redwall",
                                            "author": "Brian Jacques", "series": "Redwall"},
            "Vol.2 of Something": {"itemId": "li_gone", "title": ""},
            "Deleted in Finder.m4a": {"itemId": "li_h", "title": "Holes"},
            "Empty record.m4a": {},
        }),
    }
    return [
        {"name": "the full mix", "subdir": "AUDIOBOOKS", "template": "{author} - {title}",
         "readTags": True, "tree": mixed},
        {"name": "the full mix without reading tags", "subdir": "AUDIOBOOKS",
         "template": "{author} - {title}", "readTags": False, "tree": mixed},
        {"name": "a different template", "subdir": "AUDIOBOOKS", "template": "{title}",
         "readTags": True,
         "tree": {"AUDIOBOOKS/Holes.m4b": B64(audio(5)),
                  "AUDIOBOOKS/Redwall": None, "AUDIOBOOKS/Redwall/1.mp3": B64(audio(6))}},
        {"name": "a nested books folder", "subdir": "Music\\Books/./", "template": None,
         "readTags": True,
         "tree": {"Music/Books/Louis Sachar - Holes.m4a": B64(audio(7)),
                  "Music/Other.m4a": B64(audio(8))}},
        {"name": "no books folder", "subdir": "AUDIOBOOKS", "template": None, "readTags": True,
         "tree": {"MUSIC/x.mp3": B64(audio(3))}},
        {"name": "an unreadable index", "subdir": "AUDIOBOOKS", "template": None,
         "readTags": True,
         "tree": {"AUDIOBOOKS/Brian Jacques - Redwall.m4a": B64(audio(3)),
                  ".absh/index.json": B64(b"\xef\xbb\xbf{\"entries\": {}}")}},
        {"name": "an index that is not an object", "subdir": "AUDIOBOOKS", "template": None,
         "readTags": True,
         "tree": {"AUDIOBOOKS/x.mp3": B64(audio(3)),
                  ".absh/index.json": B64(b"[1, 2, 3]")}},
    ]


def run_device(case):
    base = Path(tempfile.mkdtemp())
    try:
        build_tree(base, case["tree"])
        template = case["template"]
        entries = device_mod.scan(str(base), case["subdir"], template,
                                  read_tags=case["readTags"])
        view = [{**{k: v for k, v in e.items() if k != "paths"},
                 "paths": [Path(p).relative_to(base).as_posix() for p in e["paths"]]}
                for e in entries]
        d = device_mod.diff(SERVER, entries, template)
        return {
            "scan": view,
            "both": [{"name": b["name"], "itemId": b["itemId"], "matchedBy": b["matchedBy"]}
                     for b in d["both"]],
            "serverOnly": [i["id"] for i in d["serverOnly"]],
            "deviceOnly": [e["name"] for e in d["deviceOnly"]],
            "index": read_index(base),
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def device_vectors():
    return {"server": SERVER,
            "cases": [{**c, "expect": run_device(c)} for c in device_cases()]}


# ------------------------------------------------------------ tags.json
def tag_files():
    big_mdat = mp4({b"\xa9nam": "After a big mdat", b"aART": "Late Moov"}, mdat_before=5000)
    truncated = mp4({b"\xa9nam": "Cut", b"aART": "Short"})[:-6]
    short_data = atom(b"\xa9nam", atom(b"data", b"\x00\x00\x00\x01\x00\x00\x00\x00") +
                      data_atom("Second data wins"))
    ilst_raw = short_data + atom(b"aART", data_atom("Raw"))
    raw_meta = atom(b"meta", b"\x00\x00\x00\x00" + atom(b"ilst", ilst_raw))
    custom = atom(b"ftyp", b"M4A ") + atom(b"moov", atom(b"udta", raw_meta))
    size_zero = atom(b"ftyp", b"M4A ") + struct.pack(">I", 0) + b"moov" + atom(
        b"udta", atom(b"meta", b"\x00" * 4 + atom(b"ilst", atom(b"\xa9nam", data_atom("Zero")))))
    two_moovs = (atom(b"ftyp", b"M4A ") + atom(b"moov", atom(b"trak", b"")) +
                 mp4({b"\xa9nam": "Second moov"})[16:])
    twice = atom(b"\xa9nam", data_atom("First")) + atom(b"\xa9nam", data_atom("Second"))
    later_wins = atom(b"ftyp", b"M4A ") + atom(b"moov", atom(b"udta", atom(
        b"meta", b"\x00" * 4 + atom(b"ilst", twice))))
    return [
        ("basic.m4a", mp4({b"\xa9nam": "Redwall", b"aART": "Brian Jacques"})),
        ("artist only.m4b", mp4({b"\xa9nam": "T", b"\xa9ART": "Narrator"})),
        ("album artist wins.m4a", mp4({b"\xa9ART": "Narrator", b"aART": "Author",
                                       b"\xa9wrt": "Composer", b"\xa9alb": "Alb"})),
        ("composer only.mp4", mp4({b"\xa9nam": "C", b"\xa9wrt": "Composer"})),
        ("later atom wins.m4p", later_wins),
        ("64-bit moov.m4a", mp4({b"\xa9nam": "Big size", b"aART": "Q"}, big_moov=True)),
        ("moov at the end.m4a", big_mdat),
        ("truncated.m4a", truncated),
        ("short data atom.m4a", custom),
        ("size zero atom.m4a", size_zero),
        ("first moov has no tags.m4a", two_moovs),
        ("bad utf-8.m4a", mp4({b"\xa9nam": b"ok \xff\xfe bad \xed\xa0\x80 end \xe2\x82",
                               b"aART": b"\xc3\xa9t\xc3\xa9"})),
        ("whitespace.m4a", mp4({b"\xa9nam": " \x1c﻿Title \x85 ".encode(),
                                b"aART": "　Wide "})),
        ("UPPER.M4A", mp4({b"\xa9nam": "Upper"})),
        ("no moov.m4a", atom(b"ftyp", b"M4A ") + atom(b"mdat", b"x" * 40)),
        ("garbage.m4a", b"\x00\x00\x00\x02junk"),
        ("empty.m4a", b""),
        ("latin1.mp3", id3([(b"TIT2", latin1("Café ")), (b"TPE1", b"\x00\x80\x93\x9f\xe9\xff"),
                            (b"TALB", latin1("Album"))])),
        ("v24.mp3", id3([(b"TIT2", b"\x03" + "Ünïcode v2.4".encode()),
                         (b"TPE2", b"\x03\xef\xbb\xbfBOM kept")], major=4)),
        ("utf16 le bom.mp3", id3([(b"TIT2", b"\x01" + "﻿Little".encode("utf-16-le")),
                                  (b"TPE1", b"\x01" + b"\xfe\xff" + "Big".encode("utf-16-be"))])),
        ("utf16 no bom.mp3", id3([(b"TIT2", b"\x01" + "Native".encode("utf-16-le") + b"\x41")])),
        ("utf16be.mp3", id3([(b"TIT2", b"\x02" + "﻿BE keeps BOM".encode("utf-16-be")),
                             (b"TPE1", b"\x02" + b"\xd8\x00\x00A")])),
        ("nuls.mp3", id3([(b"TIT2", b"\x00One\x00Two\x00"), (b"TPE1", b"\x03\x00Lead")])),
        ("padding stops it.mp3", id3([(b"TIT2", latin1("Before"))], pad=30)),
        ("zero size frame.mp3", id3([(b"TIT2", latin1("Kept")), (b"TPE1", b"")])),
        ("oversized frame.mp3", id3([(b"TIT2", latin1("Kept too"))])[:-20]),
        ("album artist wins.mp3", id3([(b"TPE1", latin1("Narr")), (b"TPE2", latin1("Auth")),
                                       (b"TRCK", latin1("3/10"))])),
        ("unknown encoding.mp3", id3([(b"TIT2", b"\x07" + "Treated as UTF-8 ✓".encode())])),
        ("v22.mp3", id3([(b"TIT2", latin1("v2.2 parsed as v2.3"))], major=2)),
        ("UPPER.MP3", id3([(b"TIT2", latin1("Upper mp3"))])),
        ("not id3.mp3", b"\xff\xfb\x90\x00" * 10),
        ("short.mp3", b"ID3\x03"),
        ("tagged.flac", mp4({b"\xa9nam": "Never read"})),
        ("noext", mp4({b"\xa9nam": "Never read either"})),
    ]


def tag_vectors():
    base = Path(tempfile.mkdtemp())
    try:
        with mock.patch.object(tags, "mutagen", None):
            files = []
            for name, blob in tag_files():
                p = base / name
                p.write_bytes(blob)
                got = tags.read(p)
                files.append({"name": name, "b64": B64(blob),
                              "out": {k: got[k] for k in ("title", "author", "album")}})
            books = []
            for names in (["basic.m4a"], ["album artist wins.m4a", "basic.m4a"],
                          ["basic.m4a", "album artist wins.m4a"], ["garbage.m4a"],
                          ["no moov.m4a", "basic.m4a"], ["empty.m4a"], []):
                got = tags.read_book([base / n for n in names])
                books.append({"files": names,
                              "out": {k: got[k] for k in ("title", "author", "album")}})
        return {"files": files, "books": books}
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------------------ entry point
def generate():
    try:
        committed = json.loads((OUT / "naming.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        committed = None
    with mock.patch.object(tags, "mutagen", None):
        return {"naming.json": naming_vectors(committed), "device.json": device_vectors(),
                "tags.json": tag_vectors()}


def dumps(obj):
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="regenerate the fixture files")
    g.add_argument("--check", action="store_true", help="fail if they are out of date")
    a = ap.parse_args(argv)
    stale = []
    for name, data in generate().items():
        path = OUT / name
        text = dumps(data)
        if a.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"  wrote {path.relative_to(ROOT)}")
        elif not path.exists() or path.read_text(encoding="utf-8") != text:
            stale.append(str(path.relative_to(ROOT)))
    if stale:
        print("out of date - run: python3 tools/parity_vectors.py --write\n  " +
              "\n  ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
