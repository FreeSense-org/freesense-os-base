#!/usr/bin/env python3
"""Validate a frozen component plan at the reusable-workflow boundary."""
import argparse
import json
import os
from pathlib import Path
import re

from build_platform import load_policy, target
from multiarch_pin import SHA256, validate, worker


def check(plan: dict, stage: str, generation: str, pin: dict, policy: dict) -> dict:
    if stage not in {"system", "packages"} or not re.fullmatch(r"[1-9][0-9]*", generation):
        raise ValueError("farm requires a component and reserved pair generation")
    validate(pin)
    execution = worker(pin, plan["target"], plan["build_host"])
    descriptor = target(policy, plan["target"])
    for field in ("architecture", "package_arch", "abi", "altabi", "freebsd_target",
                  "freebsd_target_arch", "poudriere_arch", "kernel"):
        if plan.get(field) != descriptor[field]:
            raise ValueError(f"farm target descriptor mismatch: {field}")
    expected = {
        "executor": execution["executor"], "image_sha256": execution["worker_image"]["sha256"],
        "worker_tools_sha256": execution["worker_tools"]["sha256"],
        "binary_seed_object": execution["binary_seed"]["object"],
        "binary_seed_provenance_sha256": execution["binary_seed"]["provenance_sha256"],
        "freebsd_sha": pin["freebsd_source"]["commit"], "ports_sha": pin["freebsd_ports"]["commit"],
    }
    if any(plan.get(key) != value for key, value in expected.items()):
        raise ValueError("farm inputs differ from the selected immutable pin/executor")
    previous = plan.get("previous_freesense_repository", "")
    if previous and not SHA256.fullmatch(str(previous)):
        raise ValueError("invalid previous FreeSense repository seed")
    for field in ("system", "platform", "fingerprint", "freebsd_pin_id", stage):
        if not SHA256.fullmatch(str(plan.get(field, ""))):
            raise ValueError(f"invalid component identity: {field}")
    if plan["fingerprint"] != plan[stage] or plan.get("channel") != "devel":
        raise ValueError("farm plan belongs to a different component or channel")
    for field in ("source_sha", "system_sha", "packages_sha", "os_base_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(plan.get(field, ""))):
            raise ValueError(f"invalid source identity: {field}")
    if plan["os_base_sha"] != os.environ.get("GITHUB_SHA", plan["os_base_sha"]):
        raise ValueError("coordinator plan was created with a different workflow revision")
    matrix = [{"part": "shard", "shard": str(index)} for index in range(8)]
    if stage == "system":
        matrix.insert(0, {"part": "core", "shard": "0"})
    return {"fingerprint": plan[stage], "matrix": {"include": matrix}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = check(json.loads(os.environ["COMPONENT_PLAN"]), os.environ["COMPONENT_STAGE"],
                   os.environ["PAIR_GENERATION"], json.loads((root / "config/freebsd-16.json").read_text()), load_policy())
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"fingerprint={result['fingerprint']}\n")
        output.write("matrix=" + json.dumps(result["matrix"], separators=(",", ":")) + "\n")
