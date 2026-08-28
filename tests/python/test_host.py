"""Tests for the native messaging host.

Plain unittest so it runs with no third-party deps; pytest collects it too.
"""
import json, os, shutil, struct, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "native" / "absh_host.py"
sys.path.insert(0, str(ROOT / "native"))
import absh_host as H  # noqa: E402


# --------------------------------------------------------------- unit tests
class TestClean(unittest.TestCase):
    def test_strips_reserved_characters(self):
        self.assertEqual(H.clean('a/b\\c:d*e?f"g<h>i|j'), "abcdefghij")

    def test_transliterates_to_ascii(self):
        self.assertEqual(H.clean("Pema Chödrön"), "Pema Chodron")
        self.assertEqual(H.clean("Antoine de Saint-Exupéry"), "Antoine de Saint-Exupery")

    def test_collapses_whitespace(self):
        self.assertEqual(H.clean("  a   b  "), "a b")

    def test_never_returns_empty(self):
        self.assertEqual(H.clean(""), "Untitled")
        self.assertEqual(H.clean("///"), "Untitled")
        self.assertEqual(H.clean(None), "Untitled")


class TestOutExt(unittest.TestCase):
    def test_m4b_renamed_when_enabled(self):
        self.assertEqual(H.out_ext("x.m4b", True), ".m4a")
        self.assertEqual(H.out_ext("X.M4B", True), ".m4a")

    def test_m4b_preserved_when_disabled(self):
        self.assertEqual(H.out_ext("x.m4b", False), ".m4b")

    def test_other_extensions_untouched(self):
        for e in (".mp3", ".m4a", ".flac", ".opus"):
            self.assertEqual(H.out_ext("x" + e, True), e)


