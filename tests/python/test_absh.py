"""The absh engine: naming, tags, the Audiobookshelf client, matching and sync.

These replace the JavaScript tests for the same logic, which moved into Python
when the script became the product.
"""
import io
import json
import struct
import sys
import shutil
import tempfile
import unittest
import zipfile
from email import message_from_bytes
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from absh import config as config_mod          # noqa: E402
from absh import device as device_mod          # noqa: E402
from absh import index as index_mod            # noqa: E402
from absh import devices as devices_mod        # noqa: E402
from absh import naming, sync, tags            # noqa: E402
from absh.abs_api import (AbsError, Client, _encode_multipart,   # noqa: E402
                          normalize_item)
from absh.tui import build_rows, filter_rows, summarise, _key    # noqa: E402


# ------------------------------------------------------------- fixtures
def mp4(title, author, album=None):
    """A real MP4 atom tree with a metadata block - not a stub."""
    def atom(name, payload):
        return struct.pack(">I", len(payload) + 8) + name + payload

    def data(text):
        return atom(b"data", struct.pack(">I", 1) + struct.pack(">I", 0) + text.encode())

    ilst = atom(b"\xa9nam", data(title)) + atom(b"aART", data(author))
    if album:
        ilst += atom(b"\xa9alb", data(album))
    meta = atom(b"meta", struct.pack(">I", 0) + atom(b"ilst", ilst))
    return atom(b"ftyp", b"M4A ") + atom(b"moov", atom(b"udta", meta))


def id3(frames, major=3):
    body = b""
    for fid, text in frames:
        payload = b"\x00" + text.encode("latin-1")
        size = (struct.pack(">I", len(payload)) if major < 4 else
                struct.pack(">BBBB", *[(len(payload) >> s) & 0x7F for s in (21, 14, 7, 0)]))
        body += fid + size + b"\x00\x00" + payload
    header = b"ID3" + bytes([major, 0, 0]) + struct.pack(
        ">BBBB", *[(len(body) >> s) & 0x7F for s in (21, 14, 7, 0)])
    return header + body + b"\xff\xfb\x00" * 8


ITEMS = [
    {"id": "li_1", "title": "Redwall", "author": "Brian Jacques", "series": "",
     "relPath": "Brian Jacques/Redwall", "numTracks": 1, "size": 100},
    {"id": "li_2", "title": "Holes", "author": "Louis Sachar", "series": "",
     "relPath": "Louis Sachar/Holes", "numTracks": 1, "size": 200},
]


