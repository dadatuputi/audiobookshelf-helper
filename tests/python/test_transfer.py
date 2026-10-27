"""Writing Audiobookshelf's tags on copy, and moving only what changed.

The MP4s here are small but real: a sample table whose chunk offsets point
into mdat, so a test can tell whether the audio still lines up after the
tags moved things around.
"""
import argparse
import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from absh import device, embed, index, mp4meta, sync   # noqa: E402
from absh import index as index_mod            # noqa: E402


def atom(name, payload):
    return struct.pack(">I", len(payload) + 8) + name + payload


def text(key, value):
    return atom(key, atom(b"data", struct.pack(">II", 1, 0) + value.encode()))


AUDIO = bytes(range(256)) * 64            # 16 KB "audio", four 4 KB chunks


def mp4(faststart=True, title="Old Title", audio=AUDIO, wide=False):
    """ftyp, moov and mdat, in either order, with chunk offsets into mdat."""
    ilst = atom(b"ilst", text(b"\xa9nam", title) + text(b"\xa9too", "Lavf"))
    udta = atom(b"udta", atom(b"meta", b"\0" * 4 + ilst))
    ftyp = atom(b"ftyp", b"M4A \0\0\0\0M4A isom")
    chunks = 4
    step = len(audio) // chunks

    def moov_with(base):
        if wide:
            table = atom(b"co64", struct.pack(">II", 0, chunks) +
                         b"".join(struct.pack(">Q", base + i * step) for i in range(chunks)))
        else:
            table = atom(b"stco", struct.pack(">II", 0, chunks) +
                         b"".join(struct.pack(">I", base + i * step) for i in range(chunks)))
        stbl = atom(b"stbl", table)
        trak = atom(b"trak", atom(b"mdia", atom(b"minf", stbl)))
        return atom(b"moov", atom(b"mvhd", b"\0" * 100) + trak + udta)

    mdat = atom(b"mdat", audio)
    if faststart:
        size = len(moov_with(0))
        return ftyp + moov_with(len(ftyp) + size + 8) + mdat
    return ftyp + mdat + moov_with(len(ftyp) + 8)


def chunk_offsets(data):
    """The stco/co64 entries of the first moov, which is the one players read."""
    out = []
    seen = []

    def walk(start, end):
        i = start
        while i + 8 <= end:
            size, kind = struct.unpack(">I4s", data[i:i + 8])
            if kind == b"moov" and not seen:
                seen.append(i)
                walk(i + 8, i + size)
            elif kind in (b"trak", b"mdia", b"minf", b"stbl"):
                walk(i + 8, i + size)
            elif kind in (b"stco", b"co64"):
                n = struct.unpack(">I", data[i + 12:i + 16])[0]
                w = 8 if kind == b"co64" else 4
                fmt = ">Q" if w == 8 else ">I"
                out.extend(struct.unpack(fmt, data[i + 16 + k * w:i + 16 + (k + 1) * w])[0]
                           for k in range(n))
            i += size
    walk(0, len(data))
    return out


def audio_lines_up(data, audio=AUDIO):
    step = len(audio) // 4
    return [data[o:o + step] for o in chunk_offsets(data)] == \
        [audio[i * step:(i + 1) * step] for i in range(4)]


def materialise(path, segs):
    with open(path, "rb") as f:
        return b"".join(sync._blocks(f, segs))


def tags_of(path):
    with open(path, "rb") as f:
        return mp4meta.read_tags(f)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, name, data):
        p = self.tmp / name
        p.write_bytes(data)
        return p


TAGS = {mp4meta.KEYS["title"]: "New Title", mp4meta.KEYS["grouping"]: "Redwall #2",
        mp4meta.KEYS["track"]: (1, 0)}


