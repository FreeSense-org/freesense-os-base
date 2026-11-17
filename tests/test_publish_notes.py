"""The rolling Development release-notes log is rewritten on every build."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_notes as notes  # noqa: E402

A, B, C = "a" * 40, "b" * 40, "c" * 40
S1, S2 = "1" * 64, "2" * 64
P1, P2 = "3" * 64, "4" * 64


def provenance(source=A):
    return {"source": source, "system_ports": A, "packages": A, "freebsd": A, "ports": A}


def release(source=A):
    return {"generation": 100, "system": S1, "packages_fingerprint": P1,
            "provenance": {**provenance(source), "os_definition": A}}


def empty_packages():
    return {"available": True, "updated": [], "added": [], "removed": [],
            "counts": {"updated": 0, "added": 0, "removed": 0}, "truncated": False}


class PublishNotesTests(unittest.TestCase):
    def update(self, existing, rel, source=B, system=S2, packages=P2, generation=200, commits=None):
        commits = commits if commits is not None else [{"type": "fix", "title": "WebUI: fix a thing"}]
        with mock.patch.object(notes.pd, "github_compare", return_value=commits), \
                mock.patch.object(notes.pd, "package_changes", return_value=empty_packages()):
            return notes.update_log(existing, rel, generation, provenance(source), system,
                                    packages, "https://pkg.example/v1", "amd64", "1.2.0")

    def test_first_run_takes_its_baseline_from_the_download_document(self):
        log = self.update(None, release())
        self.assertTrue(notes.valid_log(log))
        entry = log["entries"][0]
        self.assertEqual(entry["generation"], 200)
        self.assertEqual(entry["changes"][0]["scope"], "System")
        self.assertEqual(entry["platform"]["freebsd"]["from_commit"], A)

    def test_without_any_baseline_the_entry_records_inputs_but_no_changes(self):
        log = self.update(None, None)
        self.assertEqual(log["entries"][0]["changes"], [])
        self.assertIsNone(log["entries"][0]["platform"])

    def test_a_new_build_is_added_in_front(self):
        first = self.update(None, release())
        second = self.update(first, release(), source=C, system="5" * 64, packages="6" * 64, generation=300)
        self.assertEqual([entry["generation"] for entry in second["entries"]], [300, 200])
        self.assertEqual(second["entries"][0]["provenance"]["source"], C)

    def test_a_build_with_the_same_inputs_changes_nothing(self):
        first = self.update(None, release())
        self.assertIsNone(self.update(first, release(), generation=201))

    def test_the_log_keeps_only_the_newest_entries(self):
        log = self.update(None, release())
        for index in range(notes.LOG_LIMIT + 5):
            log = self.update(log, release(), source=f"{index + 16:040x}",
                              system=f"{index + 1:064x}", generation=300 + index)
        self.assertEqual(len(log["entries"]), notes.LOG_LIMIT)
        self.assertEqual(log["entries"][0]["generation"], 300 + notes.LOG_LIMIT + 4)

    def test_build_only_commits_never_appear(self):
        with mock.patch.object(notes.pd, "github_compare",
                               return_value=[{"type": "build", "title": "Build: tweak the workflow"}]):
            changes = notes.repository_changes(provenance(A), provenance(B))
        self.assertEqual(changes, [])

    def test_the_same_title_is_listed_once_per_scope(self):
        commit = {"type": "fix", "title": "Same title"}
        with mock.patch.object(notes.pd, "github_compare", return_value=[commit, commit]):
            changes = notes.repository_changes(provenance(A), provenance(B))
        scopes = [change["scope"] for change in changes]
        self.assertEqual(sorted(scopes), sorted(set(scopes)))

    def test_long_package_lists_are_trimmed_and_marked(self):
        long = empty_packages()
        long["updated"] = [{"name": f"p{n}", "from": "1", "to": "2", "origin": "x/y"} for n in range(80)]
        with mock.patch.object(notes.pd, "package_changes", return_value=long):
            platform = notes.platform_changes({"system": S1, "provenance": provenance()},
                                              provenance(B), S2, "https://x/v1", "amd64")
        self.assertEqual(len(platform["packages"]["updated"]), notes.PACKAGES_PER_LIST)
        self.assertTrue(platform["packages"]["truncated"])

    def test_an_invalid_published_log_is_never_overwritten(self):
        self.assertFalse(notes.valid_log({"schema_version": "other", "entries": []}))
        self.assertFalse(notes.valid_log({"schema_version": notes.LOG_SCHEMA,
                                          "entries": [{"generation": "x"}]}))

    def test_a_compare_failure_stops_the_step_without_a_new_log(self):
        with mock.patch.object(notes.pd, "github_compare", side_effect=SystemExit("compare failed")),                 mock.patch.object(notes.pd, "package_changes", return_value=empty_packages()):
            with self.assertRaises(SystemExit):
                notes.update_log(None, release(), 200, provenance(B), S2, P2,
                                 "https://pkg.example/v1", "amd64", "1.2.0")

    def test_provenance_comes_from_the_plan(self):
        plan = {"targets": {"amd64": {
            "system": {"source_sha": A, "system_sha": B, "freebsd_sha": C, "ports_sha": A},
            "packages": {"packages_sha": B}}}}
        self.assertEqual(notes.build_provenance(plan, "amd64"),
                         {"source": A, "system_ports": B, "packages": B, "freebsd": C, "ports": A})
        plan["targets"]["amd64"]["system"]["source_sha"] = "bad"
        with self.assertRaises(SystemExit):
            notes.build_provenance(plan, "amd64")


def raw(sha, message, date="2026-11-10T10:00:00Z"):
    return {"sha": sha, "commit": {"message": message, "committer": {"date": date}}}


class SinceStableTests(unittest.TestCase):
    """What is new in 1.2: on main, and not in RELENG_1_1."""

    def stable(self, ahead, behind):
        def compare(repository, before, after, limit=None):
            return ahead if before == notes.STABLE_BRANCH else behind
        with mock.patch.object(notes.pd, "github_compare_commits", side_effect=compare):
            return notes.stable_changes(provenance())

    def test_a_cherry_picked_commit_is_not_new_in_1_2(self):
        original = "1" * 40
        result = self.stable(
            ahead=[raw(original, "WebUI: the page width setting is per user"),
                   raw("2" * 40, "Dashboard: a new widget layout for 1.2 only")],
            behind=[raw("3" * 40, "WebUI: the page width setting is per user\n\n(cherry picked from commit %s)" % original)])
        titles = [change["title"] for change in result["changes"]]
        self.assertNotIn("WebUI: the page width setting is per user", titles)
        self.assertIn("Dashboard: a new widget layout for 1.2 only", titles)

    def test_a_cherry_pick_without_the_trailer_is_matched_by_subject(self):
        result = self.stable(ahead=[raw("1" * 40, "Telegraf: generate a config the installed Telegraf accepts")],
                             behind=[raw("2" * 40, "Telegraf: generate a config the installed Telegraf accepts")])
        self.assertEqual(result["changes"], [])

    def test_skipped_and_build_commits_are_not_listed(self):
        result = self.stable(
            ahead=[raw("1" * 40, "Build: rename a workflow\n\nRelease-Note: skip"),
                   raw("2" * 40, "CI: run the workflow on every release build"),
                   raw("3" * 40, "VPN: WireGuard peers can be disabled")],
            behind=[])
        self.assertEqual({change["title"] for change in result["changes"]}, {"VPN: WireGuard peers can be disabled"})
        self.assertEqual(len(result["changes"]), 3)  # once per repository scope

    def test_newest_first_each_title_once_and_capped(self):
        ahead = [raw(f"{index:040x}", f"Network: change number {index}", f"2026-11-{(index % 28) + 1:02d}T10:00:00Z")
                 for index in range(150)]
        result = self.stable(ahead=ahead, behind=[])
        # three repositories return the same commits; a title is listed once per scope
        self.assertEqual(result["total"], 150 * 3)
        self.assertEqual(len(result["changes"]), notes.SINCE_STABLE_LIMIT)
        dates = [change["date"] for change in result["changes"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual(result["baseline"], "RELENG_1_1")

    def test_the_section_is_written_validated_and_kept_when_github_fails(self):
        section = {"baseline": "RELENG_1_1", "total": 1,
                   "changes": [{"type": "feature", "title": "VPN: WireGuard peers can be disabled",
                                "scope": "System", "date": "2026-11-10"}]}
        with mock.patch.object(notes.pd, "github_compare", return_value=[]), \
                mock.patch.object(notes.pd, "package_changes", return_value=empty_packages()):
            first = notes.update_log(None, release(), 200, provenance(B), S2, P2,
                                     "https://x/v1", "amd64", "1.2.0", section)
            self.assertEqual(first["since_stable"]["changes"], section["changes"])
            self.assertEqual(first["since_stable"]["generation"], 200)
            self.assertTrue(notes.valid_log(first))
            second = notes.update_log(first, release(), 300, provenance(C), "5" * 64, "6" * 64,
                                      "https://x/v1", "amd64", "1.2.0", None)
        self.assertEqual(second["since_stable"]["changes"], section["changes"])
        self.assertEqual(second["since_stable"]["generation"], 200)

    def test_an_invalid_section_makes_the_log_invalid(self):
        log = {"schema_version": notes.LOG_SCHEMA, "entries": [],
               "since_stable": {"changes": [{"type": "nope", "title": "x", "scope": "System"}]}}
        self.assertFalse(notes.valid_log(log))


if __name__ == "__main__":
    unittest.main()
