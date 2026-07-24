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
    payload_schema = payload.get("schema_version") if isinstance(payload, dict) else ""
    if payload_schema not in {"freesense.channels/v1", "freesense.channels/v2", "freesense.channels/v3"}:
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
    release_version = channel.get("version", "")
    if payload_schema == "freesense.channels/v3" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release_version):
        raise SystemExit("selected channel has an invalid release version")
    abi = channel.get("abi", "")
    abi_match = re.fullmatch(r"FreeBSD:([0-9]+):amd64", abi)
    selected_system = channel.get("system")
    osversion = selected_system.get("osversion", 0) if isinstance(selected_system, dict) else 0
    if osversion != 0 and (
        abi_match is None
        or not isinstance(osversion, int)
        or not int(abi_match.group(1)) * 100000
        <= osversion
        < (int(abi_match.group(1)) + 1) * 100000
    ):
        raise SystemExit("selected channel System has an invalid OSVERSION")
    fingerprint = component.get("fingerprint", "")
    if not SHA256.fullmatch(fingerprint):
        raise SystemExit("selected channel component has an invalid fingerprint")
    generation = component.get("generation")
    if not isinstance(generation, int) or generation <= 0:
        raise SystemExit("selected channel component has an invalid generation")
    verified = component.get("verified")
    if not isinstance(verified, bool):
        raise SystemExit("selected channel component has an invalid verification state")
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
        "verified": str(verified).lower(),
        "package_train": package_train,
        "abi": abi,
        "altabi": channel["altabi"],
        "osversion": osversion,
        "channel": args.channel,
        "release_version": release_version,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payload_base64": envelope["payload"],
        "signature_base64": envelope["signature"],
    }
    declared_pin_id = component.get("freebsd_pin_id", "")
    if payload_schema in {"freesense.channels/v2", "freesense.channels/v3"} and not SHA256.fullmatch(declared_pin_id):
        raise SystemExit("selected channel component has an invalid FreeBSD pin identity")
    if args.component == "packages":
        system_fingerprint = component.get("system_fingerprint", "")
        built_against_system = component.get("built_against_system", system_fingerprint)
        selected_system = channel.get("system", {}).get("fingerprint", "")
        if (not SHA256.fullmatch(system_fingerprint) or system_fingerprint != selected_system
                or not SHA256.fullmatch(built_against_system)):
            raise SystemExit("selected packages component lacks a valid System binding")
        values["system_fingerprint"] = system_fingerprint
        values["built_against_system"] = built_against_system
    else:
        selected_packages = channel.get("packages")
        if selected_packages is None:
            values["packages_fingerprint"] = ""
            values["packages_generation"] = 0
            values["packages_verified"] = "false"
        elif isinstance(selected_packages, dict):
            packages_fingerprint = selected_packages.get("fingerprint", "")
            packages_system = selected_packages.get("system_fingerprint", "")
            packages_built_against = selected_packages.get(
                "built_against_system", packages_system
            )
            packages_generation = selected_packages.get("generation")
            packages_verified = selected_packages.get("verified")
            packages_pin_id = selected_packages.get("freebsd_pin_id", declared_pin_id)
            expected_packages_url = (
                f"https://pkg.freesense.org/v1/artifacts/packages/"
                f"{package_train}/{packages_fingerprint}/amd64"
            )
            if (
                not SHA256.fullmatch(packages_fingerprint)
                or packages_system != fingerprint
                or not SHA256.fullmatch(packages_built_against)
                or not isinstance(packages_generation, int)
                or packages_generation <= 0
                or not isinstance(packages_verified, bool)
                or selected_packages.get("url") != expected_packages_url
                or (payload_schema in {"freesense.channels/v2", "freesense.channels/v3"}
                    and packages_pin_id != declared_pin_id)
            ):
                raise SystemExit("selected channel packages conflict with its System")
            values["packages_fingerprint"] = packages_fingerprint
            values["packages_generation"] = packages_generation
            values["packages_verified"] = str(packages_verified).lower()
            values["packages_freebsd_pin_id"] = packages_pin_id
            values["packages_built_against_system"] = packages_built_against
        else:
            raise SystemExit("selected channel packages are invalid")

    marker_url = url.removesuffix("/amd64") + "/complete.json"
    try:
        marker = json.loads(fetch_bytes(marker_url))
        inputs = marker["inputs"]
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"selected {args.component} completion marker is invalid") from error
    if (
        not isinstance(marker, dict)
        or not isinstance(inputs, dict)
        or marker.get("schema_version") != "freesense.artifact/v1"
        or marker.get("stage") != args.component
        or marker.get("fingerprint") != fingerprint
        or marker.get("generation") != generation
        or inputs.get("package_train") != package_train
    ):
        raise SystemExit(
            f"selected {args.component} completion marker conflicts with its channel entry"
        )

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
    artifact_pin_id = hashlib.sha256(json.dumps({
        "abi": channel["abi"],
        "altabi": channel["altabi"],
        "freebsd_ports": inputs.get("ports", ""),
        "freebsd_source": inputs.get("freebsd", ""),
        "jail_seed": inputs.get("jail_object", "").removeprefix("inputs/sha256/"),
        "kind": "freebsd-pin",
        "schema": 1,
        "worker_image": inputs.get("worker_image", ""),
        "worker_tools": inputs.get("worker_tools", ""),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expected_system = fingerprint
    if args.component == "packages":
        # A package repository remains compatible with later System builds from
        # the same FreeBSD pin. Its channel binding follows the selected System,
        # while the immutable marker records the System used for the build.
        expected_system = values["built_against_system"]
        required_sha["packages"] = inputs.get("packages", "")
    if (
        inputs.get("system") != expected_system
        or (args.component == "packages"
            and inputs.get("built_against_system", inputs.get("system")) != expected_system)
        or any(not SHA.fullmatch(value) for value in required_sha.values())
        or any(not SHA256.fullmatch(value) for value in required_sha256.values())
        or not re.fullmatch(r"inputs/sha256/[0-9a-f]{64}", inputs.get("jail_object", ""))
        or inputs.get("freebsd_pin_id", artifact_pin_id) != artifact_pin_id
        or (declared_pin_id and declared_pin_id != artifact_pin_id)
    ):
        raise SystemExit(
            f"selected {args.component} completion marker conflicts with its channel entry"
        )

    if args.component == "system":
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
            "artifact_freebsd_pin_id": artifact_pin_id,
        })
        if isinstance(selected_packages, dict):
            packages_marker_url = selected_packages["url"].removesuffix("/amd64") + "/complete.json"
            try:
                packages_marker = json.loads(fetch_bytes(packages_marker_url))
                packages_inputs = packages_marker["inputs"]
            except (KeyError, TypeError, ValueError) as error:
                raise SystemExit("selected packages completion marker is invalid") from error
            packages_source = packages_inputs.get("packages", "")
            packages_built_against = values["packages_built_against_system"]
            if (
                not isinstance(packages_marker, dict)
                or not isinstance(packages_inputs, dict)
                or packages_marker.get("schema_version") != "freesense.artifact/v1"
                or packages_marker.get("stage") != "packages"
                or packages_marker.get("fingerprint") != values["packages_fingerprint"]
                or packages_marker.get("generation") != values["packages_generation"]
                or packages_inputs.get("package_train") != package_train
                or packages_inputs.get("system") != packages_built_against
                or packages_inputs.get("built_against_system", packages_inputs.get("system"))
                    != packages_built_against
                or packages_inputs.get("freebsd_pin_id", artifact_pin_id) != artifact_pin_id
                or not SHA.fullmatch(packages_source)
            ):
                raise SystemExit("selected packages completion marker conflicts with its channel entry")
            values["artifact_packages_sha"] = packages_source
    values["freebsd_pin_id"] = artifact_pin_id
    if not args.json_output and not args.github_output:
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