class TestMp4Meta(Files):
    def test_rebuild_keeps_the_audio_where_the_tables_say(self):
        for faststart in (True, False):
            for wide in (False, True):
                src = self.put("a.m4b", mp4(faststart, wide=wide))
                with open(src, "rb") as f:
                    out = materialise(src, mp4meta.rebuild(f, TAGS))
                self.assertTrue(audio_lines_up(out), (faststart, wide))
                p = self.put("b.m4b", out)
                t = tags_of(p)
                self.assertEqual(t[mp4meta.KEYS["title"]], "New Title")
                self.assertEqual(t[mp4meta.KEYS["grouping"]], "Redwall #2")
                self.assertEqual(t[mp4meta.KEYS["track"]], (1, 0))
                self.assertEqual(t[b"\xa9too"], "Lavf", "a tag it was not given went missing")

    def test_rebuild_is_deterministic_and_leaves_room(self):
        src = self.put("a.m4b", mp4())
        with open(src, "rb") as f:
            a = materialise(src, mp4meta.rebuild(f, TAGS))
            b = materialise(src, mp4meta.rebuild(f, TAGS))
        self.assertEqual(a, b)
        at = a.index(b"moov") - 4
        self.assertEqual(struct.unpack(">I", a[at:at + 4])[0] % mp4meta.ROUND, 0)

    def test_a_small_change_is_a_small_write_in_place(self):
        src = self.put("a.m4b", mp4())
        with open(src, "rb") as f:
            p = self.put("b.m4b", materialise(src, mp4meta.rebuild(f, TAGS)))
        before = p.read_bytes()
        with open(p, "rb") as f:
            writes, cut = mp4meta.retag(f, {**TAGS, mp4meta.KEYS["grouping"]: "Redwall #3"})
        self.assertIsNone(cut)
        self.assertEqual(len(writes), 1)
        written = sync._apply_writes(p, writes, cut)
        self.assertLessEqual(written, 2 * sync.SECTOR)
        after = p.read_bytes()
        self.assertEqual(len(after), len(before))
        self.assertTrue(audio_lines_up(after))
        self.assertEqual(tags_of(p)[mp4meta.KEYS["grouping"]], "Redwall #3")

    def test_too_big_for_the_room_appends_a_new_moov(self):
        p = self.put("a.m4b", mp4(faststart=True))
        big = {**TAGS, mp4meta.KEYS["comment"]: "x" * 9000}
        with open(p, "rb") as f:
            writes, cut = mp4meta.retag(f, big)
        self.assertEqual(writes[-1][1], b"free", "the old moov is retired last")
        sync._apply_writes(p, writes, cut)
        data = p.read_bytes()
        self.assertTrue(audio_lines_up(data))
        self.assertEqual(len(tags_of(p)[mp4meta.KEYS["comment"]]), 9000)

    def test_cut_off_after_the_append_still_reads_the_old_tags(self):
        p = self.put("a.m4b", mp4(faststart=True))
        with open(p, "rb") as f:
            writes, cut = mp4meta.retag(f, {**TAGS, mp4meta.KEYS["comment"]: "x" * 9000})
        sync._apply_writes(p, writes[:1], None)          # unplugged before the last write
        self.assertEqual(tags_of(p)[mp4meta.KEYS["title"]], "Old Title")
        self.assertTrue(audio_lines_up(p.read_bytes()))

    def test_moov_at_the_end_is_rewritten_where_it_is(self):
        p = self.put("a.m4b", mp4(faststart=False))
        with open(p, "rb") as f:
            writes, cut = mp4meta.retag(f, {**TAGS, mp4meta.KEYS["comment"]: "x" * 9000})
        sync._apply_writes(p, writes, cut)
        self.assertTrue(audio_lines_up(p.read_bytes()))
        self.assertEqual(tags_of(p)[mp4meta.KEYS["title"]], "New Title")

    def test_not_an_mp4_is_refused(self):
        p = self.put("a.mp3", b"ID3\x03\0\0\0\0\0\0" + b"\xff\xfb" * 100)
        with open(p, "rb") as f, self.assertRaises(mp4meta.NotMp4):
            mp4meta.rebuild(f, TAGS)


FULL = {
    "id": "li_1",
    "media": {
        "metadata": {
            "title": "Mossflower", "subtitle": "Redwall, Book 2",
            "authors": [{"id": "a1", "name": "Brian Jacques"}],
            "narrators": ["Brian Jacques", "Full Cast"],
            "series": [{"id": "s1", "name": "Redwall", "sequence": "2"},
                       {"id": "s2", "name": "Tales", "sequence": None}],
            "genres": ["Fantasy", "Children"], "publishedYear": "1988",
            "publisher": "Listening Library", "description": "Mice.",
        },
        "audioFiles": [{"index": 1, "ino": "111", "metadata": {
            "filename": "Mossflower.m4b", "relPath": "Mossflower.m4b",
            "size": 0, "mtimeMs": 1000}}],
    },
}


