#!/usr/bin/env python3
"""Derive the worker inputs for the mirror stage from the pin and the plan.

The mirror builds nothing, so most of the worker's contract -- image profiles,
channel payloads, shard coordinates -- does not apply to it. Deriving the fields
that do apply from the committed pin and build policy, rather than restating them
in a workflow, keeps the mirror bound to the same FreeBSD snapshot as everything
else and leaves one place to be wrong.

Credentials and the signing key are deliberately not emitted here: the runner
acquires those separately, and this document is written to GITHUB_ENV.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from multiarch_pin import ARCHES, digest

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Supplied by the credential broker and repository secrets, never by this script.
CREDENTIAL_FIELDS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "R2_ENDPOINT", "R2_BUCKET", "FREESENSE_REPO_SIGNING_KEY",
)

# The mirror carries no image, channel or farm identity. Empty is the honest
# value: a placeholder that looked plausible would be harder to spot in a log.
EMPTY_FIELDS = (
    "PLATFORM_ID", "SYSTEM_ID", "SYSTEM_SHA", "PACKAGES_SHA", "PACKAGES_ID",
    "CHANNEL_PAYLOAD_SHA256", "CHANNEL_PAYLOAD_B64", "CHANNEL_SIGNATURE_B64",
    "BUNDLE_ID", "CLOUD_FILESYSTEM", "CLOUD_VIRTUAL_SIZE_GIB", "IMAGE_PROFILE",
    "FIRMWARE", "INSTALLER_FORMAT", "BOOT_INPUTS", "TARGET_MODELS",
    "PARTITION_SCHEME", "APPLIANCE_FILESYSTEM", "APPLIANCE_FORMAT",
    "APPLIANCE_COMPRESSION",
)


def blob_sha(value: object, label: str) -> str:
    if not isinstance(value, dict) or not SHA256.fullmatch(str(value.get("sha256", ""))):
        raise ValueError(f"the pin has no usable {label}")
    return value["sha256"]


def worker_env(pin: dict, policy: dict, plan: dict, *, architecture: str, plan_object: str,
               source_sha: str, os_base_sha: str, generation: int) -> dict[str, str]:
    if architecture not in ARCHES:
        raise ValueError("unsupported architecture")
    if plan.get("schema_version") != "freesense.mirror-plan/v1":
        raise ValueError("invalid mirror plan")
    target = policy.get("targets", {}).get(architecture)
    pinned = pin.get("targets", {}).get(architecture)
    if not isinstance(target, dict) or not isinstance(pinned, dict):
        raise ValueError(f"no {architecture} target in the build policy or pin")
    if plan.get("abi") != target.get("abi") or plan.get("architecture") != architecture:
        raise ValueError("the mirror plan does not describe this target")
    if not plan_object.startswith("inputs/sha256/") or not SHA256.fullmatch(plan_object.rsplit("/", 1)[-1]):
        raise ValueError("the mirror plan must be a pinned content-addressed object")
    for label, value in (("source", source_sha), ("os-base", os_base_sha)):
        if not SHA1.fullmatch(value):
            raise ValueError(f"the {label} revision must be an exact commit")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("invalid generation")

    version = policy["release"]["development_version"]
    fields = {
        "STAGE": "mirror",
        "FINGERPRINT": plan["fingerprint"],
        "MIRROR_PLAN_OBJECT": plan_object,
        "SOURCE_SHA": source_sha,
        "OS_BASE_SHA": os_base_sha,
        "FREEBSD_SHA": pin["freebsd_source"]["commit"],
        "PORTS_SHA": plan["ports_commit"],
        "IMAGE_SHA256": blob_sha(pinned.get("worker_image"), f"{architecture} worker image"),
        "WORKER_TOOLS_SHA256": blob_sha(pinned.get("worker_tools"), f"{architecture} worker tools"),
        "JAIL_OBJECT": pinned["jail_seed"]["object"],
        "FREEBSD_PIN_ID": digest(pin),
        "PACKAGE_TRAIN": policy["package_train"],
        "PRODUCT_VERSION": f"{version}-DEVELOPMENT",
        "GENERATION": str(generation),
        "SYSTEM_GENERATION": str(generation),
        "PUBLIC_BASE_URL": policy["public_base_url"],
        "CHANNEL": "devel",
        "TARGET": architecture,
        "ARCHITECTURE": architecture,
        "PACKAGE_ARCH": target["package_arch"],
        "ABI": target["abi"],
        "ALTABI": target["altabi"],
        "OSVERSION": str(pin["bootstrap_snapshot"]["osversion"]),
        "FREEBSD_TARGET": target["freebsd_target"],
        "FREEBSD_TARGET_ARCH": target["freebsd_target_arch"],
        "POUDRIERE_ARCH": target["poudriere_arch"],
        "KERNEL": target["kernel"],
        "EXECUTOR": target["executor"],
        "IMAGE_CAPABILITIES": "{}",
        "PUBLISH_ENABLED": "true",
        # The mirror is one job with no farm; these keep the worker's shard
        # arithmetic well formed rather than describing anything real.
        "SYSTEM_PART": "full",
        "SYSTEM_SHARD_INDEX": "0",
        "SYSTEM_SHARD_COUNT": "1",
        "FARM_LAYOUT": "legacy",
        "SHARD_POLICY_VERSION": "legacy",
    }
    fields.update({name: "" for name in EMPTY_FIELDS})
    return dict(sorted(fields.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=tuple(ARCHES), required=True)
    parser.add_argument("--pin", type=Path, default=Path("config/freebsd-16.json"))
    parser.add_argument("--policy", type=Path, default=Path("config/build-policy.json"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-object", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--os-base-sha", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True, help="GITHUB_ENV to append to")
    args = parser.parse_args()
    fields = worker_env(
        json.loads(args.pin.read_text(encoding="utf-8")),
        json.loads(args.policy.read_text(encoding="utf-8")),
        json.loads(args.plan.read_text(encoding="utf-8")),
        architecture=args.architecture, plan_object=args.plan_object,
        source_sha=args.source_sha, os_base_sha=args.os_base_sha, generation=args.generation)
    with args.output.open("a", encoding="utf-8") as handle:
        for name, value in fields.items():
            if "\n" in value:
                raise SystemExit(f"worker input {name} is not a single line")
            handle.write(f"{name}={value}\n")
    print(f"prepared {len(fields)} mirror worker inputs for {args.architecture}")


if __name__ == "__main__":
    main()
