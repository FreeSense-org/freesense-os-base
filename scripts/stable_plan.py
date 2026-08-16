#!/usr/bin/env python3
"""Resolve one immutable Stable-train release from its checked lock."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from plan import (
    OPTIONAL_PACKAGE_CONFIG_PATHS,
    fingerprint,
    recipe_digest,
    remote_recipe_digest,
)


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELEASE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--os-base-sha", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    policy = json.loads((ROOT / "config/build-policy.json").read_text(encoding="utf-8"))
    # Stable remains amd64-only until a separate promotion change.  Resolve it
    # through the same descriptor as Development so the v3 policy schema does
    # not create a second architecture mapping.
    target = policy.get("targets", {}).get("amd64")
    if target is None:
        # Read compatibility for sealed-lock tests and historical Stable
        # policy snapshots. Stable is intentionally amd64-only.
        target = {"abi": policy.get("abi"), "altabi": policy.get("altabi")}
    if target.get("abi") != "FreeBSD:16:amd64" or target.get("altabi") != "freebsd:16:x86:64":
        raise SystemExit("Stable policy has an invalid amd64 descriptor")
    release_match = RELEASE.fullmatch(args.release)
    stable_train = policy.get("release", {}).get("stable_train")
    if release_match is None or f"{release_match.group(1)}.{release_match.group(2)}" != stable_train:
        raise SystemExit(f"--release must be an exact {stable_train}.x version")
    lock = json.loads((ROOT / "config/releases" / f"{args.release}.json").read_text(encoding="utf-8"))
    if lock.get("schema_version") != "freesense.release-lock/v1" or not lock.get("sealed"):
        raise SystemExit("release lock is not sealed")
    if lock.get("release") != args.release or lock.get("product_version") != f"{args.release}-RELEASE":
        raise SystemExit("release lock version does not match the requested release")
    if lock.get("package_train") != stable_train:
        raise SystemExit("release lock package train does not match Stable policy")
    for key in ("source_sha", "system_ports_sha", "packages_sha", "freebsd_source_sha", "freebsd_ports_sha"):
        if not SHA.fullmatch(lock.get(key, "")):
            raise SystemExit(f"invalid release input {key}")
    for key in ("worker_image_sha256", "worker_tools_sha256"):
        if not SHA256.fullmatch(lock.get(key, "")):
            raise SystemExit(f"invalid release input {key}")
    abi_match = re.fullmatch(r"FreeBSD:([0-9]+):amd64", target["abi"])
    osversion = lock.get("freebsd_osversion")
    if (
        abi_match is None
        or not isinstance(osversion, int)
        or not int(abi_match.group(1)) * 100000
        <= osversion
        < (int(abi_match.group(1)) + 1) * 100000
    ):
        raise SystemExit("release lock has no exact OSVERSION matching its ABI")
    if not SHA.fullmatch(args.os_base_sha):
        raise SystemExit("--os-base-sha must be a full commit")
    signing_key = hashlib.sha256((ROOT / "config/channel-signing-public.pem").read_bytes()).hexdigest()
    jail_sha = lock["jail_object"].removeprefix("inputs/sha256/")
    pin_id = fingerprint({
        "schema": 1, "kind": "freebsd-pin", "freebsd_source": lock["freebsd_source_sha"],
        "freebsd_ports": lock["freebsd_ports_sha"], "jail_seed": jail_sha,
        "worker_image": lock["worker_image_sha256"], "worker_tools": lock["worker_tools_sha256"],
        "abi": target["abi"], "altabi": target["altabi"],
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
    package_build_config = remote_recipe_digest(
        "FreeSense-org/freesense",
        lock["source_sha"],
        OPTIONAL_PACKAGE_CONFIG_PATHS,
    )
    packages = fingerprint({
        "schema": 4, "kind": "packages", "freebsd_pin": pin_id,
        "packages": lock["packages_sha"], "package_train": lock["package_train"],
        "package_build_config": package_build_config,
        "signing_public_key": signing_key,
        "recipe": recipe_digest([ROOT / "scripts/render-worker.py", ROOT / "scripts/runner/install-worker-tools.sh",
            ROOT / "scripts/runner/worker-common.sh", ROOT / "scripts/runner/stages/packages.sh"]),
    })
    values = {
        "platform": platform, "system": system, "packages": packages, "freebsd_pin_id": pin_id,
        "source_sha": lock["source_sha"], "system_sha": lock["system_ports_sha"],
        "packages_sha": lock["packages_sha"], "os_base_sha": args.os_base_sha,
        "package_build_config_sha256": package_build_config,
        "freebsd_sha": lock["freebsd_source_sha"], "ports_sha": lock["freebsd_ports_sha"],
        "image_sha256": lock["worker_image_sha256"], "worker_tools_sha256": lock["worker_tools_sha256"],
        "jail_object": lock["jail_object"], "package_train": lock["package_train"],
        "release_version": lock["release"],
        "product_version": lock["product_version"], "abi": target["abi"], "altabi": target["altabi"],
        "osversion": osversion,
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