class FakeResponse:
    def __init__(self, body, headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status = status

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


# ------------------------------------------------------------- naming
class TestNaming(unittest.TestCase):
    def test_template_and_cleaning(self):
        self.assertEqual(naming.target_name({"author": "Brian Jacques", "title": "Redwall"}),
                         "Brian Jacques - Redwall")

    def test_missing_author_leaves_no_dangling_separator(self):
        self.assertEqual(naming.target_name({"author": "", "title": "Holes"}), "Holes")

    def test_reserved_characters_and_transliteration(self):
        self.assertEqual(naming.clean('a/b\\c:d*e?f"g<h>i|j'), "abcdefghij")
        self.assertEqual(naming.clean("Pema Chödrön"), "Pema Chodron")

    def test_never_empty(self):
        for junk in ("", "///", None):
            self.assertEqual(naming.clean(junk), "Untitled")

    def test_rename_round_trip(self):
        """The whole premise: .m4b out, .m4a on the device, .m4b back."""
        self.assertEqual(naming.out_ext("Book.m4b", True), ".m4a")
        self.assertEqual(naming.out_ext("Book.m4b", False), ".m4b")
        self.assertEqual(naming.source_ext("Book.m4a"), ".m4b")
        self.assertEqual(naming.out_ext("Book.mp3", True), ".mp3")

    def test_subdir_cannot_escape(self):
        self.assertEqual(naming.safe_subdir("../../etc"), Path("etc"))
        self.assertEqual(naming.safe_subdir("../.."), Path("AUDIOBOOKS"))
        self.assertFalse(naming.safe_subdir("/etc/passwd").is_absolute())

    def test_fuzzy_key_survives_how_people_actually_write_names(self):
        same = [(("The Hobbit", "J.R.R. Tolkien"), ("Hobbit, The", "JRR Tolkien")),
                (("A Wrinkle in Time", "L'Engle"), ("Wrinkle in Time, A", "LEngle")),
                (("Redwall", "Brian Jacques"), ("redwall", "brian  jacques"))]
        for a, b in same:
            self.assertEqual(naming.norm_key(*a), naming.norm_key(*b), f"{a} vs {b}")

    def test_fuzzy_key_still_separates_different_books(self):
        self.assertNotEqual(naming.norm_key("Holes", "Sachar"),
                            naming.norm_key("Redwall", "Jacques"))


# --------------------------------------------------------------- tags
class TestTags(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mp4_atoms(self):
        p = self.tmp / "a.m4b"
        p.write_bytes(mp4("Chapter One", "Brian Jacques", "Redwall"))
        t = tags.read(p)
        self.assertEqual(t["title"], "Chapter One")
        self.assertEqual(t["album"], "Redwall")

    def test_album_artist_beats_artist(self):
        """An audiobook's artist is as often the narrator as the author."""
        def atom(n, p):
            return struct.pack(">I", len(p) + 8) + n + p

        def data(t):
            return atom(b"data", struct.pack(">I", 1) + struct.pack(">I", 0) + t.encode())
        ilst = (atom(b"\xa9ART", data("Narrator Person"))
                + atom(b"aART", data("Real Author")))
        blob = atom(b"ftyp", b"M4A ") + atom(b"moov", atom(b"udta", atom(
            b"meta", struct.pack(">I", 0) + atom(b"ilst", ilst))))
        p = self.tmp / "b.m4b"
        p.write_bytes(blob)
        self.assertEqual(tags.read(p)["author"], "Real Author")

    def test_id3_both_versions(self):
        """v2.3 and v2.4 encode frame sizes differently."""
        for major in (3, 4):
            p = self.tmp / f"c{major}.mp3"
            p.write_bytes(id3([(b"TIT2", "Holes"), (b"TPE2", "Louis Sachar"),
                               (b"TALB", "Holes"), (b"TRCK", "2/12")], major))
            t = tags.read(p)
            self.assertEqual(t["title"], "Holes", f"v2.{major}")
            self.assertEqual(t["author"], "Louis Sachar", f"v2.{major}")
            self.assertEqual(t["track"], 2, f"v2.{major}")

    def test_garbage_is_blank_not_an_exception(self):
        (self.tmp / "junk.mp3").write_bytes(b"not an mp3")
        self.assertEqual(tags.read(self.tmp / "junk.mp3")["title"], "")
        self.assertEqual(tags.read(self.tmp / "missing.m4b")["title"], "")

    def test_multi_file_book_uses_the_album_as_the_title(self):
        a = self.tmp / "1.m4a"
        a.write_bytes(mp4("Chapter One", "Author", "The Whole Book"))
        self.assertEqual(tags.read_book([a, a])["title"], "The Whole Book")

    def test_reader_is_reported(self):
        self.assertIn(tags.available(), ("mutagen", "builtin"))


# ------------------------------------------------------------- abs_api
class TestClient(unittest.TestCase):
    def test_refuses_a_non_http_server(self):
        for bad in ("file:///etc", "ftp://x", ""):
            with self.assertRaises(AbsError):
                Client(bad, "k")

    def test_download_url_carries_the_token(self):
        c = Client("http://x:13378/", "tok")
        self.assertEqual(c.download_url("li_1"),
                         "http://x:13378/api/items/li_1/download?token=tok")

    def test_download_url_escapes(self):
        self.assertIn("token=a%20b", Client("http://x", "a b").download_url("i"))

    def test_multipart_is_valid_and_binary_survives(self):
        body, ctype = _encode_multipart(
            {"library": "lib1", "title": "T", "series": None},
            [("file0", "Book.m4b", b"\x00\x01\xff audio")])
        msg = message_from_bytes(b"Content-Type: " + ctype.encode()
                                 + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
        self.assertTrue(msg.is_multipart())
        parts = {p.get_param("name", header="content-disposition"): p
                 for p in msg.get_payload()}
        self.assertEqual(sorted(parts), ["file0", "library", "title"])
        self.assertNotIn("series", parts, "None fields must be omitted")
        self.assertEqual(parts["file0"].get_payload(decode=True), b"\x00\x01\xff audio")
        self.assertEqual(parts["file0"].get_param("filename",
                                                  header="content-disposition"), "Book.m4b")

    def test_http_errors_are_explained(self):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"nope"))
        c = Client("http://x", "k", opener=boom)
        with self.assertRaises(AbsError) as cm:
            c.libraries()
        self.assertIn("check the API key", str(cm.exception))


class TestContract(unittest.TestCase):
    """The Audiobookshelf response shapes this tool depends on.

    Pinned here so an upstream change fails a build instead of silently
    emptying the book list. The same fields are listed in
    tests/fixtures/abs/contract.json for the upstream watcher to quote.
    """

    def fixture(self, name):
        return json.loads((ROOT / "tests" / "fixtures" / "abs" / name).read_text())

    def test_libraries_filtered_to_books(self):
        data = self.fixture("libraries.json")
        c = Client("http://x", "k",
                   opener=lambda req, timeout=None: FakeResponse(json.dumps(data).encode()))
        self.assertEqual(c.libraries(),
                         [{"id": "lib_c1u6t4p45c35rf0nzd", "name": "Audiobooks",
                           "mediaType": "book"}])

    def test_minified_items(self):
        results = self.fixture("items-minified.json")["results"]
        b = normalize_item(results[0])
        self.assertEqual(b["id"], "li_8gch9ve09orgn4fdz8")
        self.assertEqual(b["title"], "Redwall")
        self.assertEqual(b["author"], "Brian Jacques")
        self.assertEqual(b["relPath"], "Brian Jacques/Redwall")
        self.assertEqual(b["numTracks"], 1)
        self.assertEqual(normalize_item(results[1])["numTracks"], 12)

    def test_full_form_authors_and_series_arrays(self):
        it = self.fixture("items-full.json")["results"][0]
        b = normalize_item(it)
        self.assertEqual(b["author"], "Brian Jacques, Someone Else")
        self.assertEqual(b["series"], "Redwall")
        self.assertEqual(b["numTracks"], 1, "counted from audioFiles")

    def test_survives_an_empty_item(self):
        b = normalize_item({"id": "x"})
        self.assertEqual(b["title"], "(untitled)")
        self.assertEqual(b["author"], "")

    def test_contract_file_still_lists_every_endpoint(self):
        paths = [e["path"] for e in self.fixture("contract.json")["endpoints"]]
        self.assertIn("/api/libraries", paths)
        self.assertIn("/api/libraries/{id}/items?limit=0", paths)


# ------------------------------------------------ device, index, matching
class FakeClient:
    """Serves books over the same interface the real client exposes.

    Everything moves through the server now, so the tests drive the download
    path rather than a shortcut that no longer exists.
    """

    def __init__(self, bodies=None):
        self.bodies = bodies or {}
        self.downloads = []
        self.uploads = []

    def open_download(self, item_id, timeout=None):
        self.downloads.append(item_id)
        body, name, ctype = self.bodies[item_id]
        return FakeResponse(body, {"Content-Type": ctype,
                                   "Content-Disposition": f'attachment; filename="{name}"'})

    def upload(self, library_id, folder_id, title, author, files, series=None, timeout=None):
        self.uploads.append({"library": library_id, "folder": folder_id, "title": title,
                             "author": author, "names": [n for n, _ in files]})
        return {"id": "li_new"}


class DeviceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dev = self.tmp / "dev"
        (self.dev / "AUDIOBOOKS").mkdir(parents=True)
        self.client = FakeClient({
            it["id"]: (mp4(it["title"], it["author"]), f"{it['title']}.m4b", "audio/mp4")
            for it in ITEMS
        })
        self.opts = {"devicePath": str(self.dev), "subdir": "AUDIOBOOKS",
                     "renameM4b": True, "folderTemplate": "{author} - {title}"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def place(self, name, title, author):
        p = self.dev / "AUDIOBOOKS" / name
        p.write_bytes(mp4(title, author))
        return p


class TestPullAndMatch(DeviceBase):
    def test_pull_renames_and_records(self):
        rep = sync.pull(self.client, [ITEMS[0]], self.opts)
        self.assertEqual(rep["copied"], 1)
        self.assertEqual(rep["errors"], [])
        self.assertTrue((self.dev / "AUDIOBOOKS" / "Brian Jacques - Redwall.m4a").exists())
        self.assertIn("Brian Jacques - Redwall.m4a",
                      index_mod.load(str(self.dev))["entries"])

    def test_second_pull_skips_without_downloading_again(self):
        """The expensive mistake: everything comes over the wire now, so a
        re-run must not fetch a whole book to discover it is already there."""
        sync.pull(self.client, [ITEMS[0]], self.opts)
        self.assertEqual(self.client.downloads, ["li_1"])
        rep = sync.pull(self.client, [ITEMS[0]], self.opts)
        self.assertEqual(rep["copied"], 0)
        self.assertEqual(rep["skipped"], 1)
        self.assertEqual(self.client.downloads, ["li_1"], "downloaded it a second time")

    def test_force_re_downloads(self):
        sync.pull(self.client, [ITEMS[0]], self.opts)
        sync.pull(self.client, [ITEMS[0]], dict(self.opts, force=True))
        self.assertEqual(self.client.downloads, ["li_1", "li_1"])

    def test_status_matches_by_id(self):
        sync.pull(self.client, [ITEMS[0]], self.opts)
        st = device_mod.status(str(self.dev), "AUDIOBOOKS", ITEMS)
        self.assertEqual([b["matchedBy"] for b in st["both"]], ["id"])
        self.assertEqual([i["title"] for i in st["serverOnly"]], ["Holes"])
        self.assertEqual(st["deviceOnly"], [])

    def test_side_loaded_book_the_server_has_is_matched_by_tags(self):
        """The important one: a hand-copied file with a scruffy name must not
        be offered for upload when the server already has it."""
        self.place("some_random_rip.m4a", "Holes", "Louis Sachar")
        st = device_mod.status(str(self.dev), "AUDIOBOOKS", ITEMS)
        self.assertEqual([b["matchedBy"] for b in st["both"]], ["tags"])
        self.assertEqual(st["deviceOnly"], [])

    def test_side_loaded_book_the_server_lacks_is_offered_for_upload(self):
        self.place("mystery.m4a", "The Silmarillion", "J.R.R. Tolkien")
        st = device_mod.status(str(self.dev), "AUDIOBOOKS", ITEMS)
        self.assertEqual([e["title"] for e in st["deviceOnly"]], ["The Silmarillion"])
        self.assertEqual(st["deviceOnly"][0]["author"], "J.R.R. Tolkien")

    def test_folder_name_matching_when_tags_are_absent(self):
        p = self.dev / "AUDIOBOOKS" / "Louis Sachar - Holes.m4a"
        p.write_bytes(b"no tags here at all")
        st = device_mod.status(str(self.dev), "AUDIOBOOKS", ITEMS)
        self.assertEqual([b["matchedBy"] for b in st["both"]], ["name"])

    def test_index_is_pruned_when_files_vanish(self):
        sync.pull(self.client, [ITEMS[0]], self.opts)
        (self.dev / "AUDIOBOOKS" / "Brian Jacques - Redwall.m4a").unlink()
        device_mod.scan(str(self.dev), "AUDIOBOOKS")
        self.assertEqual(index_mod.load(str(self.dev))["entries"], {})

    def test_non_audio_clutter_is_ignored(self):
        (self.dev / "AUDIOBOOKS" / "player.db").write_bytes(b"x")
        (self.dev / "AUDIOBOOKS" / "cover.jpg").write_bytes(b"x")
        self.assertEqual(device_mod.scan(str(self.dev), "AUDIOBOOKS"), [])

    def test_multi_file_book_titled_from_the_folder(self):
        d = self.dev / "AUDIOBOOKS" / "Big Book"
        d.mkdir()
        for i in (1, 2):
            (d / f"{i:03d}.m4a").write_bytes(mp4(f"Part {i}", "Some Author"))
        entry = device_mod.scan(str(self.dev), "AUDIOBOOKS")[0]
        self.assertEqual(entry["title"], "Big Book")
        self.assertEqual(entry["files"], 2)


class TestMultiFileDownload(DeviceBase):
    """Audiobookshelf returns a zip when a book is more than one file."""

    def zipped(self, *names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                z.writestr(n, b"audio")
            z.writestr("cover.jpg", b"not audio")
        return buf.getvalue()

    def test_zip_is_expanded_numbered_and_renamed(self):
        client = FakeClient({"li_1": (self.zipped("one.m4b", "two.m4b"),
                                      "book.zip", "application/zip")})
        rep = sync.pull(client, [ITEMS[0]], self.opts)
        out = self.dev / "AUDIOBOOKS" / "Brian Jacques - Redwall"
        self.assertEqual(rep["copied"], 2)
        self.assertEqual(sorted(p.suffix for p in out.iterdir()), [".m4a", ".m4a"])
        self.assertTrue(all(p.name[:3].isdigit() for p in out.iterdir()),
                        "parts must sort in order on the player")

    def test_the_cover_is_not_treated_as_a_track(self):
        client = FakeClient({"li_1": (self.zipped("one.m4b"), "book.zip", "application/zip")})
        sync.pull(client, [ITEMS[0]], self.opts)
        out = self.dev / "AUDIOBOOKS" / "Brian Jacques - Redwall"
        self.assertEqual([p.suffix for p in out.iterdir()], [".m4a"])

    def test_a_zip_with_no_audio_is_an_error_not_an_empty_book(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", b"nothing here")
        client = FakeClient({"li_1": (buf.getvalue(), "b.zip", "application/zip")})
        rep = sync.pull(client, [ITEMS[0]], self.opts)
        self.assertEqual(rep["copied"], 0)
        self.assertIn("no audio files", rep["errors"][0])

    def test_a_download_failure_is_reported_not_raised(self):
        class Broken(FakeClient):
            def open_download(self, item_id, timeout=None):
                raise AbsError("server said 500")
        rep = sync.pull(Broken(), [ITEMS[0]], self.opts)
        self.assertEqual(rep["copied"], 0)
        self.assertIn("500", rep["errors"][0])


class TestPush(DeviceBase):
    def test_upload_restores_the_m4b_extension(self):
        self.place("scruffy_name.m4a", "The Silmarillion", "J.R.R. Tolkien")
        st = device_mod.status(str(self.dev), "AUDIOBOOKS", ITEMS)
        client = FakeClient()
        rep = sync.push(client, st["deviceOnly"],
                        dict(self.opts, libraryId="lib1", folderId="fol1"))
        self.assertEqual(rep["uploaded"], 1)
        sent = client.uploads[0]
        self.assertEqual(sent["title"], "The Silmarillion")   # from the tags
        self.assertEqual(sent["author"], "J.R.R. Tolkien")    # from the tags
        self.assertEqual(sent["names"], ["scruffy_name.m4b"])  # rename undone

    def test_upload_without_a_target_is_an_error_not_a_crash(self):
        self.place("x.m4a", "Some Book", "Someone")
        st = device_mod.status(str(self.dev), "AUDIOBOOKS", ITEMS)
        rep = sync.push(FakeClient(), st["deviceOnly"], self.opts)
        self.assertEqual(rep["uploaded"], 0)
        self.assertIn("no target library", rep["errors"][0])


class TestRemove(DeviceBase):
    def test_removes_and_forgets(self):
        sync.pull(self.client, [ITEMS[0]], self.opts)
        rep = sync.remove(["Brian Jacques - Redwall.m4a"], self.opts)
        self.assertEqual(rep["removed"], ["Brian Jacques - Redwall.m4a"])
        self.assertEqual(index_mod.load(str(self.dev))["entries"], {})

    def test_nothing_outside_the_device_folder_is_touched(self):
        outside = self.dev / "keepme.txt"
        outside.write_text("not in the subdir")
        sync.pull(self.client, [ITEMS[0]], self.opts)
        sync.remove(["Brian Jacques - Redwall.m4a"], self.opts)
        self.assertTrue(outside.exists())

    def test_refuses_traversal_and_absolute_paths(self):
        outside = self.tmp / "precious.txt"
        outside.write_text("keep me")
        rep = sync.remove(["../../precious.txt", str(outside)], self.opts)
        self.assertEqual(rep["removed"], [])
        self.assertTrue(outside.exists())
        self.assertEqual(len(rep["errors"]), 2)

    @unittest.skipUnless(hasattr(__import__("os"), "symlink"), "needs symlinks")
    def test_refuses_symlinks(self):
        outside = self.tmp / "precious.txt"
        outside.write_text("keep me")
        try:
            (self.dev / "AUDIOBOOKS" / "sneaky").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted here")
        rep = sync.remove(["sneaky"], self.opts)
        self.assertEqual(rep["removed"], [])
        self.assertTrue(outside.exists())
        self.assertIn("symlink", rep["errors"][0])

    def test_partial_success(self):
        sync.pull(self.client, [ITEMS[0]], self.opts)
        rep = sync.remove(["Brian Jacques - Redwall.m4a", "ghost"], self.opts)
        self.assertEqual(len(rep["removed"]), 1)
        self.assertEqual(len(rep["errors"]), 1)


# -------------------------------------------------------------- config
class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        cfg = config_mod.load(path=self.path)
        cfg["absUrl"] = "http://x:1"
        cfg["apiKey"] = "secret"
        config_mod.save(cfg, path=self.path)
        self.assertEqual(config_mod.load(path=self.path)["absUrl"], "http://x:1")

    def test_secret_is_never_printed(self):
        r = config_mod.redacted({"apiKey": "secret", "absUrl": "http://x"})
        self.assertEqual(r["apiKey"], "***")
        self.assertEqual(r["absUrl"], "http://x")

    def test_environment_overrides_the_file(self):
        config_mod.save({**config_mod.DEFAULTS, "absUrl": "http://file"}, path=self.path)
        with mock.patch.dict("os.environ", {"ABSH_ABS_URL": "http://env"}):
            self.assertEqual(config_mod.load(path=self.path)["absUrl"], "http://env")

    def test_booleans_come_back_as_booleans(self):
        with mock.patch.dict("os.environ", {"ABSH_RENAME_M4B": "false"}):
            self.assertIs(config_mod.load(path=self.path)["renameM4b"], False)

    def test_missing_names_what_to_set(self):
        gaps = config_mod.missing(dict(config_mod.DEFAULTS))
        self.assertEqual(len(gaps), 3)

    def test_unreadable_file_falls_back_to_defaults(self):
        self.path.write_text("{ not json")
        self.assertEqual(config_mod.load(path=self.path)["subdir"], "AUDIOBOOKS")


# ------------------------------------------------------------- devices
class TestDeviceDiscovery(unittest.TestCase):
    """Finding the player, so nobody has to remember its path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.clip = self.tmp / "CLIP"
        (self.clip / "AUDIOBOOKS").mkdir(parents=True)
        self.backup = self.tmp / "BACKUP"
        self.backup.mkdir()
        self.patch = mock.patch.object(devices_mod, "roots",
                                       lambda system=None: [self.clip, self.backup])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_one_with_an_audiobooks_folder_sorts_first(self):
        found = devices_mod.candidates()
        self.assertEqual([d["name"] for d in found], ["CLIP", "BACKUP"])
        self.assertTrue(found[0]["hasSubdir"])
        self.assertFalse(found[1]["hasSubdir"])

    def test_a_device_we_have_synced_to_before_scores_higher(self):
        plain = devices_mod.describe(self.backup)["score"]
        (self.backup / ".absh").mkdir()
        self.assertGreater(devices_mod.describe(self.backup)["score"], plain)

    def test_the_subdir_being_looked_for_is_the_configured_one(self):
        """Someone using a different folder must still be recognised."""
        odd = self.tmp / "ODD"
        (odd / "BOOKS").mkdir(parents=True)
        with mock.patch.object(devices_mod, "roots", lambda system=None: [odd]):
            self.assertFalse(devices_mod.candidates()[0]["hasSubdir"])
            self.assertTrue(devices_mod.candidates(subdir="BOOKS")[0]["hasSubdir"])

    def test_resolve_accepts_a_bare_volume_name(self):
        self.assertEqual(devices_mod.resolve("CLIP"), str(self.clip))
        self.assertEqual(devices_mod.resolve("clip"), str(self.clip), "case insensitive")

    def test_resolve_passes_an_existing_path_through(self):
        self.assertEqual(devices_mod.resolve(str(self.backup)), str(self.backup))

    def test_resolve_returns_nothing_for_a_name_that_is_not_mounted(self):
        self.assertIsNone(devices_mod.resolve("NOT_PLUGGED_IN"))
        self.assertIsNone(devices_mod.resolve(""))

    def test_reports_free_space(self):
        self.assertGreater(devices_mod.describe(self.clip)["free"], 0)

    def test_probing_a_vanished_volume_does_not_raise(self):
        gone = devices_mod.describe(self.tmp / "unplugged")
        self.assertEqual(gone["total"], 0)
        self.assertEqual(gone["score"], 0)


class TestPlatformRoots(unittest.TestCase):
    """Each OS keeps mounted volumes somewhere different."""

    def test_mac_skips_the_boot_volume(self):
        """/Volumes carries a symlink to / - offering that as a "device" would
        point sync and, worse, remove at the whole filesystem."""
        tmp = Path(tempfile.mkdtemp())
        try:
            vols = tmp / "Volumes"
            (vols / "CLIP").mkdir(parents=True)
            try:
                (vols / "Macintosh HD").symlink_to("/")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not permitted here")
            self.assertEqual([p.name for p in devices_mod._mac_roots(vols)], ["CLIP"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mac_handles_no_volumes_directory(self):
        self.assertEqual(devices_mod._mac_roots("/definitely/not/here"), [])

    def test_unknown_platform_falls_back_to_the_linux_layout(self):
        with mock.patch.object(devices_mod, "_linux_roots", lambda: ["sentinel"]):
            self.assertEqual(devices_mod.roots("freebsd13"), ["sentinel"])


# ----------------------------------------------------------------- tui
class TestTuiModel(unittest.TestCase):
    STATUS = {
        "both": [{"itemId": "li_1", "name": "A.m4a", "title": "Redwall",
                  "author": "Brian Jacques", "bytes": 100}],
        "serverOnly": [{"id": "li_2", "title": "Holes", "author": "Louis Sachar", "size": 200}],
        "deviceOnly": [{"name": "rip.m4a", "title": "Silmarillion",
                        "author": "Tolkien", "bytes": 400}],
        "free": {"free": 1000},
    }

    def test_actionable_rows_come_first(self):
        kinds = [r["kind"] for r in build_rows(self.STATUS)]
        self.assertEqual(kinds, ["server", "device", "both"])

    def test_filter_matches_the_device_filename_too(self):
        rows = build_rows(self.STATUS)
        self.assertEqual([r["title"] for r in filter_rows(rows, "rip")], ["Silmarillion"])

    def test_keys_are_unique_and_kind_aware(self):
        rows = build_rows(self.STATUS)
        self.assertEqual(len({_key(r) for r in rows}), len(rows))
        self.assertNotEqual(_key({"kind": "server", "id": "x", "name": None}),
                            _key({"kind": "both", "id": "x", "name": None}))

    def test_summary(self):
        rows = build_rows(self.STATUS)
        self.assertIn("nothing selected", summarise(rows, set()))
        self.assertIn("2 selected", summarise(rows, {_key(rows[0]), _key(rows[1])}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
