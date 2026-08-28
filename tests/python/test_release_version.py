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

    def test_firefox_prerelease_sorts_below_the_release(self):
        """1.0.0a1 < 1.0.0 in Mozilla's version comparison."""
        self.assertEqual(RV.parse_tag("v1.0.0-alpha.1")["firefox"], "1.0.0a1")
        self.assertEqual(RV.parse_tag("v1.0.0-beta.2")["firefox"], "1.0.0b2")
        self.assertEqual(RV.parse_tag("v1.0.0-rc.1")["firefox"], "1.0.0rc1")

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
        self.assertEqual(RV.parse_tag("v1.0.0-alpha")["firefox"], "1.0.0a1")

    def test_dashless_form_is_accepted(self):
        self.assertEqual(RV.parse_tag("v1.0.0-beta3")["firefox"], "1.0.0b3")

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
        self.assertEqual(BUILD.manifest_for("firefox", v["firefox"])["version"], "1.0.0a1")
        self.assertEqual(BUILD.manifest_for("chrome", v["chrome"])["version"], "1.0.0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
