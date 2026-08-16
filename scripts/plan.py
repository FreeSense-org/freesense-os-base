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
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_platform import image_profile, load_policy, manifest_name, pin_target, target


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
USER_AGENT = "FreeSense-build/1"
OPTIONAL_PACKAGE_CONFIG_PATHS = (
    "tools/conf/pfPorts/make.conf",
)
SEMVER = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")
TRAIN = re.compile(r"^[0-9]+\.[0-9]+$")


def release_policy(policy: dict, channel: str, version: str) -> tuple[str, str]:
    configured = policy.get("release")
    if not isinstance(configured, dict):
        raise SystemExit("build policy has no release policy")
    match = SEMVER.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        raise SystemExit("release version must be semantic X.Y.Z")
    train = f"{match.group(1)}.{match.group(2)}"
    key = "stable_train" if channel == "stable" else "development_train"
    expected = configured.get(key)
    if not isinstance(expected, str) or not TRAIN.fullmatch(expected):
        raise SystemExit(f"build policy has an invalid {key}")
    if train != expected:
        raise SystemExit(f"{channel} release {version} does not match configured train {expected}")
    lifecycle = configured.get(
        "stable_lifecycle" if channel == "stable" else "development_lifecycle"
    )
    expected_lifecycle = "supported" if channel == "stable" else "experimental"
    if lifecycle != expected_lifecycle:
        raise SystemExit(f"{channel} release lifecycle must be {expected_lifecycle}")
    return train, lifecycle


def remote_sha(repository: str, branch: str = "main") -> str:
    output = subprocess.check_output(
        ["git", "ls-remote", f"https://github.com/{repository}.git", f"refs/heads/{branch}"],
        text=True,
    ).strip()
    value = output.split()[0] if output else ""
    if not SHA.fullmatch(value):
        raise SystemExit(f"could not resolve {repository}@{branch}")
    return value


