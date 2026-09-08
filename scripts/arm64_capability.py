#!/usr/bin/env python3
"""Probe the native ARM worker before any pair generation is reserved."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import tempfile

from multiarch_pin import blob, validate


def inspect_host(image_sha256: str, *, minimum_memory_mib: int = 6144,
                 minimum_disk_gib: int = 70) -> dict:
    try:
        available = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:"))
    except (OSError, StopIteration, ValueError):
        available = 0
    free = shutil.disk_usage(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).free
    return {
        "schema_version": "freesense.arm64-capability/v1", "image_sha256": image_sha256,
        "kvm": platform.machine() == "aarch64" and os.access("/dev/kvm", os.R_OK | os.W_OK),
        "memory": available >= minimum_memory_mib * 1024,
        "disk": free >= minimum_disk_gib * 1024**3,
        "qemu": shutil.which("qemu-system-aarch64") is not None,
        "firmware": all(Path(f"/usr/share/AAVMF/AAVMF_{kind}.fd").is_file() for kind in ("CODE", "VARS")),
        "boot": False,
    }


def run_probe(command: list[str]) -> bool:
    # Give run-vm.sh's TERM trap time to stop its daemonized QEMU and clean the
    # overlay. Killing only the immediate subprocess at timeout leaks the VM.
    with subprocess.Popen(command, start_new_session=True) as process:
        try:
            return process.wait(timeout=900) == 0
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return False


def probe(pin: dict, report: Path, *, candidate: bool = False) -> dict:
    if candidate:
        image_input = blob(pin["targets"]["arm64"].get("worker_image"), "candidate ARM worker")
        if image_input.get("architecture") != "arm64":
            raise ValueError("candidate probe requires a native ARM64 image")
    else:
        validate(pin)
    image = pin["targets"]["arm64"]["worker_image"]["sha256"]
    result = inspect_host(image)
    if all(result[key] for key in ("kvm", "memory", "disk", "qemu", "firmware")):
        with tempfile.TemporaryDirectory(prefix="arm64-probe-", dir=os.environ.get("RUNNER_TEMP")) as directory:
            script = Path(directory) / "probe.sh"
            script.write_text('#!/bin/sh\nset -eu\ntest "$(uname -p)" = aarch64\n')
            command = [
                "bash", str(Path(__file__).parent / "runner/run-vm.sh"),
                "--host-architecture", "arm64", "--image-sha256", image,
                "--script", str(script), "--timeout", "600", "--vcpus", "2",
                "--memory-mib", "4096", "--disk-gib", "20", "--minimum-free-gib", "70",
                "--failure-dir", str(report.resolve().parent / "arm64-probe-failure"),
            ]
            try:
                result["boot"] = run_probe(command)
            except OSError:
                result["boot"] = False
    report.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=Path("config/freebsd-16.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate", action="store_true", help="probe a hashed candidate image before declaring the pin ready")
    args = parser.parse_args()
    probe(json.loads(args.pin.read_text()), args.output, candidate=args.candidate)
