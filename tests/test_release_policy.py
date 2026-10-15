from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
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


check = load_script("freesense_check_release_locks", "scripts/check_release_locks.py")
gate = load_script("freesense_stable_gate", "scripts/stable_gate.py")
POLICY = json.loads((ROOT / "config/build-policy.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def lock(release: str, **changes) -> dict:
    document = {
        "schema_version": "freesense.release-lock/v1", "release": release, "product_version": f"{release}-RELEASE",
        "package_train": release.rsplit(".", 1)[0], "sealed": True,
        "source_sha": "1" * 40, "system_ports_sha": "2" * 40, "packages_sha": "3" * 40,
        "freebsd_source_sha": "4" * 40, "freebsd_ports_sha": "5" * 40, "freebsd_osversion": 1600027,
        "jail_object": "inputs/sha256/" + "6" * 64,
        "worker_image_sha256": "7" * 64, "worker_tools_sha256": "8" * 64,
    }
    document.update(changes)
    return document


def withdrawn(*entries: dict) -> dict:
    return {"schema_version": "freesense.withdrawn-releases/v1", "withdrawn": list(entries)}


ENTRY = {"release": "1.1.1", "date": "2026-10-14", "reason": "cancelled", "superseded_by": "1.1.2"}


class Tree:
    def __init__(self, directory: str):
        self.root = Path(directory)
        (self.root / "config/releases").mkdir(parents=True)

    def write(self, name: str, document: dict) -> None:
        (self.root / "config/releases" / name).write_text(json.dumps(document), encoding="utf-8")


class RealLocksTests(unittest.TestCase):
    def test_the_checked_in_locks_follow_the_policy(self):
        self.assertEqual(check.check_set(ROOT, POLICY), [])

    def test_the_soak_time_is_one_day(self):
        self.assertEqual(POLICY["stable_soak_hours"], 24)


class LockSetTests(unittest.TestCase):
    def errors(self, files: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            tree = Tree(directory)
            for name, document in files.items():
                tree.write(name, document)
            return check.check_set(tree.root, POLICY)

    def test_contiguous_locks_pass(self):
        self.assertEqual(self.errors({"1.1.0.json": lock("1.1.0"), "1.1.1.json": lock("1.1.1")}), [])

    def test_a_gap_in_patch_numbers_fails(self):
        errors = self.errors({"1.1.0.json": lock("1.1.0"), "1.1.2.json": lock("1.1.2")})
        self.assertTrue(any("without gaps" in error for error in errors))

    def test_an_invalid_current_train_lock_fails(self):
        errors = self.errors({"1.1.0.json": lock("1.1.0", source_sha="main")})
        self.assertTrue(any("source_sha" in error for error in errors))

    def test_a_lock_named_after_another_release_fails(self):
        self.assertNotEqual(self.errors({"1.1.0.json": lock("1.1.1")}), [])

    def test_withdrawn_entries_must_name_existing_locks(self):
        files = {"1.1.0.json": lock("1.1.0"), "1.1.1.json": lock("1.1.1"), "1.1.2.json": lock("1.1.2")}
        self.assertEqual(self.errors({**files, "withdrawn.json": withdrawn(ENTRY)}), [])
        missing = {**ENTRY, "release": "1.1.7"}
        self.assertTrue(any("has no lock" in error for error in self.errors({**files, "withdrawn.json": withdrawn(missing)})))

    def test_a_malformed_withdrawn_entry_fails(self):
        files = {"1.1.0.json": lock("1.1.0"), "1.1.1.json": lock("1.1.1"),
                 "withdrawn.json": withdrawn({"release": "1.1.1", "reason": "x"})}
        self.assertTrue(any("withdrawn.json" in error for error in self.errors(files)))


class LockChangeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tree = Tree(self.directory.name)
        self.tree.write("1.1.0.json", lock("1.1.0"))
        self.tree.write("1.1.1.json", lock("1.1.1"))
        self.tree.write("1.1.2.json", lock("1.1.2"))
        self.tree.write("withdrawn.json", withdrawn(ENTRY))
        for command in (["init", "-q"], ["add", "."],
                        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base"]):
            subprocess.run(["git", *command], cwd=self.tree.root, check=True)

    def tearDown(self):
        self.directory.cleanup()

    def test_adding_a_lock_passes(self):
        self.tree.write("1.1.3.json", lock("1.1.3"))
        self.assertEqual(check.check_change(self.tree.root, "HEAD"), [])

    def test_editing_a_merged_lock_fails(self):
        self.tree.write("1.1.1.json", lock("1.1.1", source_sha="9" * 40))
        self.assertTrue(any("append-only" in error for error in check.check_change(self.tree.root, "HEAD")))

    def test_deleting_a_merged_lock_fails(self):
        (self.tree.root / "config/releases/1.1.2.json").unlink()
        self.assertTrue(any("deletes" in error for error in check.check_change(self.tree.root, "HEAD")))

    def test_withdrawn_entries_are_append_only(self):
        added = {**ENTRY, "release": "1.1.2", "superseded_by": None}
        self.tree.write("withdrawn.json", withdrawn(ENTRY, added))
        self.assertEqual(check.check_change(self.tree.root, "HEAD"), [])
        self.tree.write("withdrawn.json", withdrawn(added))
        self.assertTrue(any("append-only" in error for error in check.check_change(self.tree.root, "HEAD")))


class SoakTests(unittest.TestCase):
    def live(self, version: str, hours_ago: float) -> dict:
        published = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"version": version, "system": {"published_at": published}}

    def test_a_seal_inside_the_soak_time_fails(self):
        self.assertTrue(gate.check_soak(lock("1.1.6"), self.live("1.1.5", 3), 24, NOW))

    def test_a_seal_after_the_soak_time_passes(self):
        self.assertEqual(gate.check_soak(lock("1.1.6"), self.live("1.1.5", 30), 24, NOW), [])

    def test_an_override_reason_skips_the_soak_time(self):
        self.assertEqual(gate.check_soak(lock("1.1.6", soak_override="security fix"), self.live("1.1.5", 3), 24, NOW), [])

    def test_resealing_the_live_release_is_not_a_new_seal(self):
        self.assertEqual(gate.check_soak(lock("1.1.5"), self.live("1.1.5", 1), 24, NOW), [])


if __name__ == "__main__":
    unittest.main()
