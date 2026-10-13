from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


refresh = load_script("freesense_stable_refresh", "scripts/stable_refresh.py")
release_lock = load_script("freesense_release_lock_for_refresh", "scripts/release_lock.py")
import build_platform  # noqa: E402

POLICY = build_platform.load_policy(ROOT / "config/build-policy.json")
PIN = json.loads((ROOT / "config/freebsd-16.json").read_text(encoding="utf-8"))
START = datetime.fromisoformat(PIN["valid_from"].replace("Z", "+00:00"))
END = datetime.fromisoformat(PIN["valid_until"].replace("Z", "+00:00"))
MIDDLE = START + (END - START) / 2
LOCK = json.loads((ROOT / "config/releases/1.1.1.json").read_text(encoding="utf-8"))
HEADS = {"freesense": LOCK["source_sha"], "freesense-system-ports": LOCK["system_ports_sha"],
         "freesense-packages": LOCK["packages_sha"]}


def tree(*locks: dict) -> Path:
    directory = Path(tempfile.mkdtemp())
    (directory / "config/releases").mkdir(parents=True)
    for lock in locks:
        (directory / "config/releases" / f"{lock['release']}.json").write_text(json.dumps(lock), encoding="utf-8")
    return directory


class StableRefreshTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: [shutil.rmtree(d, ignore_errors=True) for d in self.trees])
        self.trees: list[Path] = []

    def root(self, *locks: dict) -> Path:
        directory = tree(*locks)
        self.trees.append(directory)
        return directory

    def test_the_branch_follows_the_train(self):
        self.assertEqual(refresh.maintenance_branch("1.1"), "RELENG_1_1")
        self.assertEqual(refresh.next_version("1.1.9"), "1.1.10")

    def test_the_newest_lock_is_compared_numerically(self):
        locks = [{**LOCK, "release": f"1.1.{n}", "product_version": f"1.1.{n}-RELEASE"} for n in (1, 2, 9, 10)]
        version, _ = refresh.latest_lock(self.root(*locks), "1.1")
        self.assertEqual(version, "1.1.10")

    def test_no_maintenance_branch_means_nothing_to_propose(self):
        result = refresh.propose(self.root(LOCK), POLICY, PIN, {**HEADS, "freesense": None}, MIDDLE)
        self.assertFalse(result["due"])
        self.assertIn("RELENG_1_1", result["reason"])
        self.assertIn("freesense", result["reason"])

    def test_a_quiet_fortnight_proposes_nothing(self):
        result = refresh.propose(self.root(LOCK), POLICY, PIN, HEADS, MIDDLE)
        self.assertEqual(result, {"due": False, "reason": "nothing changed since 1.1.1"})

    def test_a_maintenance_commit_proposes_the_next_patch(self):
        heads = {**HEADS, "freesense": "a" * 40}
        result = refresh.propose(self.root(LOCK), POLICY, PIN, heads, MIDDLE)
        self.assertTrue(result["due"])
        self.assertEqual((result["version"], result["previous"]), ("1.1.2", "1.1.1"))
        self.assertEqual(result["changed"], ["source_sha"])
        lock = result["lock"]
        self.assertEqual((lock["release"], lock["product_version"]), ("1.1.2", "1.1.2-RELEASE"))
        self.assertEqual(lock["source_sha"], "a" * 40)
        self.assertEqual(lock["system_ports_sha"], LOCK["system_ports_sha"])

    def test_a_new_freebsd_pin_proposes_a_refresh_even_without_new_commits(self):
        older = {**LOCK, "freebsd_source_sha": "9" * 40, "freebsd_osversion": 1600001}
        result = refresh.propose(self.root(older), POLICY, PIN, HEADS, MIDDLE)
        self.assertTrue(result["due"])
        self.assertEqual(set(result["changed"]), {"freebsd_source_sha", "freebsd_osversion"})
        self.assertEqual(result["lock"]["freebsd_source_sha"], PIN["freebsd_source"]["commit"])

    def test_a_proposed_lock_is_loadable_and_matches_the_pin_it_names(self):
        result = refresh.propose(self.root(LOCK), POLICY, PIN, {**HEADS, "freesense": "b" * 40}, MIDDLE)
        root = self.root()
        (root / "config/releases/1.1.2.json").write_text(json.dumps(result["lock"]), encoding="utf-8")
        loaded = release_lock.load(root, POLICY, "1.1.2")
        self.assertEqual(release_lock.targets(loaded), ["amd64", "arm64"])
        self.assertEqual({k: loaded[k] for k in ("jail_object", "worker_image_sha256", "worker_tools_sha256")},
                         {k: LOCK[k] for k in ("jail_object", "worker_image_sha256", "worker_tools_sha256")})

    def test_an_inactive_pin_is_refused_rather_than_proposed(self):
        for moment in (START - timedelta(seconds=1), END):
            with self.subTest(moment=moment), self.assertRaises(SystemExit):
                refresh.propose(self.root(LOCK), POLICY, PIN, {**HEADS, "freesense": "c" * 40}, moment)

    def test_the_description_says_it_publishes_nothing_and_links_what_moved(self):
        result = refresh.propose(self.root(LOCK), POLICY, PIN, {**HEADS, "freesense": "d" * 40}, MIDDLE)
        body = refresh.describe(result, "2026-10-28")
        self.assertIn("publishes nothing", body)
        self.assertIn("release=1.1.2", body)
        self.assertIn(f"compare/{LOCK['source_sha']}...{'d' * 40}", body)
        self.assertIn("16.0-CURRENT", body)
        self.assertNotIn("freesense-packages/compare", body)


class StableRefreshWorkflowTests(unittest.TestCase):
    TEXT = (ROOT / ".github/workflows/stable-refresh.yml").read_text(encoding="utf-8")

    def test_it_only_proposes_and_never_publishes_or_deletes(self):
        for forbidden in ("workflow run stable.yml", "gh pr merge", "--delete-branch", "push --force", "push -f",
                          "git push origin --delete", "FREESENSE_PKG_SIGNING_KEY", "secrets."):
            self.assertNotIn(forbidden, self.TEXT, forbidden)

    def test_it_runs_from_main_on_a_schedule_and_by_hand_only(self):
        self.assertIn("schedule:", self.TEXT)
        self.assertIn("workflow_dispatch:", self.TEXT)
        self.assertNotIn("pull_request", self.TEXT)
        self.assertIn("github.ref == 'refs/heads/main'", self.TEXT)

    def test_stale_proposals_close_and_are_not_reopened_unchanged(self):
        self.assertIn("gh pr close", self.TEXT)
        self.assertIn("--state all", self.TEXT)
        self.assertIn('STALE_DAYS: "14"', self.TEXT)

    def test_the_required_check_is_started_on_the_proposal(self):
        self.assertIn('gh workflow run ci.yml --ref "${BRANCH}"', self.TEXT)
        self.assertIn("workflow_dispatch", (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
