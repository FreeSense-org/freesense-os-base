import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mirror_report

OBSERVED = "e26b1e4bc8ec94e73586539808ef21695232e8aa"
NEWEST = "5052095ee6f48f63c4fd38dd9ffb2632513036d3"


def candidate(commit, name="alpha"):
    return {"commit": commit, "committed_at": "2026-09-12T01:57:18Z",
            "requirements": {"marker": name}}


class SelectionTests(unittest.TestCase):
    def test_evaluating_the_observed_commit_is_exact(self):
        chosen, selection = mirror_report.select(
            [candidate(NEWEST), candidate(OBSERVED, "upstream")], OBSERVED)
        self.assertEqual(selection, "observed")
        self.assertEqual(chosen["requirements"], {"marker": "upstream"})

    def test_a_window_that_missed_the_observed_commit_is_only_an_estimate(self):
        # Upstream builds from whatever commit its run started at. Sampling one
        # ports commit per day almost never lands on it, which is why the pin
        # has to check out the observed commit instead of choosing from a window.
        chosen, selection = mirror_report.select([candidate(NEWEST)], OBSERVED)
        self.assertEqual(selection, "estimated")
        self.assertEqual(chosen["commit"], NEWEST)

    def test_no_candidates_at_all_is_an_error_not_an_estimate(self):
        with self.assertRaisesRegex(ValueError, "no ports candidates"):
            mirror_report.select([], OBSERVED)


if __name__ == "__main__":
    unittest.main()
