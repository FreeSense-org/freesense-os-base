#!/usr/bin/env python3
"""Resolve the one immutable FreeSense 1.0 release from its checked lock."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from plan import fingerprint, recipe_digest


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--os-base-sha", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    lock = json.loads((ROOT / "config/releases/1.0.json").read_text(encoding="utf-8"))
    policy = json.loads((ROOT / "config/build-policy.json").read_text(encoding="utf-8"))
    if lock.get("schema_version") != "freesense.release-lock/v1" or not lock.get("sealed"):
        raise SystemExit("1.0 release lock is not sealed")
    for key in ("source_sha", "system_ports_sha", "packages_sha", "freebsd_source_sha", "freebsd_ports_sha"):
        if not SHA.fullmatch(lock.get(key, "")):
            raise SystemExit(f"invalid release input {key}")
    for key in ("worker_image_sha256", "worker_tools_sha256"):
        if not SHA256.fullmatch(lock.get(key, "")):
            raise SystemExit(f"invalid release input {key}")
    if not SHA.fullmatch(args.os_base_sha):
        raise SystemExit("--os-base-sha must be a full commit")
    signing_key = hashlib.sha256((ROOT / "config/channel-signing-public.pem").read_bytes()).hexdigest()
    jail_sha = lock["jail_object"].removeprefix("inputs/sha256/")
    pin_id = fingerprint({
        "schema": 1, "kind": "freebsd-pin", "freebsd_source": lock["freebsd_source_sha"],
        "freebsd_ports": lock["freebsd_ports_sha"], "jail_seed": jail_sha,
        "worker_image": lock["worker_image_sha256"], "worker_tools": lock["worker_tools_sha256"],
        "abi": policy["abi"], "altabi": policy["altabi"],
    })
    runner_recipe = recipe_digest([ROOT / "scripts/runner/run-vm.sh"])
    platform = fingerprint({
        "schema": 3, "kind": "platform", "release": lock["release"],
        "product_version": lock["product_version"], "freebsd_pin": pin_id,
        "source": lock["source_sha"], "system_ports": lock["system_ports_sha"],
        "package_train": lock["package_train"], "runner_policy": policy["runner"],
        "runner_recipe": runner_recipe, "signing_public_key": signing_key,
        "recipe": recipe_digest([ROOT / "scripts/runner/install-worker-tools.sh",
            ROOT / "scripts/runner/worker-common.sh", ROOT / "scripts/runner/stages/system.sh",
            ROOT / "apply.sh", ROOT / "manifest.env", *sorted((ROOT / "patches").glob("*.patch"))]),
    })
    system = fingerprint({
        "schema": 3, "kind": "system", "platform": platform, "release": lock["release"],
        "product_version": lock["product_version"], "source": lock["source_sha"],
        "system_ports": lock["system_ports_sha"], "package_train": lock["package_train"],
        "recipe": recipe_digest([ROOT / "scripts/render-worker.py", ROOT / "scripts/runner/install-worker-tools.sh",
            ROOT / "scripts/runner/worker-common.sh", ROOT / "scripts/runner/stages/system.sh"]),
    })
    packages = fingerprint({
        "schema": 3, "kind": "packages", "freebsd_pin": pin_id,
        "packages": lock["packages_sha"], "package_train": lock["package_train"],
        "signing_public_key": signing_key,
        "recipe": recipe_digest([ROOT / "scripts/render-worker.py", ROOT / "scripts/runner/install-worker-tools.sh",
            ROOT / "scripts/runner/worker-common.sh", ROOT / "scripts/runner/stages/packages.sh"]),
    })
    values = {
        "platform": platform, "system": system, "packages": packages, "freebsd_pin_id": pin_id,
        "source_sha": lock["source_sha"], "system_sha": lock["system_ports_sha"],
        "packages_sha": lock["packages_sha"], "os_base_sha": args.os_base_sha,
        "freebsd_sha": lock["freebsd_source_sha"], "ports_sha": lock["freebsd_ports_sha"],
        "image_sha256": lock["worker_image_sha256"], "worker_tools_sha256": lock["worker_tools_sha256"],
        "jail_object": lock["jail_object"], "package_train": lock["package_train"],
        "product_version": lock["product_version"], "abi": policy["abi"], "altabi": policy["altabi"],
        "public_base_url": policy["public_base_url"],
    }
    print(json.dumps(values, indent=2, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
