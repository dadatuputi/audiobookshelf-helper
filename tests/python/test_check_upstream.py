"""The upstream Audiobookshelf watcher.

Stubs the GitHub call: this must not need the network, and must not fail the
build when GitHub is unreachable - a watcher that breaks CI is a watcher that
gets switched off.
"""
import importlib.util, json, tempfile, unittest, urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("upstream", ROOT / "tools" / "check_upstream.py")
UP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(UP)

CONTRACT = json.loads((ROOT / "tests" / "fixtures" / "abs" / "contract.json").read_text())


def rel(tag, body=""):
    return {"tag_name": tag, "body": body, "draft": False,
            "html_url": f"https://github.com/advplyr/audiobookshelf/releases/tag/{tag}"}


class TestRelevantLines(unittest.TestCase):
    def test_picks_out_api_changes(self):
        body = ("* Fix cover art scaling\n"
                "* Update API: /api/libraries now returns paging info\n"
                "* Bump dependencies\n")
        hits = UP.relevant_lines(body)
        self.assertEqual(len(hits), 1)
        self.assertIn("/api/libraries", hits[0])

    def test_catches_auth_and_token_wording(self):
        for line in ("Breaking: JWT token query parameter removed",
                     "Change apiKey handling", "auth refactor"):
            self.assertTrue(UP.relevant_lines(f"- {line}"), line)

    def test_catches_the_metadata_fields_we_read(self):
        for line in ("rename authorName to author", "relPath is now absolute",
                     "numTracks removed from minified items"):
            self.assertTrue(UP.relevant_lines(f"- {line}"), line)

    def test_ignores_unrelated_notes(self):
        self.assertEqual(UP.relevant_lines("- Fix player seek bar on mobile"), [])

    def test_tolerates_an_empty_body(self):
        self.assertEqual(UP.relevant_lines(None), [])
        self.assertEqual(UP.relevant_lines(""), [])

    def test_caps_the_number_of_lines(self):
        body = "\n".join(f"- api change {i}" for i in range(60))
        self.assertLessEqual(len(UP.relevant_lines(body)), 25)


class TestReport(unittest.TestCase):
    def test_names_the_release_and_links_it(self):
        r = UP.build_report([rel("v2.30.0", "- api: something")], CONTRACT)
        self.assertIn("v2.30.0", r)
        self.assertIn("releases/tag/v2.30.0", r)

    def test_lists_every_endpoint_we_depend_on(self):
        r = UP.build_report([rel("v2.30.0")], CONTRACT)
        for e in CONTRACT["endpoints"]:
            self.assertIn(e["path"], r)

    def test_says_so_when_nothing_looks_relevant(self):
        r = UP.build_report([rel("v2.30.0", "- fix a typo")], CONTRACT)
        self.assertIn("Nothing in the changelog", r)

    def test_mentions_the_extra_releases_when_several_are_new(self):
        r = UP.build_report([rel("v2.30.0"), rel("v2.29.0"), rel("v2.28.0")], CONTRACT)
        self.assertIn("2 other release(s)", r)

    def test_points_at_the_fixtures_to_re_record(self):
        r = UP.build_report([rel("v2.30.0")], CONTRACT)
        self.assertIn("tests/fixtures/abs", r)
        self.assertIn("contract.test.js", r)


class TestRun(unittest.TestCase):
    """What is news is decided by the pinned version alone."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pin = self.tmp / "abs-versions.json"
        self.pin.write_text('{"_comment": "kept", "oldest": "v2.26.0", "newest": "v2.36.0"}\n')
        self.out = self.tmp / "report.md"
        self.gh = self.tmp / "gh-output"

    def run_with(self, releases):
        argv = ["check_upstream", "--versions", str(self.pin), "--output", str(self.out)]
        with mock.patch.object(UP, "fetch", return_value=releases), \
             mock.patch("sys.argv", argv), \
             mock.patch.dict("os.environ", {"GITHUB_OUTPUT": str(self.gh)}):
            return UP.main()

    def outputs(self):
        return dict(l.split("=", 1) for l in self.gh.read_text().splitlines())

    def test_a_release_newer_than_the_pin_is_news_on_the_very_first_run(self):
        """The old watcher kept its own record and never saved it, so it never
        reported anything. There is no first run now: the pin is the record."""
        self.assertEqual(self.run_with([rel("v2.37.1"), rel("v2.37.0"), rel("v2.36.0")]), 0)
        body = self.out.read_text()
        self.assertIn("v2.37.1", body)
        self.assertIn("since v2.36.0", body)
        self.assertEqual(self.outputs(), {"newer": "true", "latest": "v2.37.1", "pinned": "v2.36.0"})

    def test_nothing_newer_is_no_news(self):
        self.run_with([rel("v2.36.0"), rel("v2.35.1")])
        self.assertFalse(self.out.exists())
        self.assertEqual(self.outputs()["newer"], "false")
        self.assertEqual(self.outputs()["latest"], "v2.36.0")

    def test_newest_by_version_not_by_listing_order(self):
        self.run_with([rel("v2.36.1"), rel("v2.38.0"), rel("v2.37.0")])
        self.assertEqual(self.outputs()["latest"], "v2.38.0")

    def test_drafts_and_prereleases_are_not_tested_against(self):
        self.run_with([{**rel("v2.39.0"), "draft": True},
                       {**rel("v2.38.0-beta.1"), "prerelease": True},
                       {**rel("v2.38.0"), "prerelease": True}, rel("v2.37.0")])
        self.assertEqual(self.outputs()["latest"], "v2.37.0")

    def test_set_newest_moves_only_the_newest(self):
        argv = ["check_upstream", "--versions", str(self.pin), "--set-newest", "v2.38.0"]
        with mock.patch("sys.argv", argv):
            self.assertEqual(UP.main(), 0)
        self.assertEqual(json.loads(self.pin.read_text()),
                         {"_comment": "kept", "oldest": "v2.26.0", "newest": "v2.38.0"})

    def test_set_newest_refuses_what_is_not_a_tag(self):
        argv = ["check_upstream", "--versions", str(self.pin), "--set-newest", "main"]
        with mock.patch("sys.argv", argv):
            self.assertEqual(UP.main(), 2)
        self.assertIn('"newest": "v2.36.0"', self.pin.read_text())

    def test_the_real_range_file_names_both_ends(self):
        v = json.loads(UP.VERSIONS.read_text())
        self.assertTrue(UP.version_key(v["oldest"]) and UP.version_key(v["newest"]))
        self.assertLessEqual(UP.version_key(v["oldest"]), UP.version_key(v["newest"]))

    def test_an_unreachable_github_is_a_warning_not_a_failure(self):
        argv = ["check_upstream", "--versions", str(self.pin), "--output", str(self.out)]
        with mock.patch.object(UP, "fetch", side_effect=urllib.error.URLError("down")), \
             mock.patch("sys.argv", argv):
            self.assertEqual(UP.main(), 0)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
