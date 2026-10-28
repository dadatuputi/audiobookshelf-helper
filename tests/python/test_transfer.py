"""Copying only what changed, and having Audiobookshelf write the tags.

Tags are written by Audiobookshelf's own Embed Metadata, on the server. The
fake server here does what that does to a file - rewrites its tag area -
and, like the real one, may not update the size and time it reports for the
file until it next scans.
"""
import argparse
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from absh import device, embed, index, sync   # noqa: E402
from absh import index as index_mod            # noqa: E402
from absh.abs_api import AbsError              # noqa: E402

AUDIO = bytes(range(256)) * 64            # 16 KB of "audio"
TAG_AREA = 4096                           # where the fake embed writes, one sector


def book_file(audio=AUDIO, tags=""):
    """A file: a fixed-size tag area, then the audio."""
    return tags.encode().ljust(TAG_AREA, b"\0") + audio


def tags_in(data):
    return data[:TAG_AREA].rstrip(b"\0").decode()


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
    def test_the_fingerprint_moves_with_the_details_and_only_them(self):
        a = embed.digest(FULL)
        self.assertEqual(a, embed.digest(json.loads(json.dumps(FULL))))
        moved = json.loads(json.dumps(FULL))
        moved["media"]["metadata"]["title"] = "Mattimeo"
        self.assertNotEqual(a, embed.digest(moved))

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
    """Answers like Audiobookshelf, counts downloads and embeds, and embeds
    the way it does: in place, as a task that takes a moment."""

    def __init__(self, files, meta=None, admin=True):
        self.files = dict(files)            # ino -> bytes
        self.mtimes = {ino: 1000 for ino in files}
        self.sizes = {ino: len(d) for ino, d in files.items()}
        self.meta = dict(FULL["media"]["metadata"], **(meta or {}))
        self.admin = admin
        self.fetched = []
        self.embeds = []
        self.busy = 0                       # polls left before the task is done

    def item(self, item_id):
        afs = [{"index": n, "ino": ino, "metadata": {
                   "filename": f"part {n}.m4b", "relPath": f"part {n}.m4b",
                   "size": self.sizes[ino], "mtimeMs": self.mtimes[ino]}}
               for n, ino in enumerate(sorted(self.files), 1)]
        return {"id": item_id, "media": {"metadata": self.meta, "audioFiles": afs}}

    def open_file(self, item_id, ino, timeout=None):
        self.fetched.append(ino)
        return Resp(self.files[ino])

    def embed_metadata(self, item_id):
        if not self.admin:
            raise AbsError("Audiobookshelf /api/tools/... responded 403", status=403)
        self.embeds.append(item_id)
        self.busy = 2
        title = self.meta["title"]
        for ino, data in self.files.items():
            # Rewritten in place. The size and time it reports stay as they
            # were until a scan - the case the copy has to cope with.
            self.files[ino] = book_file(data[TAG_AREA:], f"{title}|{sorted(self.files).index(ino) + 1}")

    def embedding(self, item_id):
        self.busy = max(0, self.busy - 1)
        return self.busy > 0

    def change(self, ino, data):
        self.files[ino] = data
        self.mtimes[ino] += 1
        self.sizes[ino] = len(data)


ITEM = {"id": "li_1", "title": "Mossflower", "author": "Brian Jacques"}


