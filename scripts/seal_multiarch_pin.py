#!/usr/bin/env python3
"""Mirror a complete verified v4 candidate, then atomically replace its pin file.

All candidate inputs and native-worker evidence must be prepared first. No
partial target can change the active pin. Failed mirrors may leave harmless
unreferenced immutable blobs, which existing retention handles after its grace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from multiarch_pin import ARCHES, rollover, validate

FILES = {"jail_seed": "base.txz", "package_catalog": "packagesite.pkg", "binary_seed": "binary-seed.tar",
         "worker_image": "worker.qcow2", "worker_tools": "worker-tools.tar"}


def verify_candidate(candidate: dict, directory: Path) -> list[tuple[Path, dict]]:
    validate(candidate)
    files = []
    for arch in ARCHES:
        inputs = candidate["targets"][arch]
        evidence = json.loads((directory / arch / "worker-evidence.json").read_text())
        if (evidence.get("schema_version") != "freesense.worker-evidence/v1"
                or evidence.get("architecture") != arch or evidence.get("boot") is not True
                or evidence.get("tools") is not True
                or evidence.get("worker_image_sha256") != inputs["worker_image"]["sha256"]
                or evidence.get("worker_tools_sha256") != inputs["worker_tools"]["sha256"]
                or evidence.get("requirements_sha256") != inputs["binary_seed"]["requirements_sha256"]):
            raise ValueError(f"{arch} worker evidence does not bind the candidate")
        for field, filename in FILES.items():
            path = directory / arch / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"missing regular {arch} {field} file")
            with path.open("rb") as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if sha != inputs[field]["sha256"] or path.stat().st_size != inputs[field]["size"]:
                raise ValueError(f"{arch} {field} content hash/size mismatch")
            files.append((path, inputs[field]))
    return files


def seal(pin_path: Path, candidate: dict, directory: Path, *, mirror, security_rollover: bool = False) -> None:
    previous_bytes = pin_path.read_bytes()
    previous = json.loads(previous_bytes)
    rollover(previous, candidate, security_rollover=security_rollover)
    files = verify_candidate(candidate, directory)
    for path, expected in files:
        report = mirror(path)
        if (report.get("key") != expected["object"] or report.get("sha256") != expected["sha256"]
                or report.get("size") != expected["size"]):
            raise ValueError("immutable mirror returned conflicting identity")
    # Refuse a concurrent local pin edit rather than overwriting its decision.
    if pin_path.read_bytes() != previous_bytes:
        raise ValueError("active pin changed while mirroring candidate")
    temporary = pin_path.with_name(pin_path.name + ".candidate")
    try:
        temporary.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(pin_path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--fsbuild", required=True)
    parser.add_argument("--security-rollover", action="store_true")
    args = parser.parse_args()
    def mirror(path):
        return json.loads(subprocess.check_output([args.fsbuild, "blob", "put", "--file", str(path)], text=True))
    seal(args.pin, json.loads(args.candidate.read_text()), args.directory, mirror=mirror, security_rollover=args.security_rollover)