def remote_recipe_digest(repository: str, commit: str, paths: tuple[str, ...]) -> str:
    if not SHA.fullmatch(commit):
        raise SystemExit("remote recipe commit must be a full Git commit")
    digest = hashlib.sha256()
    for path in sorted(paths):
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise SystemExit("remote recipe path is invalid")
        url = f"https://raw.githubusercontent.com/{repository}/{commit}/{path}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            raise SystemExit(
                f"remote recipe fetch failed for {path} with HTTP {error.code}"
            ) from error
        except (OSError, urllib.error.URLError) as error:
            raise SystemExit(f"remote recipe fetch failed for {path}: {error}") from error
        relative = path.encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


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
        if not isinstance(payload, dict) or payload.get("schema_version") not in {
            "freesense.channels/v1", "freesense.channels/v2", "freesense.channels/v3"
        }:
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
    parser.add_argument("kind", choices=("system", "packages", "iso", "cloud"))
    parser.add_argument("--target", default="amd64")
    parser.add_argument("--image-profile")
    parser.add_argument("--filesystem", choices=("ufs", "zfs"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--os-base-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--system-closure", type=Path)
    args = parser.parse_args()
    if args.kind == "cloud" and args.filesystem is None:
        raise SystemExit("cloud planning requires --filesystem ufs or zfs")
    if args.kind != "cloud" and args.filesystem is not None:
        raise SystemExit("--filesystem is valid only for cloud planning")
    if args.kind not in {"iso", "cloud"} and args.image_profile is not None:
        raise SystemExit("--image-profile is valid only for image planning")

    if args.kind in {"packages", "iso", "cloud"}:
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
    channel_release_version = closure.get("release_version", "")
    channel_payload_sha256 = closure.get("payload_sha256", "")
    channel_payload_base64 = closure.get("payload_base64", "")
    channel_signature_base64 = closure.get("signature_base64", "")
    channel_system_verified = closure.get("verified", "false")
    system_platform_id = closure.get("artifact_platform", "")
    system_source_sha = closure.get("artifact_source_sha", "")
    system_system_sha = closure.get("artifact_system_sha", "")
    system_packages_sha = closure.get("artifact_packages_sha", "")
    system_os_base_sha = closure.get("artifact_os_base_sha", "")
    system_freebsd_sha = closure.get("artifact_freebsd_sha", "")
    system_ports_sha = closure.get("artifact_ports_sha", "")
    system_image_sha256 = closure.get("artifact_image_sha256", "")
    system_worker_tools_sha256 = closure.get("artifact_worker_tools_sha256", "")
    system_jail_object = closure.get("artifact_jail_object", "")
    system_signing_public_key_sha256 = closure.get("artifact_signing_public_key_sha256", "")
    system_freebsd_pin_id = closure.get("artifact_freebsd_pin_id", "")
    current_packages_fingerprint = closure.get("packages_fingerprint", "")
    current_packages_generation = closure.get("packages_generation", 0)
    current_packages_verified = closure.get("packages_verified", "false")
    channel_osversion = closure.get("osversion", 0)
    channel_architecture = closure.get("architecture", "amd64")
    channel_package_arch = closure.get("package_arch", "amd64")

    if args.kind == "system" and not SHA.fullmatch(args.os_base_sha):
        raise SystemExit("--os-base-sha must be a full Git commit")
    lock = json.loads((ROOT / "config/freebsd-16.json").read_text())
    policy = load_policy(ROOT / "config/build-policy.json")
    selected_target = target(policy, args.target)
    if not selected_target["build_enabled"]:
        raise SystemExit(f"target {args.target} builds are disabled")
    target_pin = pin_target(lock, args.target)
    jail_seed = target_pin["jail_seed"]
    selected_profile = image_profile(policy, args.image_profile, args.target)
    if args.kind in {"packages", "iso", "cloud"} and (
        channel_architecture != selected_target["architecture"]
        or channel_package_arch != selected_target["package_arch"]
    ):
        raise SystemExit("selected System closure belongs to a different architecture")
    abi_match = re.fullmatch(r"FreeBSD:([0-9]+):(amd64|aarch64)", selected_target["abi"])
    pinned_osversion = lock.get("freebsd_source", {}).get("osversion")
    if (
        abi_match is None
        or not isinstance(pinned_osversion, int)
        or not int(abi_match.group(1)) * 100000
        <= pinned_osversion
        < (int(abi_match.group(1)) + 1) * 100000
    ):
        raise SystemExit("FreeBSD lock has no exact OSVERSION matching its ABI")
    try:
        valid_from = datetime.fromisoformat(lock["valid_from"].replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(lock["valid_until"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit("FreeBSD lock has no valid 14-day window") from error
    now = datetime.now(timezone.utc)
    if valid_until - valid_from != timedelta(days=14):
        raise SystemExit("FreeBSD lock window must be exactly 14 days")
    if now < valid_from or now >= valid_until:
        raise SystemExit("FreeBSD lock is outside its active 14-day window")
    worker_tools = lock.get("worker_tools", {})
    worker_tools_lock_sha256 = (
        worker_tools.get("sha256", "") if isinstance(worker_tools, dict) else ""
    )
    if args.kind == "system" and (
        not SHA256.fullmatch(worker_tools_lock_sha256)
        or worker_tools.get("object") != "inputs/sha256/" + worker_tools_lock_sha256
    ):
        raise SystemExit("FreeBSD lock has no pinned worker-tool bundle; run Pin FreeBSD")

    artifact_policy = {
        "package_train": policy["package_train"],
        "abi": selected_target["abi"],
        "altabi": selected_target["altabi"],
        "public_base_url": policy["public_base_url"],
    }
    signing_public_key_sha256 = hashlib.sha256(
        (ROOT / "config/channel-signing-public.pem").read_bytes()
    ).hexdigest()
    freebsd_pin_id = fingerprint({
        "schema": 1,
        "kind": "freebsd-pin",
        "freebsd_source": lock["freebsd_source"]["commit"],
        "freebsd_ports": lock["freebsd_ports"]["commit"],
        "jail_seed": jail_seed["sha256"],
        "worker_image": lock["worker_image"]["sha256"],
        "worker_tools": worker_tools_lock_sha256,
        "abi": selected_target["abi"],
        "altabi": selected_target["altabi"],
    })
    patch_files = [ROOT / "apply.sh", ROOT / "manifest.env", *sorted((ROOT / "patches").glob("*.patch"))]
    platform_recipe = recipe_digest([
        ROOT / "scripts/runner/install-worker-tools.sh",
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
            "jail_seed": jail_seed["sha256"],
            "worker_image": lock["worker_image"]["sha256"],
            "worker_tools": worker_tools_lock_sha256,
            "source": latest_source_sha,
            "system_ports": latest_system_sha,
            "package_train": policy["package_train"],
            "artifact_policy": artifact_policy,
            "runner_policy": policy["runner"],
            "runner_recipe": runner_recipe,
            "signing_public_key": signing_public_key_sha256,
            "recipe": platform_recipe,
            **({} if args.target == "amd64" else {"target": selected_target}),
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
                ROOT / "scripts/runner/install-worker-tools.sh",
                ROOT / "scripts/runner/worker-common.sh",
                ROOT / "scripts/runner/stages/system.sh",
            ]),
        })

    selected_package_train = policy["package_train"]
    if args.kind in {"packages", "iso", "cloud"}:
        sha_inputs = {
            "source": system_source_sha,
            "system": system_system_sha,
            "os base": system_os_base_sha,
            "FreeBSD": system_freebsd_sha,
            "ports": system_ports_sha,
        }
        if args.kind in {"iso", "cloud"}:
            sha_inputs["optional packages"] = system_packages_sha
        sha256_inputs = {
            "System": system_id,
            "platform": system_platform_id,
            "image": system_image_sha256,
            "worker tools": system_worker_tools_sha256,
            "signing key": system_signing_public_key_sha256,
            "FreeBSD pin": system_freebsd_pin_id,
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
                raise SystemExit("selected release channel identity is invalid")
            release_policy(policy, channel_name, channel_release_version)
            if (
                channel_system_verified != "true"
                or not SHA256.fullmatch(current_packages_fingerprint)
                or not isinstance(current_packages_generation, int)
                or current_packages_generation <= 0
                or current_packages_verified != "true"
            ):
                raise SystemExit("selected release channel is not a verified System/Packages pair")
            try:
                payload = base64.b64decode(channel_payload_base64, validate=True)
                signature = base64.b64decode(channel_signature_base64, validate=True)
            except (TypeError, ValueError) as error:
                raise SystemExit("selected release channel document is invalid") from error
            if (not isinstance(channel_payload_sha256, str)
                    or not SHA256.fullmatch(channel_payload_sha256)
                    or hashlib.sha256(payload).hexdigest() != channel_payload_sha256
                    or (selected_target["publish_enabled"] and not signature)):
                raise SystemExit("selected release channel document is invalid")
        selected_package_train = channel_package_train
        source_sha = system_source_sha
        system_sha = system_system_sha
        packages_sha = (
            remote_sha("FreeSense-org/freesense-packages")
            if args.kind == "packages" else system_packages_sha
        )
        os_base_sha = system_os_base_sha
        freebsd_sha = system_freebsd_sha
        ports_sha = system_ports_sha
        image_sha256 = system_image_sha256
        worker_tools_sha256 = system_worker_tools_sha256
        jail_object = system_jail_object
        platform = system_platform_id
        system = system_id
        freebsd_pin_id = system_freebsd_pin_id
    else:
        source_sha = latest_source_sha
        system_sha = latest_system_sha
        packages_sha = "0" * 40
        os_base_sha = args.os_base_sha
        freebsd_sha = lock["freebsd_source"]["commit"]
        ports_sha = lock["freebsd_ports"]["commit"]
        image_sha256 = lock["worker_image"]["sha256"]
        worker_tools_sha256 = worker_tools_lock_sha256
        jail_object = jail_seed["object"]
        platform = desired_platform
        system = desired_system
    package_build_config = (
        remote_recipe_digest(
            "FreeSense-org/freesense",
            source_sha,
            OPTIONAL_PACKAGE_CONFIG_PATHS,
        )
        if args.kind == "packages"
        else "0" * 64
    )
    optional_architecture_policy = (
        remote_recipe_digest(
            "FreeSense-org/freesense-packages", packages_sha,
            ("architecture-policy.json",),
        )
        if args.kind == "packages" else "0" * 64
    )
    packages = fingerprint({
        "schema": 4,
        "kind": "packages",
        "freebsd_pin": freebsd_pin_id,
        "packages": packages_sha,
        "package_build_config": package_build_config,
        "architecture_policy": optional_architecture_policy,
        "package_train": selected_package_train,
        "signing_public_key": signing_public_key_sha256,
        "recipe": recipe_digest([
            ROOT / "scripts/render-worker.py",
            ROOT / "scripts/runner/install-worker-tools.sh",
            ROOT / "scripts/runner/worker-common.sh",
            ROOT / "scripts/runner/stages/packages.sh",
        ]),
        **({} if args.target == "amd64" else {"package_arch": selected_target["package_arch"]}),
    })
    release_version = (
        channel_release_version
        if args.kind in {"iso", "cloud"}
        else policy["release"]["development_version"]
    )
    if args.kind in {"iso", "cloud"}:
        release_policy(policy, channel_name, release_version)
    shared_assembly = {
        "schema": 1,
        "system": system,
        "packages": current_packages_fingerprint,
        "platform": platform,
        "source": source_sha,
        "freebsd_source": freebsd_sha,
        "freebsd_ports": ports_sha,
        "worker_image": image_sha256,
        "worker_tools": worker_tools_sha256,
        "channel": channel_name,
        "channel_payload": channel_payload_sha256,
        "release_version": release_version,
        "package_train": selected_package_train,
        "signing_public_key": signing_public_key_sha256,
        "runner_policy": policy["runner"],
        "runner_recipe": runner_recipe,
        "assembly_recipe": recipe_digest([
            ROOT / "scripts/render-worker.py",
            ROOT / "scripts/runner/install-worker-tools.sh",
            ROOT / "scripts/runner/worker-common.sh",
            ROOT / "scripts/runner/assembly-common.sh",
        ]),
        "iso_recipe": recipe_digest([
            ROOT / "scripts/runner/stages/iso.sh",
            ROOT / "patches/0005-installer.patch",
        ]),
        "cloud_recipe": recipe_digest([
            ROOT / "scripts/runner/stages/cloud.sh",
        ]),
        "cloud_policy": selected_profile,
        **({} if args.target == "amd64" else {
            "architecture": selected_target["architecture"],
            "package_arch": selected_target["package_arch"],
            "image_profile": selected_profile["name"],
        }),
    }
    bundle = fingerprint(shared_assembly)
    iso = fingerprint({
        "schema": 3,
        "kind": "iso",
        "bundle": bundle,
    })
    cloud_variants = selected_profile.get("variants", {})
    if (not isinstance(cloud_variants, dict)
            or set(cloud_variants) != {"ufs", "zfs"}):
        raise SystemExit("cloud policy must define exactly ufs and zfs variants")
    for filesystem, variant in cloud_variants.items():
        if (not isinstance(variant, dict)
                or not isinstance(variant.get("virtual_size_gib"), int)
                or variant["virtual_size_gib"] <= 0
                or variant.get("root_growth") is not True):
            raise SystemExit(f"cloud {filesystem} variant is invalid")
    cloud_ids = {
        filesystem: fingerprint({
            "schema": 2,
            "kind": "cloud",
            "filesystem": filesystem,
            "variant": variant,
            "bundle": bundle,
        })
        for filesystem, variant in cloud_variants.items()
    }
    selected_filesystem = args.filesystem or "ufs"
    cloud = cloud_ids[selected_filesystem]
    identifiers = {
        "platform": platform, "system": system, "packages": packages,
        "bundle": bundle, "iso": iso, "cloud": cloud,
        "cloud_ufs": cloud_ids["ufs"], "cloud_zfs": cloud_ids["zfs"],
    }
    selected = identifiers[args.kind]
    manifest_url = policy["public_base_url"] + "/" + manifest_name(
        selected_target, legacy=args.target == "amd64"
    )
    if args.kind in {"iso", "cloud"}:
        current = ""
    elif args.kind == "packages":
        current = current_packages_fingerprint
    else:
        # Experimental targets build into immutable staging prefixes before
        # they are allowed to publish a signed channel root.
        current = (current_component(manifest_url, "system")
                   if selected_target["publish_enabled"] else "")
    values: dict[str, object] = {
        **identifiers,
        "fingerprint": selected,
        "current_fingerprint": current,
        "needed": selected != current,
        "source_sha": source_sha,
        "system_sha": system_sha,
        "packages_sha": packages_sha,
        "packages_fingerprint": (
            current_packages_fingerprint if args.kind in {"iso", "cloud"} else ""
        ),
        "package_build_config_sha256": package_build_config,
        "optional_architecture_policy_sha256": optional_architecture_policy,
        "os_base_sha": os_base_sha,
        "freebsd_sha": freebsd_sha,
        "ports_sha": ports_sha,
        "image_sha256": image_sha256,
        "worker_tools_sha256": worker_tools_sha256,
        "jail_object": jail_object,
        "package_train": selected_package_train,
        "target": args.target,
        "architecture": selected_target["architecture"],
        "package_arch": selected_target["package_arch"],
        "freebsd_target": selected_target["freebsd_target"],
        "freebsd_target_arch": selected_target["freebsd_target_arch"],
        "poudriere_arch": selected_target["poudriere_arch"],
        "kernel": selected_target["kernel"],
        "executor": selected_target["executor"],
        "publish_enabled": selected_target["publish_enabled"],
        "image_profile": selected_profile["name"],
        "firmware": ",".join(selected_profile["firmware"]),
        "image_capabilities": json.dumps(selected_profile["capabilities"], sort_keys=True, separators=(",", ":")),
        "installer_format": selected_profile["installer"],
        "abi": selected_target["abi"],
        "altabi": selected_target["altabi"],
        "osversion": pinned_osversion if args.kind == "system" else channel_osversion,
        "public_base_url": policy["public_base_url"],
        "channel": channel_name,
        "channel_generation": channel_generation,
        "release_version": release_version,
        "channel_payload_sha256": channel_payload_sha256,
        "channel_payload_base64": channel_payload_base64,
        "channel_signature_base64": channel_signature_base64,
        "signing_public_key_sha256": signing_public_key_sha256,
        "freebsd_pin_id": freebsd_pin_id,
        "cloud_filesystem": selected_filesystem,
        "cloud_virtual_size_gib": cloud_variants[selected_filesystem]["virtual_size_gib"],
        "cloud_ufs_virtual_size_gib": cloud_variants["ufs"]["virtual_size_gib"],
        "cloud_zfs_virtual_size_gib": cloud_variants["zfs"]["virtual_size_gib"],
        "product_version": (f"{release_version}-RELEASE"
                            if args.kind in {"iso", "cloud"} and channel_name == "stable"
                            else f"{release_version}-DEVELOPMENT"),
    }
    print(json.dumps(values, indent=2, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
