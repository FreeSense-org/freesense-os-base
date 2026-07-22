#!/usr/bin/env python3
"""Verify and read the compact signed FreeSense channel document."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request


USER_AGENT = "FreeSense-build/1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://pkg.freesense.org/v1/repos.manifest.json")
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--channel", choices=("devel", "stable"), required=True)
    parser.add_argument("--component", choices=("system", "packages"), required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    envelope_bytes = fetch_bytes(args.url)
    envelope = json.loads(envelope_bytes)
    if not isinstance(envelope, dict) or envelope.get("schema_version") != "freesense.repositories/v3":
        raise SystemExit("unsupported channel envelope")
    try:
        payload_bytes = base64.b64decode(envelope["payload"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (KeyError, ValueError) as error:
        raise SystemExit("invalid channel envelope encoding") from error

    with tempfile.TemporaryDirectory() as directory:
        payload_file = Path(directory, "payload.json")
        signature_file = Path(directory, "signature.bin")
        payload_file.write_bytes(payload_bytes)
        signature_file.write_bytes(signature)
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(args.public_key),
             "-signature", str(signature_file), str(payload_file)],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    payload = json.loads(payload_bytes)
    if not isinstance(payload, dict) or payload.get("schema_version") != "freesense.channels/v1":
        raise SystemExit("unsupported signed channel payload")
    try:
        channel = payload["channels"][args.channel]
        component = channel[args.component]
    except (KeyError, TypeError) as error:
        raise SystemExit(f"{args.channel} has no {args.component} component") from error
    if not isinstance(channel, dict) or not isinstance(component, dict):
        raise SystemExit(f"{args.channel} has no valid {args.component} component")
    package_train = channel.get("package_train", "")
    if not isinstance(package_train, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", package_train):
        raise SystemExit("selected channel has an invalid package train")
    fingerprint = component.get("fingerprint", "")
    if not SHA256.fullmatch(fingerprint):
        raise SystemExit("selected channel component has an invalid fingerprint")
    generation = component.get("generation")
    if not isinstance(generation, int) or generation <= 0:
        raise SystemExit("selected channel component has an invalid generation")
    url = component.get("url", "")
    expected_url = f"https://pkg.freesense.org/v1/artifacts/system/{fingerprint}/amd64"
    if args.component == "packages":
        expected_url = f"https://pkg.freesense.org/v1/artifacts/packages/{package_train}/{fingerprint}/amd64"
    if url != expected_url:
        raise SystemExit("selected channel component has a non-canonical artifact URL")
    values = {
        "fingerprint": fingerprint,
        "url": url,
        "generation": generation,
        "published_at": component["published_at"],
        "verified": str(bool(component.get("verified"))).lower(),
        "package_train": package_train,
        "abi": channel["abi"],
        "altabi": channel["altabi"],
        "channel": args.channel,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payload_base64": envelope["payload"],
        "signature_base64": envelope["signature"],
    }
    if args.component == "packages":
        system_fingerprint = component.get("system_fingerprint", "")
        selected_system = channel.get("system", {}).get("fingerprint", "")
        if not SHA256.fullmatch(system_fingerprint) or system_fingerprint != selected_system:
            raise SystemExit("selected packages component lacks a valid System binding")
        values["system_fingerprint"] = system_fingerprint
    else:
        selected_packages = channel.get("packages")
        if selected_packages is None:
            values["packages_fingerprint"] = ""
        elif isinstance(selected_packages, dict):
            packages_fingerprint = selected_packages.get("fingerprint", "")
            packages_system = selected_packages.get("system_fingerprint", "")
            expected_packages_url = (
                f"https://pkg.freesense.org/v1/artifacts/packages/"
                f"{package_train}/{packages_fingerprint}/amd64"
            )
            if (
                not SHA256.fullmatch(packages_fingerprint)
                or packages_system != fingerprint
                or selected_packages.get("url") != expected_packages_url
            ):
                raise SystemExit("selected channel packages conflict with its System")
            values["packages_fingerprint"] = packages_fingerprint
        else:
            raise SystemExit("selected channel packages are invalid")
        marker_url = url.removesuffix("/amd64") + "/complete.json"
        try:
            marker = json.loads(fetch_bytes(marker_url))
            inputs = marker["inputs"]
        except (KeyError, TypeError, ValueError) as error:
            raise SystemExit("selected System completion marker is invalid") from error
        required_sha = {
            "source": inputs.get("source", ""),
            "system_ports": inputs.get("system_ports", ""),
            "freebsd": inputs.get("freebsd", ""),
            "ports": inputs.get("ports", ""),
            "os_definition": inputs.get("os_definition", ""),
        }
        required_sha256 = {
            "platform": inputs.get("platform", ""),
            "worker_image": inputs.get("worker_image", ""),
            "worker_tools": inputs.get("worker_tools", ""),
            "signing_public_key": inputs.get("signing_public_key", ""),
        }
        if (
            marker.get("schema_version") != "freesense.artifact/v1"
            or marker.get("stage") != "system"
            or marker.get("fingerprint") != fingerprint
            or marker.get("generation") != generation
            or inputs.get("system") != fingerprint
            or inputs.get("package_train") != channel.get("package_train")
            or any(not SHA.fullmatch(value) for value in required_sha.values())
            or any(not SHA256.fullmatch(value) for value in required_sha256.values())
            or not re.fullmatch(r"inputs/sha256/[0-9a-f]{64}", inputs.get("jail_object", ""))
        ):
            raise SystemExit("selected System completion marker conflicts with its channel entry")
        values.update({
            "artifact_platform": required_sha256["platform"],
            "artifact_source_sha": required_sha["source"],
            "artifact_system_sha": required_sha["system_ports"],
            "artifact_freebsd_sha": required_sha["freebsd"],
            "artifact_ports_sha": required_sha["ports"],
            "artifact_os_base_sha": required_sha["os_definition"],
            "artifact_image_sha256": required_sha256["worker_image"],
            "artifact_worker_tools_sha256": required_sha256["worker_tools"],
            "artifact_jail_object": inputs["jail_object"],
            "artifact_signing_public_key_sha256": required_sha256["signing_public_key"],
        })
    print(json.dumps(values, indent=2, sort_keys=True))
    if args.json_output:
        args.json_output.write_text(json.dumps(values, sort_keys=True) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