class TestPullByBytes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dev = self.tmp / "dev"
        (self.dev / "AUDIOBOOKS").mkdir(parents=True)
        self.opts = {"devicePath": str(self.dev), "subdir": "AUDIOBOOKS", "renameM4b": False,
                     "folderTemplate": "{author} - {title}", "writeTags": True}
        patcher = mock.patch.object(sync.time, "sleep")
        self.sleeps = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pull(self, server, **kw):
        rep = sync.pull(server, [ITEM], dict(self.opts, **kw))
        self.assertEqual(rep["errors"], [])
        return rep

    def book(self):
        return self.dev / "AUDIOBOOKS" / "Brian Jacques - Mossflower.m4b"

    def rec(self):
        return index_mod.load(str(self.dev))["entries"][self.book().name]

    def test_tags_are_embedded_by_audiobookshelf_then_copied(self):
        s = Server({"111": book_file()})
        rep = self.pull(s)
        self.assertEqual(s.embeds, ["li_1"])
        self.assertTrue(self.sleeps.called, "did not wait for the embed to finish")
        self.assertEqual(rep["copied"], 1)
        self.assertEqual(tags_in(self.book().read_bytes()), "Mossflower|1")
        self.assertEqual(self.rec()["files"][0]["ino"], "111")
        self.assertEqual(self.rec()["files"][0]["tags"], embed.digest(s.item("li_1")))

    def test_without_tags_nothing_is_embedded_and_the_file_is_the_servers(self):
        s = Server({"111": book_file()})
        self.pull(s, writeTags=False)
        self.assertEqual(s.embeds, [])
        self.assertEqual(self.book().read_bytes(), book_file())
        self.assertIsNone(self.rec()["files"][0]["tags"])

    def test_nothing_changed_nothing_is_embedded_or_moved(self):
        s = Server({"111": book_file()})
        self.pull(s)
        rep = self.pull(s)
        self.assertEqual(s.embeds, ["li_1"], "embedded again for nothing")
        self.assertEqual(s.fetched, ["111"], "fetched the book again")
        self.assertEqual((rep["skipped"], rep["updated"], rep["written"]), (1, 0, 0))

    def test_new_details_embed_again_and_write_only_the_changed_sector(self):
        audio = AUDIO * 128                            # 2 MB, so blocks matter
        s = Server({"111": book_file(audio)})
        self.pull(s)
        s.meta = dict(s.meta, title="Mossflower (Redwall 2)")
        rep = self.pull(s)
        self.assertEqual(s.embeds, ["li_1", "li_1"])
        # The server did not say the file changed; it was fetched anyway.
        self.assertEqual(s.fetched, ["111", "111"])
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(rep["written"], sync.SECTOR)
        self.assertEqual(tags_in(self.book().read_bytes()), "Mossflower (Redwall 2)|1")

    def test_turning_tags_on_later_embeds_and_updates_in_place(self):
        s = Server({"111": book_file()})
        self.pull(s, writeTags=False)
        rep = self.pull(s)
        self.assertEqual(s.embeds, ["li_1"])
        self.assertEqual(rep["written"], sync.SECTOR)
        self.assertEqual(tags_in(self.book().read_bytes()), "Mossflower|1")

    def test_a_key_that_is_not_an_admins_copies_without_tags_and_says_so(self):
        s = Server({"111": book_file()}, admin=False)
        rep = self.pull(s)
        self.assertEqual(rep["copied"], 1)
        self.assertEqual(self.book().read_bytes(), book_file())
        self.assertEqual(len(rep["notes"]), 1)
        self.assertIn("admin", rep["notes"][0])
        self.assertIsNone(self.rec()["files"][0]["tags"])

    def test_an_embed_that_never_finishes_gives_up_and_copies_as_is(self):
        s = Server({"111": book_file()})
        s.embedding = lambda item_id: True
        with mock.patch.object(sync, "EMBED_TIMEOUT", -1):
            rep = self.pull(s)
        self.assertEqual(rep["copied"], 1)
        self.assertIn("too long", rep["notes"][0])

    def test_a_changed_file_on_the_server_writes_only_the_blocks_that_differ(self):
        audio = bytearray(AUDIO * 128)
        s = Server({"111": book_file(bytes(audio))})
        self.pull(s, writeTags=False)
        audio[1_000_000] ^= 0xFF
        s.change("111", book_file(bytes(audio)))
        rep = self.pull(s, writeTags=False)
        self.assertEqual(s.fetched, ["111", "111"])
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(rep["written"], sync.SECTOR)
        self.assertEqual(self.book().read_bytes(), book_file(bytes(audio)))

    def test_a_forced_copy_of_the_same_book_writes_nothing(self):
        s = Server({"111": book_file()})
        self.pull(s)
        rep = self.pull(s, force=True)
        self.assertEqual(s.fetched, ["111", "111"])
        self.assertEqual(s.embeds, ["li_1"])
        self.assertEqual(rep["written"], 0)
        self.assertEqual(rep["wrote"], {"li_1": 0})

    def test_a_forced_copy_repairs_a_damaged_file_in_place(self):
        s = Server({"111": book_file()})
        self.pull(s)
        good = self.book().read_bytes()
        bad = bytearray(good)
        bad[-100] ^= 0xFF
        self.book().write_bytes(bytes(bad))
        rep = self.pull(s, force=True)
        self.assertEqual(self.book().read_bytes(), good)
        self.assertEqual(rep["written"], sync.SECTOR)
        self.assertEqual(rep["wrote"], {"li_1": sync.SECTOR})

    def test_a_book_in_parts_moves_only_the_part_that_changed(self):
        s = Server({"1": book_file(), "2": book_file(AUDIO[::-1]), "3": book_file()})
        rep = self.pull(s, writeTags=False)
        self.assertEqual(rep["copied"], 3)
        folder = self.dev / "AUDIOBOOKS" / "Brian Jacques - Mossflower"
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, ["001 - part 1.m4b", "002 - part 2.m4b", "003 - part 3.m4b"])
        s.change("2", book_file(AUDIO))
        rep = self.pull(s, writeTags=False)
        self.assertEqual(s.fetched, ["1", "2", "3", "2"])
        self.assertEqual(rep["updated"], 1)
        self.assertEqual((folder / names[1]).read_bytes(), book_file(AUDIO))

    def test_each_part_carries_its_own_track_number(self):
        s = Server({"1": book_file(), "2": book_file()})
        self.pull(s)
        folder = self.dev / "AUDIOBOOKS" / "Brian Jacques - Mossflower"
        self.assertEqual([tags_in(p.read_bytes()) for p in sorted(folder.iterdir())],
                         ["Mossflower|1", "Mossflower|2"])

    def test_a_book_copied_before_files_were_tracked_is_not_fetched_again(self):
        s = Server({"111": book_file()})
        self.book().write_bytes(book_file())
        index_mod.record(str(self.dev), self.book().name, ITEM,
                         [{"name": self.book().name, "size": len(book_file())}], "file", ".m4b")
        rep = self.pull(s, writeTags=False)
        self.assertEqual(s.fetched, [])
        self.assertEqual(rep["skipped"], 1)
        self.assertEqual(self.rec()["files"][0]["ino"], "111")

    def test_a_file_is_fetched_by_the_inode_the_server_downloads_it_by(self):
        """Audiobookshelf finds a file to download among the item's files,
        and the inode on its audio entry can be out of date: asking for
        that one is a 404."""
        class Stale(Server):
            def item(self, item_id):
                full = super().item(item_id)
                full["libraryFiles"] = []
                for af in full["media"]["audioFiles"]:
                    af["metadata"]["path"] = "/books/" + af["metadata"]["relPath"]
                    full["libraryFiles"].append({"ino": af["ino"], "metadata": dict(af["metadata"])})
                    af["ino"] = "999" + af["ino"]          # what a replaced file left behind
                return full

            def open_file(self, item_id, ino, timeout=None):
                if ino not in self.files:
                    raise AbsError("Audiobookshelf ... responded 404 - Not Found", status=404)
                return super().open_file(item_id, ino, timeout)

        s = Stale({"111": book_file()})
        self.pull(s, writeTags=False)
        self.assertEqual(s.fetched, ["111"])
        self.assertEqual(self.book().read_bytes(), book_file())

    def legacy_folder(self, inner):
        """A one-file book as older versions copied it: Audiobookshelf's
        download is a zip when the book's folder holds a cover too, so the
        book went into a folder of its own."""
        folder = self.dev / "AUDIOBOOKS" / "Brian Jacques - Mossflower"
        folder.mkdir()
        bad = bytearray(book_file())
        bad[-100] ^= 0xFF
        (folder / inner).write_bytes(bytes(bad))
        index_mod.record(str(self.dev), folder.name, ITEM,
                         [{"name": inner, "size": len(bad)}], "dir", ".m4b")
        return folder

    def test_copy_again_puts_right_a_one_file_book_kept_in_a_folder(self):
        s = Server({"111": book_file()})
        folder = self.legacy_folder("001 - Mossflower.m4b")
        rep = self.pull(s, force=True, writeTags=False)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["001 - Mossflower.m4b"])
        self.assertEqual((folder / "001 - Mossflower.m4b").read_bytes(), book_file())
        self.assertEqual(rep["written"], sync.SECTOR)
        self.assertFalse(self.book().exists())
        rec = index_mod.load(str(self.dev))["entries"][folder.name]
        self.assertEqual((rec["kind"], rec["files"][0]["ino"]), ("dir", "111"))

    def test_copy_again_keeps_a_name_changed_on_the_player(self):
        s = Server({"111": book_file()})
        folder = self.legacy_folder("Mossflower, cut short.m4b")
        self.pull(s, force=True, writeTags=False)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Mossflower, cut short.m4b"])
        self.assertEqual((folder / "Mossflower, cut short.m4b").read_bytes(), book_file())

    def test_a_book_in_parts_renamed_on_the_player_is_written_over(self):
        s = Server({"1": book_file(), "2": book_file(AUDIO[::-1])})
        self.pull(s, writeTags=False)
        folder = self.dev / "AUDIOBOOKS" / "Brian Jacques - Mossflower"
        data = index_mod.load(str(self.dev))
        for n, r in enumerate(data["entries"][folder.name]["files"], 1):
            (folder / r["name"]).rename(folder / f"Part {n}.m4b")
            r["name"] = f"Part {n}.m4b"
        index_mod.save(str(self.dev), data)
        (folder / "Part 2.m4b").write_bytes(book_file())
        self.pull(s, force=True, writeTags=False)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["Part 1.m4b", "Part 2.m4b"])
        self.assertEqual((folder / "Part 2.m4b").read_bytes(), book_file(AUDIO[::-1]))

    def test_a_player_pulled_out_mid_download_is_not_made_again(self):
        s = Server({"111": book_file(), "222": book_file()})
        dev = self.dev
        real_open = s.open_file

        def gone(item_id, ino, timeout=None):
            shutil.rmtree(dev)                  # out it comes, mid-download
            return real_open(item_id, ino)
        s.open_file = gone
        rep = sync.pull(s, [ITEM], self.opts)
        self.assertEqual(rep["interrupted"], [ITEM["id"]])
        self.assertEqual(rep["errors"], [])
        self.assertFalse(dev.exists(), "the player's folders were made again on this disk")

    def test_a_server_without_per_file_downloads_still_copies(self):
        class Old:
            downloads = []

            def open_download(self, item_id, timeout=None):
                self.downloads.append(item_id)
                r = Resp(book_file())
                r.headers = {"Content-Type": "audio/mp4",
                             "Content-Disposition": 'attachment; filename="Mossflower.m4b"'}
                return r
        rep = sync.pull(Old(), [ITEM], self.opts)
        self.assertEqual(rep["copied"], 1)


