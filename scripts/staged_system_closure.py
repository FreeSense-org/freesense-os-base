#!/usr/bin/env python3
"""Convert a verified immutable System marker into a planner closure.

This is used only for build-enabled/publish-disabled targets.  It lets the
optional-package stage consume an exact System result without creating a
public signed channel document prematurely.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re

from build_platform import image_profile, load_policy, pin_target, target as platform_target


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--packages-marker", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    policy = load_policy(ROOT / "config/build-policy.json")
    target = platform_target(policy, args.target)
    profile = image_profile(policy, None, args.target)
    pin = json.loads((ROOT / "config/freebsd-16.json").read_text(encoding="utf-8"))
    selected_pin = pin_target(pin, args.target)
    if target["publish_enabled"]:
        fail("staged System closures are only valid for publish-disabled targets")
    try:
        marker = json.loads(args.marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        fail(f"staged System marker is unreadable: {error}")
    inputs = marker.get("inputs")
    if (
        marker.get("schema_version") != "freesense.artifact/v1"
        or marker.get("stage") != "system"
        or not SHA256.fullmatch(str(marker.get("fingerprint", "")))
        or not isinstance(marker.get("generation"), int)
        or marker["generation"] <= 0
        or marker.get("architecture") != target["architecture"]
        or marker.get("package_arch") != target["package_arch"]
        or marker.get("platform") != profile["name"]
        or not isinstance(inputs, dict)
        or inputs.get("system") != marker.get("fingerprint")
    ):
        fail("staged System marker identity is invalid")

    sha_fields = {
        "source": "artifact_source_sha",
        "system_ports": "artifact_system_sha",
        "os_definition": "artifact_os_base_sha",
        "freebsd": "artifact_freebsd_sha",
        "ports": "artifact_ports_sha",
    }
    digest_fields = {
        "platform": "artifact_platform",
        "worker_image": "artifact_image_sha256",
        "worker_tools": "artifact_worker_tools_sha256",
        "freebsd_pin_id": "artifact_freebsd_pin_id",
        "signing_public_key": "artifact_signing_public_key_sha256",
    }
    closure: dict[str, object] = {
        "fingerprint": marker["fingerprint"],
        "channel": "devel",
        "generation": marker["generation"],
        "package_train": inputs.get("package_train"),
        "release_version": json.loads((ROOT / "config/build-policy.json").read_text(encoding="utf-8"))["release"]["development_version"],
        "verified": "true",
        "packages_verified": "false",
        "packages_fingerprint": "",
        "packages_generation": 0,
        "architecture": target["architecture"],
        "package_arch": target["package_arch"],
        "artifact_packages_sha": "",
        "artifact_jail_object": inputs.get("jail_object"),
        "osversion": pin["freebsd_source"]["osversion"],
    }
    for source, destination in sha_fields.items():
        value = inputs.get(source)
        if not isinstance(value, str) or not SHA.fullmatch(value):
            fail(f"staged System marker has invalid {source}")
        closure[destination] = value
    for source, destination in digest_fields.items():
        value = inputs.get(source)
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            fail(f"staged System marker has invalid {source}")
        closure[destination] = value
    if inputs.get("jail_object") != "inputs/sha256/" + selected_pin["jail_seed"]["sha256"]:
        fail("staged System marker jail seed differs from the current sealed pin")
    expected_key = hashlib.sha256((ROOT / "config/channel-signing-public.pem").read_bytes()).hexdigest()
    if closure["artifact_signing_public_key_sha256"] != expected_key:
        fail("staged System marker uses a different signing trust root")

    if args.packages_marker is not None:
        try:
            packages_marker_bytes = args.packages_marker.read_bytes()
            packages_marker = json.loads(packages_marker_bytes)
        except (OSError, ValueError) as error:
            fail(f"staged Packages marker is unreadable: {error}")
        package_inputs = packages_marker.get("inputs")
        package_fingerprint = packages_marker.get("fingerprint", "")
        if (
            packages_marker.get("schema_version") != "freesense.artifact/v1"
            or packages_marker.get("stage") != "packages"
            or not SHA256.fullmatch(str(package_fingerprint))
            or not isinstance(packages_marker.get("generation"), int)
            or packages_marker["generation"] <= 0
            or packages_marker.get("architecture") != target["architecture"]
            or packages_marker.get("package_arch") != target["package_arch"]
            or packages_marker.get("platform") != profile["name"]
            or not isinstance(package_inputs, dict)
            or package_inputs.get("system") != marker["fingerprint"]
            or package_inputs.get("built_against_system") != marker["fingerprint"]
            or package_inputs.get("freebsd_pin_id") != inputs.get("freebsd_pin_id")
            or package_inputs.get("package_train") != inputs.get("package_train")
            or not SHA.fullmatch(str(package_inputs.get("packages", "")))
        ):
            fail("staged Packages marker closure is invalid")
        staged_payload = {
            "schema_version": "freesense.channels/v3",
            "channels": {"devel": {
                "name": "devel",
                "description": "Experimental staged ARM64 acceptance build",
                "version": closure["release_version"],
                "package_train": inputs["package_train"],
                "abi": target["abi"],
                "altabi": target["altabi"],
                "architecture": target["architecture"],
                "package_arch": target["package_arch"],
                "default": True,
                "system": {
                    "fingerprint": marker["fingerprint"],
                    "url": f"https://pkg.freesense.org/v1/artifacts/system/{marker['fingerprint']}/{target['package_arch']}",
                    "generation": marker["generation"],
                    "published_at": "1970-01-01T00:00:00Z",
                    "verified": True,
                    "freebsd_pin_id": inputs["freebsd_pin_id"],
                    "osversion": pin["freebsd_source"]["osversion"],
                },
                "packages": {
                    "fingerprint": package_fingerprint,
                    "system_fingerprint": marker["fingerprint"],
                    "built_against_system": marker["fingerprint"],
                    "url": f"https://pkg.freesense.org/v1/artifacts/packages/{inputs['package_train']}/{package_fingerprint}/{target['package_arch']}",
                    "generation": packages_marker["generation"],
                    "published_at": "1970-01-01T00:00:00Z",
                    "verified": True,
                    "freebsd_pin_id": inputs["freebsd_pin_id"],
                },
            }},
        }
        staged_payload_bytes = json.dumps(
            staged_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        closure.update({
            "artifact_packages_sha": package_inputs["packages"],
            "packages_fingerprint": package_fingerprint,
            "packages_generation": packages_marker["generation"],
            "packages_verified": "true",
            "payload_sha256": hashlib.sha256(staged_payload_bytes).hexdigest(),
            "payload_base64": base64.b64encode(staged_payload_bytes).decode(),
            "signature_base64": "",
        })

    args.output.write_text(json.dumps(closure, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
