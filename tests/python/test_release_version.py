"""Tag -> per-store version mapping.

Getting this wrong is expensive: a bad version is rejected at upload time,
after the release has already been published.
"""
import importlib.util, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("relver", ROOT / "tools" / "release_version.py")
RV = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RV)


class TestStable(unittest.TestCase):
    def test_plain_tag(self):
        v = RV.parse_tag("v1.0.0")
        self.assertEqual(v["semver"], "1.0.0")
        self.assertEqual(v["firefox"], "1.0.0")
        self.assertEqual(v["chrome"], "1.0.0")
        self.assertFalse(v["prerelease"])

    def test_leading_v_is_optional(self):
        self.assertEqual(RV.parse_tag("1.2.3")["chrome"], "1.2.3")

    def test_larger_numbers(self):
        self.assertEqual(RV.parse_tag("v12.30.400")["firefox"], "12.30.400")


class TestPrerelease(unittest.TestCase):
    def test_alpha_is_marked_prerelease(self):
        v = RV.parse_tag("v1.0.0-alpha.1")
        self.assertTrue(v["prerelease"])
        self.assertEqual(v["kind"], "alpha")
        self.assertEqual(v["semver"], "1.0.0-alpha.1")

    def test_firefox_prerelease_is_numeric_like_chrome(self):
        """Was 1.0.0a1, which sorted below 1.0.0 - the ordering a prerelease
        wants. Mozilla removed letters from the version format, so that form
        is now a lint error and both stores get the same numbers."""
        self.assertEqual(RV.parse_tag("v1.0.0-alpha.1")["firefox"], "1.0.0.1")
        self.assertEqual(RV.parse_tag("v1.0.0-beta.2")["firefox"], "1.0.0.102")
        self.assertEqual(RV.parse_tag("v1.0.0-rc.1")["firefox"], "1.0.0.201")

    def test_a_prerelease_now_sorts_above_its_base(self):
        """The cost of dropping letters, asserted so it stays deliberate.
        Harmless only because a prerelease never reaches a listed channel."""
        v = RV.parse_tag("v1.0.0-alpha.1")
        self.assertEqual(v["base"], "1.0.0")
        self.assertEqual(v["firefox"], "1.0.0.1")

    def test_chrome_prerelease_is_all_numeric(self):
        for tag in ("v1.0.0-alpha.1", "v1.0.0-beta.2", "v1.0.0-rc.3"):
            chrome = RV.parse_tag(tag)["chrome"]
            parts = chrome.split(".")
            self.assertLessEqual(len(parts), 4)
            for p in parts:
                self.assertTrue(p.isdigit(), chrome)
                self.assertLessEqual(int(p), 65535)

    def test_chrome_orders_prereleases_among_themselves(self):
        def key(t):
            return [int(x) for x in RV.parse_tag(t)["chrome"].split(".")]
        self.assertLess(key("v1.0.0-alpha.1"), key("v1.0.0-alpha.2"))
        self.assertLess(key("v1.0.0-alpha.9"), key("v1.0.0-beta.1"))
        self.assertLess(key("v1.0.0-beta.9"), key("v1.0.0-rc.1"))

    def test_number_defaults_to_one(self):
        self.assertEqual(RV.parse_tag("v1.0.0-alpha")["firefox"], "1.0.0.1")

    def test_dashless_form_is_accepted(self):
        self.assertEqual(RV.parse_tag("v1.0.0-beta3")["firefox"], "1.0.0.103")

    def test_base_is_the_release_it_leads_to(self):
        self.assertEqual(RV.parse_tag("v1.0.0-alpha.1")["base"], "1.0.0")


class TestRejects(unittest.TestCase):
    def test_junk(self):
        for bad in ("", "v1", "v1.0", "1.0.0.0", "v1.0.0-dev.1", "release-1.0.0",
                    "v1.0.0-alpha.1.2", "vx.y.z"):
            with self.assertRaises(ValueError, msg=bad):
                RV.parse_tag(bad)

    def test_prerelease_number_beyond_chrome_range(self):
        with self.assertRaises(ValueError):
            RV.parse_tag("v1.0.0-alpha.70000")


class TestManifestAcceptance(unittest.TestCase):
    """Whatever comes out has to survive being stamped into a manifest."""

    def test_build_accepts_both_forms(self):
        bspec = importlib.util.spec_from_file_location(
            "absh_build", ROOT / "extension" / "build.py")
        BUILD = importlib.util.module_from_spec(bspec)
        bspec.loader.exec_module(BUILD)
        v = RV.parse_tag("v1.0.0-alpha.1")
        self.assertEqual(BUILD.manifest_for("firefox", v["firefox"])["version"], "1.0.0.1")
        self.assertEqual(BUILD.manifest_for("chrome", v["chrome"])["version"], "1.0.0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestStoreVersionFormat(unittest.TestCase):
    """Both stores take 1-4 integers, <=9 digits, no leading zeros.

    Firefox used to allow letters, and 1.0.0a1 sorted below 1.0.0 - the exact
    ordering a prerelease wants. Mozilla removed them; web-ext lint now calls
    the old form VERSION_FORMAT_INVALID. That shipped in a published alpha
    because CI only ever linted the dev build, which carries the manifest's
    own version and never a release one.
    """

    TAGS = ["v1.0.0", "v0.1.0", "v1.2.3", "v10.20.30",
            "v1.0.0-alpha.1", "v1.0.0-alpha.9", "v1.2.3-beta.2",
            "v2.0.0-rc.1", "v1.0.0-alpha"]

    def test_every_tag_yields_a_version_both_stores_accept(self):
        for tag in self.TAGS:
            info = RV.parse_tag(tag)
            for store in ("firefox", "chrome"):
                with self.subTest(tag=tag, store=store):
                    self.assertRegex(info[store], RV.VALID)

    def test_no_letters_anywhere(self):
        for tag in self.TAGS:
            info = RV.parse_tag(tag)
            for store in ("firefox", "chrome"):
                with self.subTest(tag=tag, store=store):
                    self.assertFalse(any(c.isalpha() for c in info[store]),
                                     f"{store} version {info[store]!r} has letters")

    def test_the_old_letter_form_would_be_rejected(self):
        # Guards the regex itself: if VALID ever loosens, this fails.
        for bad in ("1.0.0a1", "1.0.0b2", "1.0.0rc1", "1.0.0-alpha.1",
                    "1.0.01", "01.0.0", "1.0.0.0.0", "1234567890.0"):
            with self.subTest(bad=bad):
                self.assertNotRegex(bad, RV.VALID)

    def test_prereleases_of_one_base_stay_ordered(self):
        def parts(v):
            return tuple(int(x) for x in v.split("."))
        seq = [RV.parse_tag(t)["firefox"] for t in
               ("v1.0.0-alpha.1", "v1.0.0-alpha.2", "v1.0.0-beta.1", "v1.0.0-rc.1")]
        self.assertEqual(seq, sorted(seq, key=parts), seq)
        self.assertEqual(len(set(seq)), len(seq), "versions must be distinct")