class TestEmbedApi(unittest.TestCase):
    """The two calls, asked the way Audiobookshelf answers them."""

    def client(self, answers):
        from absh.abs_api import Client
        seen = []

        def opener(req, timeout=None):
            seen.append((req.get_method(), req.full_url))
            return Resp(json.dumps(answers.pop(0)).encode())
        c = Client("http://abs", "k", opener=opener)
        return c, seen

    def test_embed_is_a_post_to_the_tools_route(self):
        c, seen = self.client([{}])
        c.embed_metadata("li 1")
        self.assertEqual(seen, [("POST", "http://abs/api/tools/item/li%201/embed-metadata")])

    def test_embedding_while_queued_or_running_and_not_after(self):
        task = {"action": "embed-metadata", "isFinished": False, "data": {"libraryItemId": "li_1"}}
        c, seen = self.client([
            {"tasks": [task]},
            {"tasks": [], "queuedTaskData": {"embedMetadata": [{"libraryItemId": "li_1"}]}},
            {"tasks": [dict(task, data={"libraryItemId": "other"})],
             "queuedTaskData": {"embedMetadata": []}},
        ])
        self.assertTrue(c.embedding("li_1"))
        self.assertTrue(c.embedding("li_1"))
        self.assertFalse(c.embedding("li_1"))
        self.assertEqual(seen[0], ("GET", "http://abs/api/tasks?include=queue"))


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
            book.write_bytes(book_file())
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
