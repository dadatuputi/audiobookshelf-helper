"""The native messaging protocol itself.

What the engine does is covered in test_absh.py; this covers the wire: framing,
opt-in progress, request ids, and turning failures into answers rather than a
dead helper.
"""
import io, json, os, struct, subprocess, sys, shutil, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from absh import host as H  # noqa: E402

HOST_SCRIPT = ROOT / "native" / "absh_host.py"


def frame(msgs):
    return b"".join(struct.pack("<I", len(b)) + b
                    for b in (json.dumps(m).encode() for m in msgs))


def unframe(raw):
    out, i = [], 0
    while i < len(raw):
        n = struct.unpack("<I", raw[i:i + 4])[0]
        i += 4
        out.append(json.loads(raw[i:i + n]))
        i += n
    return out


class TestFraming(unittest.TestCase):
    def test_short_reads_are_reassembled(self):
        """A pipe may hand back one byte at a time; the framing must cope or
        every message after the first is garbage."""
        class Dribble(io.RawIOBase):
            def __init__(self, data):
                self.data, self.i = data, 0

            def read(self, n=-1):
                chunk = self.data[self.i:self.i + 1]
                self.i += len(chunk)
                return chunk

        stream = Dribble(frame([{"cmd": "ping"}]))
        self.assertEqual(H.read_msg(stream), {"cmd": "ping"})

    def test_clean_end_of_stream(self):
        self.assertIsNone(H.read_msg(io.BytesIO(b"")))

    def test_truncated_body_is_not_a_hang(self):
        self.assertIsNone(H.read_msg(io.BytesIO(struct.pack("<I", 50) + b"short")))

    def test_write_then_read_round_trip(self):
        buf = io.BytesIO()
        H.write_msg({"hello": "world", "n": 3}, buf)
        buf.seek(0)
        self.assertEqual(H.read_msg(buf), {"hello": "world", "n": 3})


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"
        self.cfg.write_text(json.dumps({"absUrl": "http://127.0.0.1:9",
                                        "apiKey": "k", "devicePath": str(self.tmp / "gone")}))
        self.env = dict(os.environ, ABSH_CONFIG=str(self.cfg))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_host(self, msgs):
        p = subprocess.run([sys.executable, str(HOST_SCRIPT)], input=frame(msgs),
                           capture_output=True, timeout=120, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[:600])
        return unframe(p.stdout)

    def test_ping_reports_what_the_helper_can_do(self):
        r = self.run_host([{"cmd": "ping"}])[0]
        self.assertTrue(r["ok"])
        self.assertIn("version", r)
        self.assertIn(r["tags"], ("mutagen", "builtin"))

    def test_unknown_command(self):
        r = self.run_host([{"cmd": "nope"}])[0]
        self.assertFalse(r["ok"])
        self.assertIn("unknown cmd", r["error"])

    def test_several_messages_get_several_replies(self):
        self.assertEqual(len(self.run_host([{"cmd": "ping"}] * 3)), 3)

    def test_a_missing_device_is_an_answer_not_a_crash(self):
        r = self.run_host([{"cmd": "status"}])[0]
        self.assertFalse(r["ok"])
        self.assertIn("not mounted", r["error"])

    def test_request_id_is_echoed(self):
        r = self.run_host([{"cmd": "ping", "rid": 42}])[0]
        self.assertEqual(r["rid"], 42)

    def test_every_reply_is_marked_done(self):
        for r in self.run_host([{"cmd": "ping"}, {"cmd": "nope"}]):
            self.assertEqual(r["event"], "done")

    def test_the_api_key_is_never_echoed_back(self):
        r = self.run_host([{"cmd": "config"}])[0]
        self.assertEqual(r["config"]["apiKey"], "***")


class TestProgressOptIn(unittest.TestCase):
    """A caller that sends one message and reads one reply must not find
    progress events queued ahead of its answer."""

    def test_off_by_default(self):
        seen = []
        H.handle({"cmd": "ping"}, seen.append)
        self.assertEqual(seen, [])

    def test_settings_from_the_request_win_over_the_file(self):
        cfg = H.settings({"cmd": "ping", "subdir": "CUSTOM"})
        self.assertEqual(cfg["subdir"], "CUSTOM")

    def test_blank_request_values_do_not_clobber_the_file(self):
        cfg = H.settings({"cmd": "ping", "subdir": ""})
        self.assertEqual(cfg["subdir"], "AUDIOBOOKS")


class TestWirePayload(unittest.TestCase):
    def test_status_does_not_leak_filesystem_paths(self):
        st = {"both": [{"name": "a", "kind": "file", "bytes": 1, "files": 1,
                        "title": "T", "author": "A", "itemId": "i", "matchedBy": "id",
                        "paths": ["/Users/someone/secret/a.m4a"],
                        "item": {"id": "i", "title": "T", "author": "A", "size": 1}}],
              "serverOnly": [], "deviceOnly": [], "free": {}, "onDeviceBytes": 1}
        wire = json.dumps(H._serialisable(st))
        self.assertNotIn("/Users/someone", wire)
        self.assertIn('"matchedBy": "id"', wire)

if __name__ == "__main__":
    unittest.main(verbosity=2)


class Watch(unittest.TestCase):
    """The one command that answers and then keeps talking."""

    def test_answers_at_once_and_says_whether_it_has_to_poll(self):
        reply = H.handle({"cmd": "watch"})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["watching"])
        # Honest about the platform rather than claiming events everywhere.
        self.assertIn(reply["polls"], (True, False))

    def test_asking_twice_does_not_start_a_second_watcher(self):
        before = H._WATCHING
        H.handle({"cmd": "watch"})
        self.assertIs(H._WATCHING, before or H._WATCHING)
        first = H._WATCHING
        H.handle({"cmd": "watch"})
        self.assertIs(H._WATCHING, first)

    def test_an_event_is_framed_like_any_other_message(self):
        """The push shares the pipe with replies, so it must be framed and
        whole - a half-written event would desynchronise every reply after
        it."""
        buf = io.BytesIO()
        H.write_msg({"event": "devices-changed"}, stream=buf)
        self.assertEqual(unframe(buf.getvalue()), [{"event": "devices-changed"}])


