#!/usr/bin/env python3
"""Resolve immutable inputs and decide whether one build-runner job is required."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")


def remote_sha(repository: str, branch: str = "main") -> str:
    output = subprocess.check_output(
        ["git", "ls-remote", f"https://github.com/{repository}.git", f"refs/heads/{branch}"],
        text=True,
    ).strip()
    value = output.split()[0] if output else ""
    if not SHA.fullmatch(value):
        raise SystemExit(f"could not resolve {repository}@{branch}")
    return value


def recipe_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(ROOT).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def fingerprint(value: dict[str, object]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def current_component(manifest_url: str, component: str) -> str:
    try:
        with urllib.request.urlopen(manifest_url, timeout=15) as response:
            envelope = json.load(response)
        if envelope.get("schema_version") != "freesense.repositories/v3":
            return ""
        payload_bytes = base64.b64decode(envelope["payload"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
        with tempfile.TemporaryDirectory() as directory:
            payload_file = Path(directory, "payload")
            signature_file = Path(directory, "signature")
            payload_file.write_bytes(payload_bytes)
            signature_file.write_bytes(signature)
            subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", str(ROOT / "config/channel-signing-public.pem"),
                 "-signature", str(signature_file), str(payload_file)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        payload = json.loads(payload_bytes)
        if payload.get("schema_version") != "freesense.channels/v1":
            return ""
        return payload.get("channels", {}).get("devel", {}).get(component, {}).get("fingerprint", "")
    except (OSError, ValueError, KeyError, urllib.error.URLError, subprocess.SubprocessError):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("system", "packages", "iso"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--os-base-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--system-id", default="", help="exact system artifact used by ISO")
    args = parser.parse_args()

    if not SHA.fullmatch(args.os_base_sha):
        raise SystemExit("--os-base-sha must be a full Git commit")
    lock = json.loads((ROOT / "config/freebsd-16.json").read_text())
    policy = json.loads((ROOT / "config/build-policy.json").read_text())
    if lock.get("schema_version") != "freesense.freebsd-pin/v2" or not lock.get("ready"):
        raise SystemExit("FreeBSD lock is not ready")

    source_sha = remote_sha("FreeSense-org/freesense")
    system_sha = remote_sha("FreeSense-org/freesense-system-ports")
    packages_sha = remote_sha("FreeSense-org/freesense-packages")
    patch_files = [ROOT / "apply.sh", ROOT / "manifest.env", *sorted((ROOT / "patches").glob("*.patch"))]
    platform_recipe = recipe_digest([
        ROOT / "scripts/runner/worker-common.sh",
        ROOT / "scripts/runner/stages/system.sh",
        *patch_files,
    ])
    platform = fingerprint({
        "schema": 1,
        "kind": "platform",
        "freebsd_source": lock["freebsd_source"]["commit"],
        "freebsd_ports": lock["freebsd_ports"]["commit"],
        "jail_seed": lock["jail_seed"]["sha256"],
        "worker_image": lock["worker_image"]["sha256"],
        "source": source_sha,
        "system_ports": system_sha,
        "package_train": policy["package_train"],
        "recipe": platform_recipe,
    })
    system = fingerprint({
        "schema": 1,
        "kind": "system",
        "platform": platform,
        "source": source_sha,
        "system_ports": system_sha,
        "package_train": policy["package_train"],
        "recipe": recipe_digest([
            ROOT / "scripts/render-worker.py",
            ROOT / "scripts/runner/worker-common.sh",
            ROOT / "scripts/runner/stages/system.sh",
        ]),
    })
    packages = fingerprint({
        "schema": 1,
        "kind": "packages",
        "platform": platform,
        "system": system,
        "source": source_sha,
        "system_ports": system_sha,
        "packages": packages_sha,
        "package_train": policy["package_train"],
        "recipe": recipe_digest([
            ROOT / "scripts/render-worker.py",
            ROOT / "scripts/runner/worker-common.sh",
            ROOT / "scripts/runner/stages/packages.sh",
        ]),
    })
    iso_system = args.system_id or system
    if args.kind == "iso" and not re.fullmatch(r"[0-9a-f]{64}", iso_system):
        raise SystemExit("--system-id must be a SHA-256 fingerprint")
    iso = fingerprint({
        "schema": 1,
        "kind": "iso",
        "system": iso_system,
        "source": source_sha,
        "package_train": policy["package_train"],
        "os_recipe": platform_recipe,
        "recipe": recipe_digest([ROOT / "scripts/render-worker.py", ROOT / "scripts/runner/worker-common.sh", ROOT / "scripts/runner/stages/iso.sh"]),
    })
    identifiers = {"platform": platform, "system": iso_system if args.kind == "iso" else system, "packages": packages, "iso": iso}
    selected = identifiers[args.kind]
    current = "" if args.kind == "iso" else current_component(policy["public_base_url"] + "/repos.manifest.json", args.kind)
    values: dict[str, object] = {
        **identifiers,
        "fingerprint": selected,
        "current_fingerprint": current,
        "needed": selected != current,
        "source_sha": source_sha,
        "system_sha": system_sha,
        "packages_sha": packages_sha,
        "os_base_sha": args.os_base_sha,
        "freebsd_sha": lock["freebsd_source"]["commit"],
        "ports_sha": lock["freebsd_ports"]["commit"],
        "image_sha256": lock["worker_image"]["sha256"],
        "jail_object": lock["jail_seed"]["object"],
        "package_train": policy["package_train"],
        "abi": policy["abi"],
        "altabi": policy["altabi"],
        "public_base_url": policy["public_base_url"],
    }
    print(json.dumps(values, indent=2, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
