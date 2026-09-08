#!/usr/bin/env python3
"""Check observed multiarch job durations and standard-runner concurrency."""
from datetime import datetime

WATCHDOG_SECONDS = 330 * 60
STANDARD_RUNNER_LIMIT = 20


def verify(pages: list[dict], run_id: int) -> dict:
    if not pages or type(run_id) is not int or run_id <= 0:
        raise ValueError("job evidence requires the exact workflow run")
    jobs = [job for page in pages for job in page["jobs"]]
    ids, intervals, durations = set(), [], []
    for job in jobs:
        if job.get("run_id") != run_id or job.get("id") in ids:
            raise ValueError("duplicate job or job from a different run")
        ids.add(job["id"])
        if job.get("name") == "verify_pair" and job.get("status") == "in_progress":
            continue  # This verifier records completed build jobs, not itself.
        if job.get("conclusion") == "skipped":
            continue
        if job.get("status") != "completed" or not job.get("started_at") or not job.get("completed_at"):
            raise ValueError("build job timing evidence is incomplete")
        start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("job timing evidence must include timezones")
        duration = (end - start).total_seconds()
        if duration < 0 or duration >= WATCHDOG_SECONDS:
            raise ValueError(f"job violates the 5.5-hour watchdog: {job['name']}")
        durations.append({"id": job["id"], "name": job["name"], "seconds": duration,
                          "conclusion": job.get("conclusion")})
        if "self-hosted" not in job.get("labels", []) and end > start:
            intervals.extend([(start, 1), (end, -1)])
    if not durations:
        raise ValueError("no completed jobs in canary timing evidence")
    active = peak = 0
    for _, change in sorted(intervals):
        active += change
        peak = max(peak, active)
    # The final verifier itself uses one standard runner after the build jobs.
    peak = max(peak, 1)
    if peak >= STANDARD_RUNNER_LIMIT:
        raise ValueError("multiarch job concurrency must remain below 20 standard runners")
    return {"run_id": run_id, "watchdog_seconds": WATCHDOG_SECONDS,
            "maximum_job_seconds": max(item["seconds"] for item in durations),
            "peak_standard_runners": peak, "jobs": durations}
