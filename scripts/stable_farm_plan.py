#!/usr/bin/env python3
"""Plan one sealed Stable release for the delta component farm.

Stable System and Optional Packages are built by the same farm as Development,
from the commits and FreeBSD pin recorded in config/releases/<version>.json.
This coordinator resolves nothing that moves: every source comes from the lock,
and plan.py refuses a lock that differs from the active FreeBSD pin.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lock
from build_platform import load_policy, manifest_name, target
from multiarch_plan import planning_closure, previous_seed
from plan import SHA, current_component_record

ROOT = Path(__file__).resolve().parents[1]
TARGET = "amd64"
BUILD_HOSTS = ("dedicated", "github-amd64")


def run_plan(kind: str, release: str, os_base_sha: str, build_host: str, *extra: str) -> dict:
    command = [
        sys.executable, str(Path(__file__).with_name("plan.py")), kind,
        "--target", TARGET, "--build-host", build_host, "--os-base-sha", os_base_sha,
        "--release-lock", release, "--immutable-only", *extra,
    ]
    return json.loads(subprocess.check_output(command, text=True))


def resolve(release: str, os_base_sha: str, build_host: str) -> dict:
    policy = load_policy(ROOT / "config/build-policy.json")
    lock = release_lock.load(ROOT, policy, release)
    manifest = policy["public_base_url"] + "/" + manifest_name(target(policy, TARGET), legacy=False)
    system = run_plan("system", release, os_base_sha, build_host)
    # A System or Optional build seeds its unchanged packages from the
    # published repository of the same FreeBSD pin, as Development does.
    system["previous_freesense_repository"] = previous_seed(
        current_component_record(manifest, "system"), system)
    with tempfile.TemporaryDirectory() as directory:
        closure = Path(directory, "system.json")
        closure.write_text(json.dumps(planning_closure(system)), encoding="utf-8")
        packages = run_plan("packages", release, os_base_sha, build_host, "--system-closure", str(closure))
    packages["previous_freesense_repository"] = previous_seed(
        current_component_record(manifest, "packages"), packages)
    return {"lock": lock, "system": system, "packages": packages}


def outputs(plans: dict) -> dict:
    lock, system, packages = plans["lock"], plans["system"], plans["packages"]
    return {
        "system": system["system"],
        "packages": packages["packages"],
        "platform": system["platform"],
        "freebsd_pin_id": system["freebsd_pin_id"],
        "source_sha": system["source_sha"],
        "system_sha": system["system_sha"],
        "packages_sha": packages["packages_sha"],
        "os_base_sha": system["os_base_sha"],
        "freebsd_sha": system["freebsd_sha"],
        "ports_sha": system["ports_sha"],
        "image_sha256": system["image_sha256"],
        "worker_tools_sha256": system["worker_tools_sha256"],
        "jail_object": system["jail_object"],
        "package_train": system["package_train"],
        "release_version": lock["release"],
        "product_version": system["product_version"],
        "abi": system["abi"],
        "altabi": system["altabi"],
        "osversion": system["osversion"],
        "public_base_url": system["public_base_url"],
        "build_host": system["build_host"],
        "system_plan": json.dumps(system, sort_keys=True, separators=(",", ":")),
        "packages_plan": json.dumps(packages, sort_keys=True, separators=(",", ":")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--os-base-sha", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--build-host", choices=BUILD_HOSTS, default="dedicated")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if not SHA.fullmatch(args.os_base_sha):
        raise SystemExit("--os-base-sha must be a full commit")
    values = outputs(resolve(args.release, args.os_base_sha, args.build_host))
    summary = {key: value for key, value in values.items() if not key.endswith("_plan")}
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