class TestHuman(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(H.human(512), "512B")
        self.assertEqual(H.human(2048), "2KB")
        self.assertEqual(H.human(5 * 1024**3), "5GB")


# ------------------------------------------------------------ copy behaviour
class CopyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lib = self.tmp / "lib"
        self.dev = self.tmp / "dev"
        self.dev.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def book(self, rel, files):
        d = self.lib / rel
        d.mkdir(parents=True, exist_ok=True)
        for name, data in files:
            p = d / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return d

    def opts(self, **kw):
        base = dict(devicePath=str(self.dev), subdir="AUDIOBOOKS", renameM4b=True,
                    folderTemplate="{author} - {title}", sourceMode="local",
                    localRoot=str(self.lib))
        base.update(kw)
        return base

    def run_copy(self, item, **kw):
        rep = {"copied": 0, "skipped": 0, "errors": []}
        H.copy_book(item, self.opts(**kw), rep)
        return rep


class TestSingleFile(CopyBase):
    def test_single_m4b_becomes_m4a_at_top_level(self):
        self.book("A/Book", [("Book.m4b", b"x" * 100)])
        rep = self.run_copy({"title": "Book", "author": "A", "relPath": "A/Book"})
        self.assertEqual(rep["copied"], 1)
        self.assertEqual(rep["errors"], [])
        out = self.dev / "AUDIOBOOKS" / "A - Book.m4a"
        self.assertTrue(out.exists(), list((self.dev / "AUDIOBOOKS").iterdir()))
        self.assertEqual(out.read_bytes(), b"x" * 100)

    def test_rename_disabled_keeps_m4b(self):
        self.book("A/Book", [("Book.m4b", b"x")])
        self.run_copy({"title": "Book", "author": "A", "relPath": "A/Book"},
                      renameM4b=False)
        self.assertTrue((self.dev / "AUDIOBOOKS" / "A - Book.m4b").exists())


class TestMultiFile(CopyBase):
    def test_numbered_and_in_folder(self):
        self.book("A/Multi", [(f"Track {i}.mp3", b"y" * i) for i in (1, 2, 3)])
        rep = self.run_copy({"title": "Multi", "author": "A", "relPath": "A/Multi"})
        self.assertEqual(rep["copied"], 3)
        d = self.dev / "AUDIOBOOKS" / "A - Multi"
        names = sorted(p.name for p in d.iterdir())
        self.assertEqual(names, ["001 - Track 1.mp3", "002 - Track 2.mp3", "003 - Track 3.mp3"])

    def test_disc_subfolders_flattened_in_order(self):
        self.book("A/Discs/Disc 01", [("Track 1.mp3", b"a"), ("Track 2.mp3", b"b")])
        self.book("A/Discs/Disc 02", [("Track 1.mp3", b"c")])
        rep = self.run_copy({"title": "Discs", "author": "A", "relPath": "A/Discs"})
        self.assertEqual(rep["copied"], 3)
        d = self.dev / "AUDIOBOOKS" / "A - Discs"
        names = sorted(p.name for p in d.iterdir())
        self.assertEqual(len(names), 3)
        # disc 1 tracks must sort before disc 2
        self.assertEqual(
            [(d / n).read_bytes() for n in names], [b"a", b"b", b"c"])


class TestIdempotency(CopyBase):
    def test_same_size_is_skipped(self):
        self.book("A/Book", [("Book.m4b", b"x" * 50)])
        item = {"title": "Book", "author": "A", "relPath": "A/Book"}
        self.assertEqual(self.run_copy(item)["copied"], 1)
        rep = self.run_copy(item)
        self.assertEqual((rep["copied"], rep["skipped"]), (0, 1))

    def test_changed_size_is_recopied(self):
        self.book("A/Book", [("Book.m4b", b"x" * 50)])
        item = {"title": "Book", "author": "A", "relPath": "A/Book"}
        self.run_copy(item)
        self.book("A/Book", [("Book.m4b", b"x" * 80)])
        rep = self.run_copy(item)
        self.assertEqual(rep["copied"], 1)


class TestErrors(CopyBase):
    def test_missing_on_local_share_reports_error(self):
        rep = self.run_copy({"title": "Nope", "author": "A", "relPath": "A/Nope"})
        self.assertEqual(rep["copied"], 0)
        self.assertTrue(any("not found" in e for e in rep["errors"]))

    def test_non_audio_files_ignored(self):
        self.book("A/Book", [("Book.m4b", b"x"), ("cover.jpg", b"j"),
                             ("metadata.json", b"{}")])
        rep = self.run_copy({"title": "Book", "author": "A", "relPath": "A/Book"})
        self.assertEqual(rep["copied"], 1)


class TestTemplate(CopyBase):
    def test_series_placeholder(self):
        self.book("A/Book", [("Book.m4b", b"x")])
        self.run_copy({"title": "Book", "author": "A", "series": "S", "relPath": "A/Book"},
                      folderTemplate="{series} - {title}")
        self.assertTrue((self.dev / "AUDIOBOOKS" / "S - Book.m4a").exists())

    def test_empty_author_does_not_leave_dangling_separator(self):
        self.book("A/Book", [("Book.m4b", b"x")])
        self.run_copy({"title": "Book", "author": "", "relPath": "A/Book"})
        self.assertTrue((self.dev / "AUDIOBOOKS" / "Book.m4a").exists())


# ------------------------------------------------------- protocol / end-to-end
def call_host(messages):
    """Speak the real 4-byte-length native messaging protocol to the host."""
    buf = b""
    for m in messages:
        b = json.dumps(m).encode()
        buf += struct.pack("<I", len(b)) + b
    p = subprocess.run([sys.executable, str(HOST)], input=buf,
                       capture_output=True, timeout=120)
    out, res, i = p.stdout, [], 0
    while i < len(out):
        n = struct.unpack("<I", out[i:i + 4])[0]
        i += 4
        res.append(json.loads(out[i:i + n]))
        i += n
    return res


class TestProtocol(CopyBase):
    def test_ping(self):
        r = call_host([{"cmd": "ping"}])[0]
        self.assertTrue(r["ok"])
        self.assertIn("version", r)

    def test_unknown_command(self):
        r = call_host([{"cmd": "wat"}])[0]
        self.assertFalse(r["ok"])

    def test_device_not_mounted(self):
        r = call_host([{"cmd": "sync", "devicePath": str(self.tmp / "absent"),
                        "items": []}])[0]
        self.assertFalse(r["ok"])
        self.assertIn("not mounted", r["error"])

    def test_multiple_messages_in_one_stream(self):
        rs = call_host([{"cmd": "ping"}, {"cmd": "ping"}])
        self.assertEqual(len(rs), 2)

    def test_full_sync_round_trip(self):
        self.book("A/Book", [("Book.m4b", b"z" * 10)])
        r = call_host([{"cmd": "sync", **self.opts(),
                        "items": [{"title": "Book", "author": "A",
                                   "relPath": "A/Book"}]}])[0]
        self.assertTrue(r["ok"])
        self.assertEqual(r["copied"], 1)
        self.assertIn("freeAfter", r)
        self.assertTrue((self.dev / "AUDIOBOOKS" / "A - Book.m4a").exists())


class TestHttpZip(CopyBase):
    """ABS returns a zip for multi-file books; exercise that path offline."""

    def test_zip_is_expanded_and_renamed(self):
        z = self.tmp / "book.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("one.m4b", b"a")
            f.writestr("two.m4b", b"b")
            f.writestr("cover.jpg", b"nope")

        class FakeResp:
            headers = {"Content-Type": "application/zip",
                       "Content-Disposition": 'attachment; filename="book.zip"'}

            def __init__(self, p): self._f = open(p, "rb")
            def read(self, n=-1): return self._f.read(n)
            def __enter__(self): return self
            def __exit__(self, *a): self._f.close()

        orig = H.urllib.request.urlopen
        H.urllib.request.urlopen = lambda *a, **k: FakeResp(z)
        try:
            rep = {"copied": 0, "skipped": 0, "errors": []}
            H.copy_book({"title": "Book", "author": "A", "relPath": "",
                         "url": "http://example/x"},
                        self.opts(sourceMode="http"), rep)
        finally:
            H.urllib.request.urlopen = orig
        self.assertEqual(rep["copied"], 2)
        d = self.dev / "AUDIOBOOKS" / "A - Book"
        self.assertEqual(sorted(p.suffix for p in d.iterdir()), [".m4a", ".m4a"])


# --------------------------------------------------------- hardening
class TestSafeSubdir(unittest.TestCase):
    def test_plain_subdir_survives(self):
        self.assertEqual(H.safe_subdir("AUDIOBOOKS"), Path("AUDIOBOOKS"))

    def test_nested_subdir_survives(self):
        self.assertEqual(H.safe_subdir("AUDIOBOOKS/Kids"), Path("AUDIOBOOKS") / "Kids")

    def test_traversal_is_stripped(self):
        self.assertEqual(H.safe_subdir("../../etc"), Path("etc"))
        self.assertEqual(H.safe_subdir("../.."), Path("AUDIOBOOKS"))

    def test_absolute_is_made_relative(self):
        self.assertFalse(H.safe_subdir("/etc/passwd").is_absolute())

    def test_empty_falls_back(self):
        self.assertEqual(H.safe_subdir(""), Path("AUDIOBOOKS"))
        self.assertEqual(H.safe_subdir(None), Path("AUDIOBOOKS"))


class TestUrlScheme(CopyBase):
    def test_file_url_is_refused(self):
        secret = self.tmp / "secret.m4b"
        secret.write_bytes(b"topsecret")
        rep = {"copied": 0, "skipped": 0, "errors": []}
        H.copy_book({"title": "Book", "author": "A", "relPath": "",
                     "url": secret.as_uri()},
                    self.opts(sourceMode="http"), rep)
        self.assertEqual(rep["copied"], 0)
        self.assertIn("refusing file url", rep["errors"][0])

    def test_schemeless_url_is_refused(self):
        rep = {"copied": 0, "skipped": 0, "errors": []}
        H.copy_book({"title": "Book", "author": "A", "relPath": "", "url": "/etc/passwd"},
                    self.opts(sourceMode="http"), rep)
        self.assertEqual(rep["copied"], 0)
        self.assertIn("refusing", rep["errors"][0])


class TestFraming(unittest.TestCase):
    def test_short_reads_are_reassembled(self):
        """A pipe may hand back fewer bytes than asked for; framing must cope."""
        import io

        class Dribble(io.RawIOBase):
            def __init__(self, data): self.data, self.i = data, 0
            def read(self, n=-1):
                chunk = self.data[self.i:self.i + 1]   # one byte at a time
                self.i += len(chunk)
                return chunk

        body = json.dumps({"cmd": "ping"}).encode()
        payload = struct.pack("<I", len(body)) + body
        orig = sys.stdin
        sys.stdin = type("S", (), {"buffer": Dribble(payload)})()
        try:
            self.assertEqual(H.read_msg(), {"cmd": "ping"})
        finally:
            sys.stdin = orig


# --------------------------------------------------------- on-device shelf
class TestListDevice(CopyBase):
    def test_reports_a_synced_book_as_present(self):
        self.book("A/Book", [("Book.m4b", b"x" * 100)])
        item = {"id": "i1", "title": "Book", "author": "A", "relPath": "A/Book"}
        H.copy_book(item, self.opts(), {"copied": 0, "skipped": 0, "errors": []})
        r = H.list_device({**self.opts(), "items": [item]})
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["onDevice"]), 1)
        self.assertEqual(r["onDevice"][0]["id"], "i1")
        self.assertEqual(r["onDevice"][0]["kind"], "file")
        self.assertEqual(r["onDevice"][0]["bytes"], 100)

    def test_absent_book_is_not_listed(self):
        item = {"id": "i9", "title": "Nope", "author": "A", "relPath": "A/Nope"}
        (self.dev / "AUDIOBOOKS").mkdir(parents=True)
        r = H.list_device({**self.opts(), "items": [item]})
        self.assertEqual(r["onDevice"], [])

    def test_multi_file_book_reported_as_dir_with_count(self):
        self.book("A/Big", [("01.mp3", b"a" * 10), ("02.mp3", b"b" * 20)])
        item = {"id": "i2", "title": "Big", "author": "A", "relPath": "A/Big"}
        H.copy_book(item, self.opts(), {"copied": 0, "skipped": 0, "errors": []})
        r = H.list_device({**self.opts(), "items": [item]})
        self.assertEqual(r["onDevice"][0]["kind"], "dir")
        self.assertEqual(r["onDevice"][0]["files"], 2)
        self.assertEqual(r["onDevice"][0]["bytes"], 30)

    def test_unknown_content_is_reported_as_an_orphan(self):
        root = self.dev / "AUDIOBOOKS"
        root.mkdir(parents=True)
        (root / "Something Else.mp3").write_bytes(b"y" * 50)
        r = H.list_device({**self.opts(), "items": []})
        self.assertEqual([o["name"] for o in r["orphans"]], ["Something Else.mp3"])

    def test_reports_free_space(self):
        (self.dev / "AUDIOBOOKS").mkdir(parents=True)
        r = H.list_device({**self.opts(), "items": []})
        self.assertIn("free", r)
        self.assertGreater(r["freeBytes"], 0)

    def test_device_not_mounted(self):
        r = H.handle({"cmd": "list", "devicePath": str(self.tmp / "gone"), "items": []})
        self.assertFalse(r["ok"])
        self.assertIn("not mounted", r["error"])


