"""The commit-message rules and how the release notes read them."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import commit_messages as rules  # noqa: E402
import publish_download as publish  # noqa: E402


class SubjectTests(unittest.TestCase):
    def test_area_and_summary_pass(self):
        for subject in ("WebUI: the page width setting is per user",
                        "Telegraf: generate a config the installed Telegraf accepts",
                        "Security: PHP upload progress is off",
                        "Open-VM-Tools: stop listing the vmware-kmod service",
                        "FreeSense-system: package pf-ng 2.1 builds again"):
            with self.subTest(subject=subject):
                self.assertEqual(rules.problems(subject), [])

    def test_a_subject_without_an_area_is_rejected_with_an_example(self):
        for subject in ("fix the thing", "Fixed several bugs in the dashboard", "webui: lowercase area"):
            with self.subTest(subject=subject):
                found = rules.problems(subject)
                self.assertTrue(any("Area: what changed" in text for text in found), found)

    def test_short_summary_long_subject_and_full_stop(self):
        self.assertTrue(any("at least" in text for text in rules.problems("WebUI: fix")))
        self.assertTrue(any("under 120" in text for text in rules.problems("WebUI: " + "x" * 120)))
        self.assertTrue(any("full stop" in text for text in rules.problems("WebUI: the page width is per user.")))

    def test_drafts_are_rejected(self):
        for subject in ("WIP: something", "fixup! WebUI: the page width", "squash! WebUI: the page width"):
            with self.subTest(subject=subject):
                self.assertTrue(rules.problems(subject))

    def test_merge_revert_and_bot_commits_are_exempt(self):
        for subject in ("Merge branch main into feature", 'Revert "WebUI: the page width"',
                        "Bump actions/checkout from 4 to 5"):
            with self.subTest(subject=subject):
                self.assertEqual(rules.problems(subject), [])

    def test_empty_message(self):
        self.assertEqual(rules.problems(""), ["the commit has no subject line"])


class TrailerTests(unittest.TestCase):
    def test_release_note_replaces_the_subject(self):
        parsed = rules.parse_message("WebUI: tweak it\n\nBody text.\n\nRelease-Note: Pages can use the full window width")
        self.assertEqual(parsed["note"], "Pages can use the full window width")
        self.assertFalse(parsed["skip"])

    def test_skip_hides_the_commit(self):
        parsed = rules.parse_message("Build: tidy the workflow names\n\nRelease-Note: skip")
        self.assertTrue(parsed["skip"])
        self.assertIsNone(parsed["note"])

    def test_release_type_must_be_known(self):
        self.assertEqual(rules.parse_message("WebUI: a change\n\nRelease-Type: Security")["type"], "security")
        self.assertIsNone(rules.parse_message("WebUI: a change\n\nRelease-Type: banana")["type"])

    def test_trailer_problems(self):
        self.assertTrue(rules.problems("WebUI: a change here ok\n\nRelease-Note: short"))
        self.assertTrue(rules.problems("WebUI: a change here ok\n\nRelease-Type: banana"))
        self.assertEqual(rules.problems("WebUI: a change here ok\n\nRelease-Note: skip\nRelease-Type: ui"), [])


class PublisherTests(unittest.TestCase):
    """github_compare turns commit messages into change lines."""

    def compare(self, messages):
        commits = [{"commit": {"message": message}} for message in messages]
        page = {"commits": commits, "total_commits": len(commits)}
        with mock.patch.object(publish.urllib.request, "urlopen"), \
                mock.patch.object(publish.json, "load", return_value=page):
            return publish.github_compare("FreeSense-org/freesense", "a" * 40, "b" * 40)

    def test_trailers_shape_the_notes(self):
        changes = self.compare([
            "WebUI: tweak the spacing\n\nRelease-Note: Compact density really tightens every page\nRelease-Type: ui",
            "Build: rename a workflow\n\nRelease-Note: skip",
            "Telegraf: generate a config the installed Telegraf accepts",
            "Security: PHP upload progress is off",
        ])
        self.assertEqual(changes, [
            {"type": "ui", "title": "Compact density really tightens every page"},
            {"type": "other", "title": "Telegraf: generate a config the installed Telegraf accepts"},
            {"type": "security", "title": "Security: PHP upload progress is off"},
        ])

    def test_old_commits_still_use_the_keyword_guess(self):
        changes = self.compare(["Fixed the broken thing in the dashboard"])
        self.assertEqual(changes[0]["type"], "fix")


class CliTests(unittest.TestCase):
    def test_range_check_reports_each_bad_commit(self):
        script = Path(__file__).resolve().parents[1] / "scripts/commit_messages.py"
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.org", *args],
                                      cwd=directory, check=True, capture_output=True, text=True)
            git("init", "-q")
            Path(directory, "a").write_text("1")
            git("add", "a")
            git("commit", "-q", "-m", "Base: the first commit of the repository")
            base = git("rev-parse", "HEAD").stdout.strip()
            for index, message in enumerate(("WebUI: the page width setting is per user", "oops fixed it")):
                Path(directory, "a").write_text(str(index + 2))
                git("commit", "-q", "-am", message)
            result = subprocess.run([sys.executable, str(script), "--range", f"{base}..HEAD"],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("'oops fixed it'", result.stdout)
            reported = [line for line in result.stdout.splitlines() if line and not line.startswith(" ")]
            self.assertEqual(len(reported), 1, result.stdout)
            good = subprocess.run([sys.executable, str(script), "--range", f"{base}..HEAD~1"],
                                  cwd=directory, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)


if __name__ == "__main__":
    unittest.main()
