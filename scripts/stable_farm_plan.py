#!/usr/bin/env python3
"""Plan one sealed Stable release for the delta component farm.

Stable System and Optional Packages are built by the same farm as Development,
from the commits and FreeBSD pin recorded in config/releases/<version>.json.
This coordinator resolves nothing that moves: every source comes from the lock,
and plan.py refuses a lock that differs from the active FreeBSD pin.

With --testing it plans the rolling Testing line instead (docs/release-policy.md):
the heads of the train's RELENG maintenance branches, versioned as the next
patch after the newest lock, built as a Development build of that version.

A lock ships amd64 and, when it carries an arm64 block, arm64 as well. The two
architectures are planned independently; amd64 keeps its output names and
arm64's carry an `arm64_` prefix, so a workflow can publish one without the
other.
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
BUILD_HOSTS = ("dedicated", "github-amd64")
# arm64 is Stable-supported only where its worker is the lock's worker: the
# dedicated host and hosted amd64 both run it under qemu-user with the amd64
# worker; a native arm64 host would need a worker of its own in the lock.


def run_plan(kind: str, release: str, os_base_sha: str, build_host: str, architecture: str, *extra: str,
             testing_inputs: Path | None = None) -> dict:
    line = (["--line-version", release, "--resolved-inputs", str(testing_inputs)]
            if testing_inputs else ["--release-lock", release])
    command = [
        sys.executable, str(Path(__file__).with_name("plan.py")), kind,
        "--target", architecture, "--build-host", build_host, "--os-base-sha", os_base_sha,
        *line, "--immutable-only", *extra,
    ]
    return json.loads(subprocess.check_output(command, text=True))


# Seed from the last Stable build first: it shares the pin and is the newest
# repository of the line. Development only qualifies when it was built on the
# same pin (its host can differ, and with it the pin id), and it lags behind.
SEED_CHANNELS = ("stable", "devel")
TESTING_SEED_CHANNELS = ("testing", "stable", "devel")


def stable_seed(manifest: str, component: str, planned: dict, channels=SEED_CHANNELS) -> str:
    for channel in channels:
        seed = previous_seed(current_component_record(manifest, component, channel), planned)
        if seed:
            return seed
    return ""


def resolve_target(policy: dict, release: str, os_base_sha: str, build_host: str, architecture: str,
                   testing_inputs: Path | None = None) -> dict:
    manifest = policy["public_base_url"] + "/" + manifest_name(target(policy, architecture), legacy=False)
    seeds = TESTING_SEED_CHANNELS if testing_inputs else SEED_CHANNELS
    system = run_plan("system", release, os_base_sha, build_host, architecture, testing_inputs=testing_inputs)
    # A System or Optional build seeds its unchanged packages from the
    # published repository of the same FreeBSD pin.
    system["previous_freesense_repository"] = stable_seed(manifest, "system", system, seeds)
    with tempfile.TemporaryDirectory() as directory:
        closure = Path(directory, "system.json")
        closure.write_text(json.dumps(planning_closure(system)), encoding="utf-8")
        packages = run_plan("packages", release, os_base_sha, build_host, architecture,
                            "--system-closure", str(closure), testing_inputs=testing_inputs)
    packages["previous_freesense_repository"] = stable_seed(manifest, "packages", packages, seeds)
    return {"system": system, "packages": packages}


def resolve(release: str, os_base_sha: str, build_host: str, architectures: list[str] | None = None) -> dict:
    policy = load_policy(ROOT / "config/build-policy.json")
    lock = release_lock.load(ROOT, policy, release)
    wanted = architectures or release_lock.targets(lock)
    shipped = release_lock.targets(lock)
    if "amd64" not in wanted or any(name not in shipped for name in wanted):
        raise SystemExit(f"release {release} ships {shipped}; cannot plan {wanted}")
    return {"lock": lock, "targets": {
        name: resolve_target(policy, release, os_base_sha, build_host, name) for name in wanted}}


def resolve_testing(os_base_sha: str, build_host: str, only_amd64: bool = False) -> dict:
    """Plan the Testing line: RELENG heads at the next patch after the newest lock."""
    from stable_refresh import SOURCES, head_commit, latest_lock, maintenance_branch, next_version
    policy = load_policy(ROOT / "config/build-policy.json")
    train = policy["release"]["stable_train"]
    branch = maintenance_branch(train)
    heads = {repository: head_commit(repository, branch) for _, repository in SOURCES}
    missing = [repository for repository, sha in heads.items() if not sha]
    if missing:
        raise SystemExit(f"no {branch} branch in: {', '.join(missing)}")
    previous, _ = latest_lock(ROOT, train)
    version = next_version(previous)
    resolved = {"source": heads["freesense"], "system_ports": heads["freesense-system-ports"],
                "packages": heads["freesense-packages"]}
    wanted = ["amd64"]
    if not only_amd64 and policy["targets"].get("arm64", {}).get("build_enabled"):
        wanted.append("arm64")
    with tempfile.TemporaryDirectory() as directory:
        inputs = Path(directory, "resolved.json")
        inputs.write_text(json.dumps(resolved), encoding="utf-8")
        targets = {name: resolve_target(policy, version, os_base_sha, build_host, name, inputs)
                   for name in wanted}
    return {"lock": {"release": version}, "targets": targets}


def target_outputs(plans: dict, prefix: str) -> dict:
    system, packages = plans["system"], plans["packages"]
    return {
        f"{prefix}system": system["system"],
        f"{prefix}packages": packages["packages"],
        f"{prefix}platform": system["platform"],
        f"{prefix}freebsd_pin_id": system["freebsd_pin_id"],
        f"{prefix}package_arch": system["package_arch"],
        f"{prefix}abi": system["abi"],
        f"{prefix}altabi": system["altabi"],
        f"{prefix}osversion": system["osversion"],
        f"{prefix}build_host": system["build_host"],
        f"{prefix}system_plan": json.dumps(system, sort_keys=True, separators=(",", ":")),
        f"{prefix}packages_plan": json.dumps(packages, sort_keys=True, separators=(",", ":")),
    }


def outputs(plan: dict) -> dict:
    lock, targets = plan["lock"], plan["targets"]
    amd64 = targets["amd64"]["system"]
    values = {
        # shared by every architecture of the release
        "release_version": lock["release"],
        "product_version": amd64["product_version"],
        "package_train": amd64["package_train"],
        "public_base_url": amd64["public_base_url"],
        "source_sha": amd64["source_sha"],
        "system_sha": amd64["system_sha"],
        "packages_sha": targets["amd64"]["packages"]["packages_sha"],
        "os_base_sha": amd64["os_base_sha"],
        "freebsd_sha": amd64["freebsd_sha"],
        "ports_sha": amd64["ports_sha"],
        "image_sha256": amd64["image_sha256"],
        "worker_tools_sha256": amd64["worker_tools_sha256"],
        "jail_object": amd64["jail_object"],
        "has_arm64": "arm64" in targets,
        **target_outputs(targets["amd64"], ""),
    }
    if "arm64" in targets:
        values.update(target_outputs(targets["arm64"], "arm64_"))
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--os-base-sha", required=True)
    parser.add_argument("--release", help="sealed release lock (not with --testing)")
    parser.add_argument("--testing", action="store_true", help="plan the rolling Testing line from RELENG heads")
    parser.add_argument("--build-host", choices=BUILD_HOSTS, default="dedicated")
    parser.add_argument("--only-amd64", action="store_true", help="plan amd64 even if the lock ships arm64")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if not SHA.fullmatch(args.os_base_sha):
        raise SystemExit("--os-base-sha must be a full commit")
    if args.testing == bool(args.release):
        raise SystemExit("plan exactly one of --release VERSION or --testing")
    plan = (resolve_testing(args.os_base_sha, args.build_host, args.only_amd64) if args.testing
            else resolve(args.release, args.os_base_sha, args.build_host,
                         ["amd64"] if args.only_amd64 else None))
    values = outputs(plan)
    summary = {key: value for key, value in values.items() if not key.endswith("_plan")}
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