class TestRemove(CopyBase):
    def _synced(self):
        self.book("A/Book", [("Book.m4b", b"x" * 100)])
        item = {"id": "i1", "title": "Book", "author": "A", "relPath": "A/Book"}
        H.copy_book(item, self.opts(), {"copied": 0, "skipped": 0, "errors": []})
        return item

    def test_removes_a_single_file_book(self):
        self._synced()
        target = self.dev / "AUDIOBOOKS" / "A - Book.m4a"
        self.assertTrue(target.exists())
        r = H.remove_entries({**self.opts(), "names": ["A - Book.m4a"]})
        self.assertTrue(r["ok"])
        self.assertEqual(r["removed"], ["A - Book.m4a"])
        self.assertEqual(r["freedBytes"], 100)
        self.assertFalse(target.exists())

    def test_removes_a_multi_file_book_folder(self):
        self.book("A/Big", [("01.mp3", b"a" * 10), ("02.mp3", b"b" * 20)])
        item = {"id": "i2", "title": "Big", "author": "A", "relPath": "A/Big"}
        H.copy_book(item, self.opts(), {"copied": 0, "skipped": 0, "errors": []})
        d = self.dev / "AUDIOBOOKS" / "A - Big"
        self.assertTrue(d.is_dir())
        r = H.remove_entries({**self.opts(), "names": ["A - Big"]})
        self.assertEqual(r["removed"], ["A - Big"])
        self.assertFalse(d.exists())

    def test_leaves_the_library_untouched(self):
        self._synced()
        H.remove_entries({**self.opts(), "names": ["A - Book.m4a"]})
        self.assertTrue((self.lib / "A" / "Book" / "Book.m4b").exists())

    def test_traversal_is_refused(self):
        outside = self.tmp / "precious.txt"
        outside.write_text("keep me")
        (self.dev / "AUDIOBOOKS").mkdir(parents=True)
        r = H.remove_entries({**self.opts(), "names": ["../../precious.txt"]})
        self.assertEqual(r["removed"], [])
        self.assertTrue(outside.exists())
        self.assertIn("single entry", r["errors"][0])

    def test_absolute_path_is_refused(self):
        outside = self.tmp / "precious.txt"
        outside.write_text("keep me")
        (self.dev / "AUDIOBOOKS").mkdir(parents=True)
        r = H.remove_entries({**self.opts(), "names": [str(outside)]})
        self.assertEqual(r["removed"], [])
        self.assertTrue(outside.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_symlink_is_refused_and_target_survives(self):
        outside = self.tmp / "precious.txt"
        outside.write_text("keep me")
        root = self.dev / "AUDIOBOOKS"
        root.mkdir(parents=True)
        try:
            (root / "sneaky").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted here")
        r = H.remove_entries({**self.opts(), "names": ["sneaky"]})
        self.assertEqual(r["removed"], [])
        self.assertTrue(outside.exists())
        self.assertIn("symlink", r["errors"][0])

    def test_missing_entry_reports_an_error(self):
        (self.dev / "AUDIOBOOKS").mkdir(parents=True)
        r = H.remove_entries({**self.opts(), "names": ["ghost"]})
        self.assertEqual(r["removed"], [])
        self.assertIn("not on the device", r["errors"][0])

    def test_partial_success_still_removes_the_valid_one(self):
        self._synced()
        r = H.remove_entries({**self.opts(), "names": ["A - Book.m4a", "ghost"]})
        self.assertEqual(r["removed"], ["A - Book.m4a"])
        self.assertEqual(len(r["errors"]), 1)


class TestProgressStream(CopyBase):
    def test_progress_is_off_by_default(self):
        """One request, one reply - or a plain client reads an event as its answer."""
        self.book("A/Book", [("Book.m4b", b"z" * 10)])
        rs = call_host([{"cmd": "sync", **self.opts(),
                         "items": [{"title": "Book", "author": "A", "relPath": "A/Book"}]}])
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["event"], "done")
        self.assertEqual(rs[0]["copied"], 1)

    def test_progress_events_precede_the_result(self):
        self.book("A/Book", [("01.mp3", b"a" * 10), ("02.mp3", b"b" * 10)])
        rs = call_host([{"cmd": "sync", **self.opts(), "progress": True,
                         "items": [{"id": "i1", "title": "Book", "author": "A",
                                    "relPath": "A/Book"}]}])
        kinds = [r.get("event") for r in rs]
        self.assertEqual(kinds[0], "item")
        self.assertIn("progress", kinds)
        self.assertEqual(kinds[-1], "done")
        self.assertEqual(rs[-1]["copied"], 2)
        steps = [r for r in rs if r.get("event") == "progress"]
        self.assertEqual(steps[-1]["done"], 2)
        self.assertEqual(steps[-1]["total"], 2)

    def test_request_id_is_echoed_on_every_message(self):
        self.book("A/Book", [("Book.m4b", b"z" * 10)])
        rs = call_host([{"cmd": "sync", **self.opts(), "progress": True, "rid": 7,
                         "items": [{"id": "i1", "title": "Book", "author": "A",
                                    "relPath": "A/Book"}]}])
        self.assertTrue(all(r["rid"] == 7 for r in rs), rs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
