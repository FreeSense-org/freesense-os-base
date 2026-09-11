import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("split_pin_candidates", ROOT / "scripts" / "split_pin_candidates.py")
module = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(module)


def candidate(index: int) -> dict:
    return {"commit": f"{index:040x}", "committed_at": f"2026-09-{index + 1:02d}T00:00:00Z"}


class SplitPinCandidatesTests(unittest.TestCase):
    def test_splits_fourteen_candidates_into_twos(self):
        chunks = module.split_candidates([candidate(i) for i in range(14)])
        self.assertEqual([chunk["id"] for chunk in chunks], list(range(7)))
        self.assertEqual([len(chunk["candidates"]) for chunk in chunks], [2] * 7)

    def test_single_candidate_is_one_chunk(self):
        chunks = module.split_candidates([candidate(1)])
        self.assertEqual(chunks, [{"id": 0, "candidates": [candidate(1)]}])

    def test_merge_restores_window_order(self):
        expected = [f"{i:040x}" for i in range(5)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "candidate-requirements-1.json").write_text(json.dumps([
                {"commit": expected[3], "accepted": 1}, {"commit": expected[4], "accepted": 2}]))
            (root / "candidate-requirements-0.json").write_text(json.dumps([
                {"commit": expected[0], "accepted": 9}, {"commit": expected[1], "accepted": 8},
                {"commit": expected[2], "accepted": 7}]))
            merged = module.merge_chunks(sorted(root.glob("candidate-requirements-*.json")), expected)
        self.assertEqual([item["commit"] for item in merged], expected)

    def test_merge_rejects_incomplete_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate-requirements-0.json"
            path.write_text(json.dumps([{"commit": "a" * 40}]))
            with self.assertRaises(ValueError):
                module.merge_chunks([path], ["a" * 40, "b" * 40])


if __name__ == "__main__":
    unittest.main()
