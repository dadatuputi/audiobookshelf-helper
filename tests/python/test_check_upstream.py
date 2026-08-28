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
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state.json"
        self.out = self.tmp / "report.md"

    def run_with(self, releases, argv_extra=()):
        argv = ["check_upstream", "--state", str(self.state),
                "--output", str(self.out), *argv_extra]
        with mock.patch.object(UP, "fetch", return_value=releases), \
             mock.patch("sys.argv", argv):
            return UP.main()

    def test_first_run_records_without_shouting(self):
        """A brand new checkout should not open an issue about history."""
        self.assertEqual(self.run_with([rel("v2.30.0")], ["--update"]), 0)
        self.assertFalse(self.out.exists())
        self.assertEqual(json.loads(self.state.read_text())["lastSeen"], "v2.30.0")

    def test_reports_only_releases_newer_than_the_last_seen(self):
        self.state.write_text(json.dumps({"lastSeen": "v2.28.0"}))
        self.run_with([rel("v2.30.0", "- api change"), rel("v2.29.0"), rel("v2.28.0")])
        body = self.out.read_text()
        self.assertIn("v2.30.0", body)
        self.assertIn("v2.29.0", body)
        self.assertNotIn("v2.28.0", body)

    def test_no_report_when_nothing_is_new(self):
        self.state.write_text(json.dumps({"lastSeen": "v2.30.0"}))
        self.run_with([rel("v2.30.0")])
        self.assertFalse(self.out.exists())

    def test_update_advances_the_pin(self):
        self.state.write_text(json.dumps({"lastSeen": "v2.29.0"}))
        self.run_with([rel("v2.30.0")], ["--update"])
        self.assertEqual(json.loads(self.state.read_text())["lastSeen"], "v2.30.0")

    def test_drafts_are_ignored(self):
        self.state.write_text(json.dumps({"lastSeen": "v2.29.0"}))
        draft = {**rel("v2.31.0"), "draft": True}
        with mock.patch.object(UP, "fetch", return_value=[draft, rel("v2.30.0")]), \
             mock.patch("sys.argv", ["x", "--state", str(self.state),
                                     "--output", str(self.out)]):
            UP.main()
        self.assertNotIn("v2.31.0", self.out.read_text())

    def test_github_being_down_is_not_a_build_failure(self):
        with mock.patch.object(UP, "fetch",
                               side_effect=urllib.error.URLError("boom")), \
             mock.patch("sys.argv", ["x", "--state", str(self.state),
                                     "--output", str(self.out)]):
            self.assertEqual(UP.main(), 0)
        self.assertFalse(self.out.exists())

    def test_unreadable_state_is_treated_as_a_first_run(self):
        self.state.write_text("{ not json")
        self.assertEqual(UP.load_state(self.state)["lastSeen"], None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
