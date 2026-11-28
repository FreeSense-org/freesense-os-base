"""Run check jobs side by side: bounded workers, one line per job, full output of failures at the end, all jobs reported."""
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

_local = threading.local()
# One browser job is a Chromium (about 0.5 GB with its renderers), a Python client and often a PHP harness container.
GB_PER_JOB = 0.7
MAX_JOBS = 12


@dataclass
class Job:
    name: str
    action: object  # callable without arguments; raises on failure
    seconds: float = 0.0
    failure: str | None = None
    output: str = ""


def free_memory_gb() -> float | None:
    if os.name == "nt":
        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "free", "totalPage", "freePage", "totalVirtual", "freeVirtual", "extended")]
        status = Status(length=ctypes.sizeof(Status))
        return status.free / 2**30 if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except OSError:
        pass
    return None


def default_jobs(requested: int | None = None, cpus: int | None = None, free_gb: float | None = None) -> int:
    """--jobs, else FREESENSE_CHECK_JOBS, else a quarter of the processors limited by free memory (2 to 12)."""
    value = requested if requested is not None else os.environ.get("FREESENSE_CHECK_JOBS")
    if value not in (None, ""):
        number = int(value)
        if number < 1:
            raise ValueError("the number of check jobs must be 1 or more")
        return number
    cpus = cpus or os.cpu_count() or 2
    free_gb = free_gb if free_gb is not None else free_memory_gb()
    by_memory = MAX_JOBS if free_gb is None else int(free_gb / GB_PER_JOB)
    return max(2, min(MAX_JOBS, cpus // 4, by_memory))


def run(args: list[str], **options) -> None:
    """subprocess.run(check=True); inside a job the output is collected for that job instead of interleaving on the console."""
    buffer = getattr(_local, "buffer", None)
    if buffer is None:
        subprocess.run(args, check=True, **options)
        return
    if "input" not in options:
        options.setdefault("stdin", subprocess.DEVNULL)
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
    buffer.append(result.stdout.decode("utf-8", "replace"))
    result.check_returncode()


def say(line: str) -> None:
    buffer = getattr(_local, "buffer", None)
    if buffer is None:
        print(line, flush=True)
    else:
        buffer.append(line + "\n")


def _execute(job: Job, lock: threading.Lock) -> None:
    _local.buffer = buffer = []
    started = time.monotonic()
    try:
        job.action()
    except subprocess.CalledProcessError as error:
        job.failure = f"exit status {error.returncode}"
    except Exception as error:  # every job reports; one failure must not stop the others
        job.failure = f"{type(error).__name__}: {error}"
    finally:
        job.seconds = time.monotonic() - started
        job.output = "".join(buffer)
        _local.buffer = None
    with lock:
        print(f"{'FAIL' if job.failure else 'ok  '} {job.seconds:6.1f}s  {job.name}", flush=True)


def read_history(path: Path | None) -> dict[str, float]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}


def execute(jobs: list[Job], workers: int, history: Path | None = None) -> list[Job]:
    """Run every job, longest first when an earlier run recorded its time. Returns the failed jobs."""
    known = read_history(history)
    ordered = sorted(jobs, key=lambda job: -known.get(job.name, float("inf")))
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(_execute, job, lock) for job in ordered]:
            future.result()
    if history:
        known.update({job.name: round(job.seconds, 1) for job in jobs if not job.failure})
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(json.dumps(known, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return [job for job in jobs if job.failure]


def summary(jobs: list[Job], wall: float, workers: int, stream=None) -> int:
    """Print the failures in full and one summary. Returns the exit status."""
    stream = stream or sys.stdout
    failed = [job for job in jobs if job.failure]
    for job in failed:
        text = f"\n===== FAILED: {job.name} ({job.failure}) =====\n{job.output.rstrip()}\n"
        encoding = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(encoding, "replace").decode(encoding))
    slowest = ", ".join(f"{job.name} {job.seconds:.0f}s" for job in sorted(jobs, key=lambda j: -j.seconds)[:5])
    stream.write(f"\nSlowest: {slowest}\n")
    stream.write(f"{len(jobs) - len(failed)} of {len(jobs)} jobs passed in {wall:.0f}s with {workers} workers.\n")
    if failed:
        stream.write("Failed: " + ", ".join(job.name for job in failed) + "\n")
    stream.flush()
    return 1 if failed else 0
