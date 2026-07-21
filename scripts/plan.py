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
SHA256 = re.compile(r"^[0-9a-f]{64}$")
USER_AGENT = "FreeSense-build/1"


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
        request = urllib.request.Request(
            manifest_url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            envelope = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return ""
        raise SystemExit(f"channel manifest fetch failed with HTTP {error.code}") from error
    except (OSError, urllib.error.URLError) as error:
        raise SystemExit(f"channel manifest fetch failed: {error}") from error

    try:
        if not isinstance(envelope, dict) or envelope.get("schema_version") != "freesense.repositories/v3":
            raise ValueError("unsupported channel envelope")
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
        if not isinstance(payload, dict) or payload.get("schema_version") != "freesense.channels/v1":
            raise ValueError("unsupported signed channel payload")
        selected = payload.get("channels", {}).get("devel", {}).get(component)
        if selected is None:
            return ""
        if not isinstance(selected, dict):
            raise ValueError("current component is not an object")
        value = selected.get("fingerprint", "")
        if not SHA256.fullmatch(value):
            raise ValueError("current component has an invalid fingerprint")
        return value
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"channel manifest verification failed: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("system", "packages", "iso"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--os-base-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--system-closure", type=Path)
    args = parser.parse_args()

    if args.kind in {"packages", "iso"}:
        if args.system_closure is None:
            raise SystemExit("--system-closure is required")
        try:
            closure = json.loads(args.system_closure.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit(f"selected System closure is unreadable: {error}") from error
        if not isinstance(closure, dict):
            raise SystemExit("selected System closure must be a JSON object")
    else:
        closure = {}

    system_id = closure.get("fingerprint", "")
    channel_name = closure.get("channel", "devel")
    channel_generation = closure.get("generation", 0)
    channel_package_train = closure.get("package_train", "")
    channel_payload_sha256 = closure.get("payload_sha256", "")
    channel_payload_base64 = closure.get("payload_base64", "")
    channel_signature_base64 = closure.get("signature_base64", "")
    system_platform_id = closure.get("artifact_platform", "")
    system_source_sha = closure.get("artifact_source_sha", "")
    system_system_sha = closure.get("artifact_system_sha", "")
    system_os_base_sha = closure.get("artifact_os_base_sha", "")
    system_freebsd_sha = closure.get("artifact_freebsd_sha", "")
    system_ports_sha = closure.get("artifact_ports_sha", "")
    system_image_sha256 = closure.get("artifact_image_sha256", "")
    system_jail_object = closure.get("artifact_jail_object", "")
    system_signing_public_key_sha256 = closure.get("artifact_signing_public_key_sha256", "")
    current_packages_fingerprint = closure.get("packages_fingerprint", "")

    if args.kind == "system" and not SHA.fullmatch(args.os_base_sha):
        raise SystemExit("--os-base-sha must be a full Git commit")
    lock = json.loads((ROOT / "config/freebsd-16.json").read_text())
    policy = json.loads((ROOT / "config/build-policy.json").read_text())
    if lock.get("schema_version") != "freesense.freebsd-pin/v2" or not lock.get("ready"):
        raise SystemExit("FreeBSD lock is not ready")

    artifact_policy = {
        key: policy[key]
        for key in ("package_train", "abi", "altabi", "public_base_url")
    }
    signing_public_key_sha256 = hashlib.sha256(
        (ROOT / "config/channel-signing-public.pem").read_bytes()
    ).hexdigest()
    patch_files = [ROOT / "apply.sh", ROOT / "manifest.env", *sorted((ROOT / "patches").glob("*.patch"))]
    platform_recipe = recipe_digest([
        ROOT / "scripts/runner/worker-common.sh",
        ROOT / "scripts/runner/stages/system.sh",
        *patch_files,
    ])
    runner_recipe = recipe_digest([ROOT / "scripts/runner/run-vm.sh"])
    latest_source_sha = ""
    latest_system_sha = ""
    desired_system = ""
    if args.kind == "system":
        latest_source_sha = remote_sha("FreeSense-org/freesense")
        latest_system_sha = remote_sha("FreeSense-org/freesense-system-ports")
        desired_platform = fingerprint({
            "schema": 2,
            "kind": "platform",
            "freebsd_source": lock["freebsd_source"]["commit"],
            "freebsd_ports": lock["freebsd_ports"]["commit"],
            "jail_seed": lock["jail_seed"]["sha256"],
            "worker_image": lock["worker_image"]["sha256"],
            "source": latest_source_sha,
            "system_ports": latest_system_sha,
            "package_train": policy["package_train"],
            "artifact_policy": artifact_policy,
            "runner_policy": policy["runner"],
            "runner_recipe": runner_recipe,
            "signing_public_key": signing_public_key_sha256,
            "recipe": platform_recipe,
        })
        desired_system = fingerprint({
            "schema": 2,
            "kind": "system",
            "platform": desired_platform,
            "source": latest_source_sha,
            "system_ports": latest_system_sha,
            "package_train": policy["package_train"],
            "recipe": recipe_digest([
                ROOT / "scripts/render-worker.py",
                ROOT / "scripts/runner/worker-common.sh",
                ROOT / "scripts/runner/stages/system.sh",
            ]),
        })

    selected_package_train = policy["package_train"]
    if args.kind in {"packages", "iso"}:
        sha_inputs = {
            "source": system_source_sha,
            "system": system_system_sha,
            "os base": system_os_base_sha,
            "FreeBSD": system_freebsd_sha,
            "ports": system_ports_sha,
        }
        sha256_inputs = {
            "System": system_id,
            "platform": system_platform_id,
            "image": system_image_sha256,
            "signing key": system_signing_public_key_sha256,
        }
        if any(not isinstance(value, str) or not SHA.fullmatch(value) for value in sha_inputs.values()):
            raise SystemExit("selected System closure contains an invalid Git commit")
        if any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in sha256_inputs.values()):
            raise SystemExit("selected System closure contains an invalid SHA-256 identity")
        if not isinstance(channel_package_train, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", channel_package_train):
            raise SystemExit("selected System package train is invalid")
        if not isinstance(system_jail_object, str) or not re.fullmatch(r"inputs/sha256/[0-9a-f]{64}", system_jail_object):
            raise SystemExit("selected System jail object is invalid")
        if system_signing_public_key_sha256 != signing_public_key_sha256:
            raise SystemExit("selected System uses a different repository signing trust root")
        if args.kind == "packages":
            if channel_name != "devel" or channel_package_train != policy["package_train"]:
                raise SystemExit("optional packages require the current devel package train")
            if current_packages_fingerprint and not SHA256.fullmatch(current_packages_fingerprint):
                raise SystemExit("selected channel has an invalid packages fingerprint")
        else:
            if channel_name not in {"devel", "stable"} or not isinstance(channel_generation, int) or channel_generation <= 0:
                raise SystemExit("selected ISO channel identity is invalid")
            try:
                payload = base64.b64decode(channel_payload_base64, validate=True)
                signature = base64.b64decode(channel_signature_base64, validate=True)
            except (TypeError, ValueError) as error:
                raise SystemExit("selected ISO channel document is invalid") from error
            if (not isinstance(channel_payload_sha256, str)
                    or not SHA256.fullmatch(channel_payload_sha256)
                    or hashlib.sha256(payload).hexdigest() != channel_payload_sha256
                    or not signature):
                raise SystemExit("selected ISO channel document is invalid")
        selected_package_train = channel_package_train
        source_sha = system_source_sha
        system_sha = system_system_sha
        packages_sha = remote_sha("FreeSense-org/freesense-packages") if args.kind == "packages" else "0" * 40
        os_base_sha = system_os_base_sha
        freebsd_sha = system_freebsd_sha
        ports_sha = system_ports_sha
        image_sha256 = system_image_sha256
        jail_object = system_jail_object
        platform = system_platform_id
        system = system_id
    else:
        source_sha = latest_source_sha
        system_sha = latest_system_sha
        packages_sha = "0" * 40
        os_base_sha = args.os_base_sha
        freebsd_sha = lock["freebsd_source"]["commit"]
        ports_sha = lock["freebsd_ports"]["commit"]
        image_sha256 = lock["worker_image"]["sha256"]
        jail_object = lock["jail_seed"]["object"]
        platform = desired_platform
        system = desired_system
    packages = fingerprint({
        "schema": 2,
        "kind": "packages",
        "platform": platform,
        "system": system,
        "source": source_sha,
        "system_ports": system_sha,
        "packages": packages_sha,
        "package_train": selected_package_train,
        "recipe": recipe_digest([
            ROOT / "scripts/render-worker.py",
            ROOT / "scripts/runner/worker-common.sh",
            ROOT / "scripts/runner/stages/packages.sh",
        ]),
    })
    iso = fingerprint({
        "schema": 2,
        "kind": "iso",
        "system": system,
        "platform": platform,
        "source": source_sha,
        "freebsd_source": freebsd_sha,
        "freebsd_ports": ports_sha,
        "worker_image": image_sha256,
        "channel": channel_name,
        "channel_generation": channel_generation,
        "channel_payload": channel_payload_sha256,
        "package_train": selected_package_train,
        "signing_public_key": signing_public_key_sha256,
        "runner_policy": policy["runner"],
        "runner_recipe": runner_recipe,
        "recipe": recipe_digest([ROOT / "scripts/render-worker.py", ROOT / "scripts/runner/worker-common.sh", ROOT / "scripts/runner/stages/iso.sh"]),
    })
    identifiers = {"platform": platform, "system": system, "packages": packages, "iso": iso}
    selected = identifiers[args.kind]
    manifest_url = policy["public_base_url"] + "/repos.manifest.json"
    if args.kind == "iso":
        current = ""
    elif args.kind == "packages":
        current = current_packages_fingerprint
    else:
        current = current_component(manifest_url, "system")
    values: dict[str, object] = {
        **identifiers,
        "fingerprint": selected,
        "current_fingerprint": current,
        "needed": selected != current,
        "source_sha": source_sha,
        "system_sha": system_sha,
        "packages_sha": packages_sha,
        "os_base_sha": os_base_sha,
        "freebsd_sha": freebsd_sha,
        "ports_sha": ports_sha,
        "image_sha256": image_sha256,
        "jail_object": jail_object,
        "package_train": selected_package_train,
        "abi": policy["abi"],
        "altabi": policy["altabi"],
        "public_base_url": policy["public_base_url"],
        "channel": channel_name,
        "channel_generation": channel_generation,
        "channel_payload_sha256": channel_payload_sha256,
        "channel_payload_base64": channel_payload_base64,
        "channel_signature_base64": channel_signature_base64,
        "signing_public_key_sha256": signing_public_key_sha256,
    }
    print(json.dumps(values, indent=2, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
