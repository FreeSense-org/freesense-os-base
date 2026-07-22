from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_ports_pin", ROOT / "scripts/select_ports_pin.py"
)
assert SPEC and SPEC.loader
select_ports_pin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(select_ports_pin)


OLD = "0" * 40
CURRENT = "7" * 40


def record(timestamp: str, commit: str | None) -> str:
    annotations = {"build_timestamp": timestamp}
    if commit is not None:
        annotations["ports_top_git_hash"] = commit
    return json.dumps({"name": "fixture", "annotations": annotations})


class PortsPinTest(unittest.TestCase):
    def test_newest_generation_wins_over_dominant_old_packages(self):
        current = record("2026-07-19T02:00:00+0000", CURRENT)
        lines = [current] + [record("2026-07-18T02:00:00+0000", OLD)] * 100
        lines.append(current)
        self.assertEqual(select_ports_pin.select_ports_commit(lines), CURRENT)

    def test_one_commit_may_cover_multiple_newest_packages(self):
        lines = [
            record("2026-07-19T02:00:00+0000", CURRENT),
            record("2026-07-19T02:00:00+0000", CURRENT),
        ]
        self.assertEqual(select_ports_pin.select_ports_commit(lines), CURRENT)

    def test_ambiguous_newest_generation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            select_ports_pin.select_ports_commit(
                [
                    record("2026-07-19T02:00:00+0000", CURRENT),
                    record("2026-07-19T02:00:00+0000", "8" * 40),
                ]
            )

    def test_incomplete_newest_generation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ports commit"):
            select_ports_pin.select_ports_commit(
                [
                    record("2026-07-18T02:00:00+0000", OLD),
                    record("2026-07-19T02:00:00+0000", None),
                ]
            )

    def test_missing_build_timestamp_is_rejected(self):
        line = json.dumps(
            {"name": "fixture", "annotations": {"ports_top_git_hash": CURRENT}}
        )
        with self.assertRaisesRegex(ValueError, "build timestamp"):
            select_ports_pin.select_ports_commit([line])


if __name__ == "__main__":
    unittest.main()
