"""The job runner reports every failure with its full output, and chooses a safe number of workers."""
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parallel
import snapshot


class Runner(unittest.TestCase):
    def test_every_job_runs_and_failures_keep_their_whole_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            history = Path(temporary) / "seconds.json"

            def child(code):
                return lambda: parallel.run([sys.executable, "-c", code])

            jobs = [parallel.Job("first", child("print('line one\\nline two'); raise SystemExit(3)")),
                    parallel.Job("second", child("print('quiet success')")),
                    parallel.Job("third", lambda: (parallel.say("said"), 1 / 0))]
            with patch("sys.stdout", io.StringIO()) as console:
                failed = parallel.execute(jobs, 3, history)
                status = parallel.summary(jobs, 1.0, 3, console)
            self.assertEqual(["first", "third"], [job.name for job in failed])
            self.assertEqual(1, status)
            report = console.getvalue()
            for expected in ["line one", "line two", "exit status 3", "ZeroDivisionError", "said", "1 of 3 jobs passed",
                             "Failed: first, third"]:
                self.assertIn(expected, report)
            self.assertNotIn("quiet success", report)
            # Only passing jobs record a time for the next run's longest-first order.
            self.assertEqual(["second"], list(parallel.read_history(history)))

    def test_passing_jobs_exit_zero(self):
        jobs = [parallel.Job("only", lambda: None)]
        with patch("sys.stdout", io.StringIO()) as console:
            self.assertEqual([], parallel.execute(jobs, 1))
            self.assertEqual(0, parallel.summary(jobs, 0.1, 1, console))

    def test_worker_count_is_bounded_by_processors_and_memory(self):
        with patch.dict(os.environ, {"FREESENSE_CHECK_JOBS": ""}):
            self.assertEqual(8, parallel.default_jobs(None, cpus=32, free_gb=64))
            self.assertEqual(4, parallel.default_jobs(None, cpus=32, free_gb=3))
            self.assertEqual(2, parallel.default_jobs(None, cpus=32, free_gb=0.5))
            self.assertEqual(2, parallel.default_jobs(None, cpus=4, free_gb=64))
            self.assertEqual(20, parallel.default_jobs(20, cpus=4, free_gb=1))
        with patch.dict(os.environ, {"FREESENSE_CHECK_JOBS": "3"}):
            self.assertEqual(3, parallel.default_jobs(None, cpus=32, free_gb=64))
            self.assertEqual(1, parallel.default_jobs(1))
        with self.assertRaises(ValueError):
            parallel.default_jobs(0)

    def test_snapshot_keeps_the_mount_contract(self):
        import tarfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "part/node_modules").mkdir(parents=True)
            (root / "part/node_modules/skip.js").write_text("")
            (root / "part/run.sh").write_bytes(b"#!/bin/sh\n")
            (root / "other").write_text("")
            with tarfile.open(fileobj=io.BytesIO(snapshot.archive(root, ["part"]))) as bundle:
                self.assertEqual(["part", "part/run.sh"], bundle.getnames())
                self.assertEqual(0o777, bundle.getmember("part/run.sh").mode)


if __name__ == "__main__":
    unittest.main()
