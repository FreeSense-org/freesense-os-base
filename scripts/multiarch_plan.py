#!/usr/bin/env python3
"""Pure dual-architecture graph and pair identity, before generation reservation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from multiarch_pin import ARCHES, SHA256, digest, select_arm_host, validate, worker


def plan(pin: dict, probe: dict, fingerprints: dict, *, force_dedicated: bool = False) -> dict:
    validate(pin)
    if set(fingerprints) != set(ARCHES):
        raise ValueError("both architecture fingerprints are required")
    for components in fingerprints.values():
        if set(components) != {"system", "packages"} or any(
                not isinstance(value, str) or not SHA256.fullmatch(value) for value in components.values()):
            raise ValueError("each target requires System and independent Optional fingerprints")
    hosts = {"amd64": "github-amd64", "arm64": select_arm_host(probe, pin, force_dedicated=force_dedicated)}
    executors = {arch: worker(pin, arch, hosts[arch]) for arch in ARCHES}
    pin_id = digest(pin)
    pair_id = digest({"schema_version": "freesense.multiarch-pair/v1", "freebsd_pin": pin_id,
                      "fingerprints": fingerprints, "executors": executors})
    system, packages = [], []
    for arch in ARCHES:
        common = {"target": arch, "build_host": hosts[arch], "executor": executors[arch]["executor"]}
        system.append({**common, "part": "core", "shard": 0, "count": 4})
        for shard in range(4):
            system.append({**common, "part": "shard", "shard": shard, "count": 4})
            packages.append({**common, "part": "shard", "shard": shard, "count": 4})
    return {
        "schema_version": "freesense.multiarch-plan/v1", "pair_fingerprint": pair_id,
        "freebsd_pin": pin_id, "fingerprints": fingerprints, "executors": executors,
        "system_matrix": {"include": system}, "packages_matrix": {"include": packages},
        "system_max_parallel": 10, "packages_max_parallel": 8,
        "finalizers": list(ARCHES),
        "release_artifacts": {
            "amd64": ["installer", "cloud-ufs", "cloud-zfs"],
            "arm64": ["installer", "arm64-rpi4b", "arm64-rpi5-d0"],
        },
    }


def shard_roots(roots: list[str], *, heavy: list[str] | None = None, count: int = 4) -> list[list[str]]:
    """Isolate measured heavy roots, then deterministically divide the remainder."""
    ordered = sorted(set(roots))
    heavy = sorted(set(heavy or []))
    if count < 1 or len(heavy) >= count or not set(heavy) <= set(ordered):
        raise ValueError("invalid measured heavyweight roots")
    shards = [[root] for root in heavy] + [[] for _ in range(count - len(heavy))]
    remaining = [root for root in ordered if root not in heavy]
    for index, root in enumerate(remaining):
        shards[len(heavy) + index % (count - len(heavy))].append(root)
    return shards


def planning_closure(system: dict) -> dict:
    """Planning-only closure; never use this synthetic object as build evidence."""
    fields = {
        "source_sha": "source_sha", "system_sha": "system_sha", "os_base_sha": "os_base_sha",
        "freebsd_sha": "freebsd_sha", "ports_sha": "ports_sha", "platform": "platform",
        "image_sha256": "image_sha256", "worker_tools_sha256": "worker_tools_sha256",
        "freebsd_pin_id": "freebsd_pin_id", "signing_public_key_sha256": "signing_public_key_sha256",
        "jail_object": "jail_object",
    }
    return {
        **{f"artifact_{destination}": system[source] for source, destination in fields.items()},
        "fingerprint": system["system"], "channel": "devel", "generation": 0,
        "package_train": system["package_train"], "release_version": system["release_version"],
        "architecture": system["architecture"], "package_arch": system["package_arch"],
        "osversion": system["osversion"],
    }


def resolve(pin: dict, probe: dict, os_base_sha: str, *, force_dedicated: bool = False) -> dict:
    from plan import remote_sha
    validate(pin, now=datetime.now(timezone.utc))
    # Resolve each moving branch exactly once, before either target is planned.
    resolved = {
        "source": remote_sha("FreeSense-org/freesense"),
        "system_ports": remote_sha("FreeSense-org/freesense-system-ports"),
        "packages": remote_sha("FreeSense-org/freesense-packages"),
    }
    arm_host = select_arm_host(probe, pin, force_dedicated=force_dedicated)
    targets, fingerprints = {}, {}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        snapshot = root / "resolved.json"
        snapshot.write_text(json.dumps(resolved), encoding="utf-8")
        for arch in ARCHES:
            host = "github-amd64" if arch == "amd64" else arm_host
            common = [sys.executable, str(Path(__file__).with_name("plan.py")),
                      "--target", arch, "--build-host", host, "--resolved-inputs", str(snapshot),
                      "--os-base-sha", os_base_sha, "--immutable-only"]
            system = json.loads(subprocess.check_output([*common, "system"], text=True))
            closure = root / f"{arch}-closure.json"
            closure.write_text(json.dumps(planning_closure(system)), encoding="utf-8")
            packages = json.loads(subprocess.check_output([*common, "packages", "--system-closure", str(closure)], text=True))
            targets[arch] = {"system": system, "packages": packages}
            fingerprints[arch] = {"system": system["system"], "packages": packages["packages"]}
    result = plan(pin, probe, fingerprints, force_dedicated=force_dedicated)
    result.update(targets=targets, resolved_inputs=resolved, os_base_sha=os_base_sha)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--os-base-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--force-dedicated", action="store_true")
    args = parser.parse_args()
    pin = json.loads((Path(__file__).resolve().parents[1] / "config/freebsd-16.json").read_text())
    result = resolve(pin, json.loads(args.probe.read_text()), args.os_base_sha, force_dedicated=args.force_dedicated)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"pair_fingerprint={result['pair_fingerprint']}\n")
            for arch in ARCHES:
                for component in ("system", "packages"):
                    output.write(f"{arch}_{component}=" + json.dumps(result["targets"][arch][component], separators=(",", ":")) + "\n")
