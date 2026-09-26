from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import multiarch_job_timings as timings


def job(identity, *, start=0, duration=60, dedicated=False, attempt=1):
    instant = datetime(2026, 9, 7, tzinfo=timezone.utc) + timedelta(seconds=start)
    return {"id": identity, "run_id": 123, "name": f"build-{identity}", "status": "completed",
            "conclusion": "success", "started_at": instant.isoformat(),
            "completed_at": (instant + timedelta(seconds=duration)).isoformat(),
            "labels": ["self-hosted", "build-runner"] if dedicated else ["ubuntu-24.04-arm"],
            "run_attempt": attempt}


class TimingTests(unittest.TestCase):
    def verify(self, jobs):
        return timings.verify([{"jobs": jobs}], 123)

    def test_counts_concurrent_jobs_without_summing_sequential_phases(self):
        report = self.verify([job(i) for i in range(10)] + [job(i + 10, start=60) for i in range(8)])
        self.assertEqual(report["peak_standard_runners"], 10)
        self.assertEqual(report["maximum_job_seconds"], 60)

    def test_dedicated_runner_is_excluded_from_standard_runner_limit(self):
        report = self.verify([job(i) for i in range(19)] + [job(20, dedicated=True)])
        self.assertEqual(report["peak_standard_runners"], 19)
        with self.assertRaisesRegex(ValueError, "below 20"):
            self.verify([job(i) for i in range(20)])

    def test_watchdog_boundary_and_negative_duration_fail(self):
        for duration in (-1, timings.WATCHDOG_SECONDS, timings.WATCHDOG_SECONDS + 1):
            with self.subTest(duration=duration), self.assertRaisesRegex(ValueError, "watchdog"):
                self.verify([job(1, duration=duration)])

    def test_a_previous_attempt_does_not_condemn_this_one(self):
        """filter=all returns every attempt of the run.

        A re-run is the intended recovery path: completed batch checkpoints
        survive, so a shard the watchdog killed resumes instead of repeating.
        The killed job stays in the evidence at or over the watchdog, and
        judging this attempt by it would fail exactly the re-runs the
        checkpoints exist to make cheap.
        """
        killed = job(1, duration=timings.WATCHDOG_SECONDS + 120, attempt=1)
        killed["conclusion"] = "failure"
        clean = [job(i + 10, attempt=2) for i in range(3)]
        report = timings.verify([{"jobs": [killed] + clean}], 123, 2)
        self.assertEqual(report["run_attempt"], 2)
        self.assertEqual(len(report["jobs"]), 3)
        self.assertEqual(report["maximum_job_seconds"], 60)
        # Unscoped, the previous attempt's casualty still fails the run.
        with self.assertRaisesRegex(ValueError, "watchdog"):
            timings.verify([{"jobs": [killed] + clean}], 123)

    def test_an_attempt_with_no_evidence_fails(self):
        with self.assertRaisesRegex(ValueError, "no job evidence"):
            timings.verify([{"jobs": [job(1, attempt=1)]}], 123, 3)
        with self.assertRaisesRegex(ValueError, "valid run attempt"):
            timings.verify([{"jobs": [job(1)]}], 123, 0)

    def test_wrong_run_duplicate_incomplete_and_empty_evidence_fail(self):
        wrong = job(1)
        wrong["run_id"] = 456
        incomplete = job(1)
        incomplete["status"] = "in_progress"
        for jobs in ([wrong], [job(1), job(1)], [incomplete], []):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                self.verify(jobs)

    def test_skipped_jobs_and_current_verifier_do_not_invent_durations(self):
        report = self.verify([job(1), {"id": 2, "run_id": 123, "conclusion": "skipped"},
                              {"id": 3, "run_id": 123, "name": "verify_pair", "status": "in_progress"}])
        self.assertEqual(len(report["jobs"]), 1)