class TestEmbed(unittest.TestCase):
    def test_the_same_fields_audiobookshelf_embeds(self):
        self.assertEqual(embed.ffmetadata(FULL), {
            "title": "Mossflower",
            "artist": "Brian Jacques",
            "album_artist": "Brian Jacques",
            "album": "Mossflower: Redwall, Book 2",
            "genre": "Fantasy; Children",
            "date": "1988",
            "comment": "Mice.",
            "description": "Mice.",
            "composer": "Brian Jacques, Full Cast",
            "copyright": "Listening Library",
            "grouping": "Redwall #2; Tales",
        })

    def test_empty_fields_are_left_out(self):
        bare = {"media": {"metadata": {"title": "Holes", "authors": []}}}
        self.assertEqual(embed.ffmetadata(bare), {"title": "Holes", "album": "Holes"})

    def test_excluded_files_are_not_copied(self):
        full = {"media": {"audioFiles": [
            {"ino": "2", "metadata": {"filename": "b.m4b", "relPath": "b.m4b"}},
            {"ino": "1", "metadata": {"filename": "a.m4b", "relPath": "a.m4b"}},
            {"ino": "3", "exclude": True, "metadata": {"filename": "c.m4b", "relPath": "c.m4b"}}]}}
        self.assertEqual([f["ino"] for f in embed.audio_files(full)], ["1", "2"])


class Resp:
    def __init__(self, body):
        self.body = body

    def read(self, n=-1):
        out, self.body = (self.body, b"") if n is None or n < 0 else (self.body[:n], self.body[n:])
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Server:
    """Answers item() and open_file() like Audiobookshelf, and counts."""

    def __init__(self, files, meta=None):
        self.files = dict(files)            # ino -> bytes
        self.mtimes = {ino: 1000 for ino in files}
        self.meta = dict(FULL["media"]["metadata"], **(meta or {}))
        self.fetched = []

    def item(self, item_id):
        afs = [{"index": n, "ino": ino, "metadata": {
                   "filename": f"part {n}.m4b", "relPath": f"part {n}.m4b",
                   "size": len(data), "mtimeMs": self.mtimes[ino]}}
               for n, (ino, data) in enumerate(sorted(self.files.items()), 1)]
        return {"id": item_id, "media": {"metadata": self.meta, "audioFiles": afs}}

    def open_file(self, item_id, ino, timeout=None):
        self.fetched.append(ino)
        return Resp(self.files[ino])

    def change(self, ino, data):
        self.files[ino] = data
        self.mtimes[ino] += 1


ITEM = {"id": "li_1", "title": "Mossflower", "author": "Brian Jacques"}


