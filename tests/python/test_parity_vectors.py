"""The fixtures that hold the Chromium folder backend to the helper's rules.

tests/fixtures/parity/*.json record what absh does - naming, the files a pull
writes, scan and classification, the built-in tag readers. The JavaScript
port in extension/src/folder.js is tested against the same files
(tests/js/folder.test.js, and in a real browser by tests/e2e/folder.spec.js).

This side regenerates them from the real code and compares. So changing a rule
in absh/ fails here until `python3 tools/parity_vectors.py --write` is run, and
that in turn fails the JavaScript tests until folder.js agrees. A change
cannot reach one implementation and not the other.
"""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("parity_vectors", ROOT / "tools" / "parity_vectors.py")
PV = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PV)

FIX = ROOT / "tests" / "fixtures" / "parity"
HINT = "absh and the parity fixtures disagree - run: python3 tools/parity_vectors.py --write"


class TestParityVectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fresh = PV.generate()

    def committed(self, name):
        return json.loads((FIX / name).read_text(encoding="utf-8"))

    def compare(self, name, *sections):
        want = self.committed(name)
        got = json.loads(PV.dumps(self.fresh[name]))
        for s in sections:
            with self.subTest(section=s):
                self.assertEqual(got[s], want[s], f"{name} [{s}]: {HINT}")
        self.assertEqual(got, want, f"{name}: {HINT}")

    def test_naming(self):
        self.compare("naming.json", "clean", "targetName", "safeSubdir", "path", "outExt",
                     "sourceExt", "normKey", "normalizeItem", "pull", "push", "remove")

    def test_device_scan_and_classification(self):
        self.compare("device.json", "server", "cases")

    def test_builtin_tag_readers(self):
        self.compare("tags.json", "files", "books")

    def test_the_files_on_disk_are_exactly_what_the_generator_writes(self):
        """Byte for byte, so a hand edit to a fixture is caught too."""
        self.assertEqual(PV.main(["--check"]), 0, HINT)

    def test_every_rule_has_cases(self):
        """An empty section would pass in both languages and prove nothing."""
        n = self.fresh["naming.json"]
        for k, v in n.items():
            self.assertTrue(v, f"naming.json has no {k} cases")
        self.assertGreaterEqual(len(self.fresh["device.json"]["cases"]), 5)
        self.assertGreaterEqual(len(self.fresh["tags.json"]["files"]), 20)

    def test_the_cases_cover_what_a_port_gets_wrong(self):
        """The rules a reimplementation is likeliest to miss, each pinned by
        at least one vector rather than left to chance."""
        n = self.fresh["naming.json"]
        clean = {c["in"]: c["out"] for c in n["clean"]}
        self.assertEqual(clean["x\x7fy"], "x\x7fy")           # DEL is not reserved
        self.assertEqual(clean["日本語"], "Untitled")          # nothing ASCII survives
        # Templates substitute in order, so a placeholder inside a value is
        # itself replaced.
        tn = {(json.dumps(c["book"], sort_keys=True), c["template"]): c["out"]
              for c in n["targetName"]}
        self.assertEqual(tn[(json.dumps({"author": "{title}", "title": "Echo"},
                                        sort_keys=True), "{author} - {title}")], "Echo - Echo")
        # Paths sort part by part, not as strings: "Disc 1/01" before "Disc 1.m4a".
        mixed = self.fresh["device.json"]["cases"][0]["expect"]["scan"]
        parts = next(e for e in mixed if e["name"] == "Hobbit parts")["paths"]
        self.assertLess(parts.index("AUDIOBOOKS/Hobbit parts/Disc 1/01.m4a"),
                        parts.index("AUDIOBOOKS/Hobbit parts/Disc 1.m4a"))
        # ID3 encoding 0 is Latin-1, not the windows-1252 a browser means by it.
        files = {f["name"]: f["out"] for f in self.fresh["tags.json"]["files"]}
        self.assertEqual(files["latin1.mp3"]["author"], "\x80\x93\x9f\xe9\xff")
        # str.strip() keeps a BOM and drops \x1c, unlike String.trim().
        self.assertEqual(files["whitespace.m4a"]["title"], "﻿Title")


if __name__ == "__main__":
    unittest.main()