class TestPullByBytes(Files):
    def setUp(self):
        super().setUp()
        self.dev = self.tmp / "dev"
        (self.dev / "AUDIOBOOKS").mkdir(parents=True)
        self.opts = {"devicePath": str(self.dev), "subdir": "AUDIOBOOKS", "renameM4b": False,
                     "folderTemplate": "{author} - {title}", "writeTags": True}

    def pull(self, server, **kw):
        rep = sync.pull(server, [ITEM], dict(self.opts, **kw))
        self.assertEqual(rep["errors"], [])
        return rep

    def book(self):
        return self.dev / "AUDIOBOOKS" / "Brian Jacques - Mossflower.m4b"

    def test_a_copy_carries_audiobookshelfs_tags(self):
        s = Server({"111": mp4()})
        rep = self.pull(s)
        self.assertEqual(rep["copied"], 1)
        t = tags_of(self.book())
        self.assertEqual(t[mp4meta.KEYS["grouping"]], "Redwall #2; Tales")
        self.assertEqual(t[mp4meta.KEYS["album"]], "Mossflower: Redwall, Book 2")
        self.assertEqual(t[mp4meta.KEYS["composer"]], "Brian Jacques, Full Cast")
        self.assertTrue(audio_lines_up(self.book().read_bytes()))
        rec = index_mod.load(str(self.dev))["entries"]["Brian Jacques - Mossflower.m4b"]
        self.assertEqual(rec["files"][0]["ino"], "111")
        self.assertTrue(rec["files"][0]["tags"])

    def test_without_tags_the_file_is_the_servers_byte_for_byte(self):
        s = Server({"111": mp4()})
        self.pull(s, writeTags=False)
        self.assertEqual(self.book().read_bytes(), mp4())

    def test_nothing_changed_nothing_moves(self):
        s = Server({"111": mp4()})
        self.pull(s)
        rep = self.pull(s)
        self.assertEqual(s.fetched, ["111"], "fetched the book again")
        self.assertEqual((rep["skipped"], rep["updated"], rep["written"]), (1, 0, 0))

    def test_new_details_on_the_server_write_only_the_tag_bytes(self):
        s = Server({"111": mp4()})
        self.pull(s)
        before = self.book().read_bytes()
        s.meta = dict(s.meta, series=[{"name": "Redwall", "sequence": "2"}])
        rep = self.pull(s)
        self.assertEqual(s.fetched, ["111"], "downloaded the audio for a tag change")
        self.assertEqual(rep["updated"], 1)
        self.assertLessEqual(rep["written"], 2 * sync.SECTOR)
        after = self.book().read_bytes()
        self.assertEqual(len(after), len(before))
        self.assertEqual(tags_of(self.book())[mp4meta.KEYS["grouping"]], "Redwall #2")
        self.assertTrue(audio_lines_up(after))

    def test_turning_tags_on_later_tags_in_place(self):
        s = Server({"111": mp4()})
        self.pull(s, writeTags=False)
        rep = self.pull(s)
        self.assertEqual(s.fetched, ["111"])
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(tags_of(self.book())[mp4meta.KEYS["grouping"]], "Redwall #2; Tales")
        self.assertTrue(audio_lines_up(self.book().read_bytes()))

    def test_a_changed_file_on_the_server_writes_only_the_blocks_that_differ(self):
        audio = bytearray(AUDIO * 128)                 # 2 MB, so blocks matter
        s = Server({"111": mp4(audio=bytes(audio))})
        self.pull(s)
        audio[1_000_000] ^= 0xFF
        s.change("111", mp4(audio=bytes(audio)))
        rep = self.pull(s)
        self.assertEqual(s.fetched, ["111", "111"])
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(rep["written"], sync.SECTOR)
        self.assertTrue(audio_lines_up(self.book().read_bytes(), bytes(audio)))

    def test_a_forced_copy_of_the_same_book_writes_nothing(self):
        s = Server({"111": mp4()})
        self.pull(s)
        rep = self.pull(s, force=True)
        self.assertEqual(s.fetched, ["111", "111"])
        self.assertEqual(rep["written"], 0)

    def test_a_forced_copy_repairs_a_damaged_file_in_place(self):
        s = Server({"111": mp4()})
        self.pull(s)
        good = self.book().read_bytes()
        bad = bytearray(good)
        bad[-100] ^= 0xFF
        self.book().write_bytes(bytes(bad))
        rep = self.pull(s, force=True)
        self.assertEqual(self.book().read_bytes(), good)
        self.assertEqual(rep["written"], sync.SECTOR)

    def test_a_book_in_parts_moves_only_the_part_that_changed(self):
        s = Server({"1": mp4(title="one"), "2": mp4(title="two"), "3": mp4(title="three")})
        rep = self.pull(s)
        self.assertEqual(rep["copied"], 3)
        folder = self.dev / "AUDIOBOOKS" / "Brian Jacques - Mossflower"
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, ["001 - part 1.m4b", "002 - part 2.m4b", "003 - part 3.m4b"])
        self.assertEqual(tags_of(folder / names[1])[mp4meta.KEYS["track"]], (2, 0))
        s.change("2", mp4(title="two", audio=AUDIO[::-1]))
        rep = self.pull(s)
        self.assertEqual(s.fetched, ["1", "2", "3", "2"])
        self.assertEqual(rep["updated"], 1)
        self.assertTrue(audio_lines_up((folder / names[1]).read_bytes(), AUDIO[::-1]))

    def test_a_book_copied_before_files_were_tracked_is_not_fetched_again(self):
        s = Server({"111": mp4()})
        self.book().write_bytes(mp4())
        index_mod.record(str(self.dev), self.book().name, ITEM,
                         [{"name": self.book().name, "size": len(mp4())}], "file", ".m4b")
        rep = self.pull(s)
        self.assertEqual(s.fetched, [])
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(tags_of(self.book())[mp4meta.KEYS["grouping"]], "Redwall #2; Tales")
        rec = index_mod.load(str(self.dev))["entries"][self.book().name]
        self.assertEqual(rec["files"][0]["ino"], "111")

    def test_a_server_without_per_file_downloads_still_copies(self):
        class Old:
            downloads = []

            def open_download(self, item_id, timeout=None):
                self.downloads.append(item_id)
                r = Resp(mp4())
                r.headers = {"Content-Type": "audio/mp4",
                             "Content-Disposition": 'attachment; filename="Mossflower.m4b"'}
                return r
        rep = sync.pull(Old(), [ITEM], self.opts)
        self.assertEqual(rep["copied"], 1)


class TestAskingAboutTags(unittest.TestCase):
    """Writing tags is asked about unless the flag or the setting says."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg_file = self.tmp / "config.json"
        self.cfg_file.write_text(json.dumps({"absUrl": "http://abs", "writeTags": "ask"}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def choose(self, answers, tags=None, setting="ask", tty=True, ask=True):
        from absh import cli
        args = argparse.Namespace(tags=tags, config=str(self.cfg_file))
        replies = iter(answers)
        with mock.patch("sys.stdin") as stdin, \
                mock.patch("builtins.input", lambda prompt="": next(replies)), \
                mock.patch("builtins.print"):
            stdin.isatty.return_value = tty
            return cli.tags_choice(args, {"writeTags": setting}, ask=ask)

    def saved(self):
        return json.loads(self.cfg_file.read_text()).get("writeTags")

    def test_the_flag_wins_without_asking(self):
        self.assertTrue(self.choose([], tags=True, setting="never"))
        self.assertFalse(self.choose([], tags=False, setting="always"))

    def test_the_setting_answers_when_it_is_not_ask(self):
        self.assertTrue(self.choose([], setting="always"))
        self.assertFalse(self.choose([], setting="never"))

    def test_nobody_at_the_keyboard_means_no(self):
        self.assertFalse(self.choose([], tty=False))
        self.assertFalse(self.choose([], ask=False))

    def test_yes_and_no_are_for_this_time_only(self):
        self.assertTrue(self.choose(["y"]))
        self.assertFalse(self.choose(["n"]))
        self.assertFalse(self.choose([""]))
        self.assertEqual(self.saved(), "ask")

    def test_always_and_never_are_remembered_and_nothing_else_changes(self):
        self.assertTrue(self.choose(["what", "a"]))
        self.assertEqual(self.saved(), "always")
        self.assertFalse(self.choose(["never"]))
        self.assertEqual(self.saved(), "never")
        self.assertEqual(json.loads(self.cfg_file.read_text())["absUrl"], "http://abs")


class TestChangedOnTheServer(unittest.TestCase):
    def test_only_a_newer_server_copy_of_a_book_this_tool_copied_is_stale(self):
        e = {"source": "index", "itemUpdatedAt": 100}
        self.assertTrue(device.stale(e, {"updatedAt": 200}))
        self.assertFalse(device.stale(e, {"updatedAt": 100}))
        self.assertFalse(device.stale(dict(e, itemUpdatedAt=0), {"updatedAt": 200}))
        self.assertFalse(device.stale(dict(e, source="tags"), {"updatedAt": 200}))

    def test_the_index_remembers_when_the_server_last_changed_the_book(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            book = tmp / "AUDIOBOOKS" / "Brian Jacques - Mossflower.m4b"
            book.parent.mkdir()
            book.write_bytes(mp4())
            index.record(str(tmp), book.name, dict(ITEM, updatedAt=100),
                         [{"name": book.name, "size": book.stat().st_size, "tags": "ab"}], "file")
            st = device.status(str(tmp), "AUDIOBOOKS", [dict(ITEM, updatedAt=200)],
                               "{author} - {title}", read_tags=False)
            self.assertEqual(len(st["both"]), 1)
            self.assertTrue(st["both"][0]["stale"])
            self.assertTrue(st["both"][0]["tagged"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
