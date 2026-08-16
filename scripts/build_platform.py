#!/usr/bin/env python3
"""Validated architecture and image-profile policy shared by build scripts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TARGET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ABI = re.compile(r"^FreeBSD:([0-9]+):(amd64|aarch64)$")


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy = json.loads((path or ROOT / "config/build-policy.json").read_text(encoding="utf-8"))
    if policy.get("schema_version") == "freesense.build-policy/v2" or (
        "schema_version" not in policy and "abi" in policy and "cloud" in policy
    ):
        # Read-only compatibility for checked Stable locks and migration tests.
        cloud = policy.pop("cloud")
        abi, altabi = policy.pop("abi"), policy.pop("altabi")
        policy.update({
            "schema_version": "freesense.build-policy/v3",
            "default_target": "amd64",
            "default_image_profiles": {"amd64": "generic-amd64"},
            "targets": {"amd64": {
                "architecture": "amd64", "freebsd_target": "amd64",
                "freebsd_target_arch": "amd64", "package_arch": "amd64",
                "poudriere_arch": "amd64.amd64", "abi": abi, "altabi": altabi,
                "kernel": "pfSense", "executor": "native-amd64",
                "build_enabled": True, "publish_enabled": True,
            }},
            "image_profiles": {"generic-amd64": {
                "target": "amd64", "platform": "generic-amd64",
                "partition_scheme": cloud.get("partition_scheme", "gpt"),
                "firmware": cloud.get("firmware", ["bios", "uefi"]), "installer": "iso",
                "filesystems": ["ufs", "zfs"], "formats": cloud["formats"],
                "devices": ["virtio-blk", "virtio-net"],
                "capabilities": {"bios": True, "uefi": True, "iso": True,
                                 "installer_img": False, "cloud_init": True},
                "variants": cloud["variants"],
            }},
        })
    if policy.get("schema_version") != "freesense.build-policy/v3":
        raise SystemExit("unsupported build policy schema")
    targets = policy.get("targets")
    profiles = policy.get("image_profiles")
    if not isinstance(targets, dict) or not targets:
        raise SystemExit("build policy has no targets")
    if not isinstance(profiles, dict) or not profiles:
        raise SystemExit("build policy has no image profiles")
    for name, value in targets.items():
        _validate_target(name, value)
    for name, value in profiles.items():
        _validate_profile(name, value, targets)
    if policy.get("default_target") not in targets:
        raise SystemExit("build policy default target is invalid")
    return policy


def _validate_target(name: str, value: Any) -> None:
    if not TARGET_NAME.fullmatch(name) or not isinstance(value, dict):
        raise SystemExit(f"invalid target {name!r}")
    required = {
        "architecture", "freebsd_target", "freebsd_target_arch", "package_arch",
        "poudriere_arch", "abi", "altabi", "kernel", "executor",
        "build_enabled", "publish_enabled",
    }
    if set(value) != required:
        raise SystemExit(f"target {name} has invalid fields")
    if value["architecture"] != name:
        raise SystemExit(f"target {name} architecture does not match its key")
    match = ABI.fullmatch(value["abi"] if isinstance(value["abi"], str) else "")
    expected = "amd64" if name == "amd64" else "aarch64"
    if match is None or match.group(2) != expected:
        raise SystemExit(f"target {name} has an invalid ABI")
    expected_values = {
        "amd64": ("amd64", "amd64", "amd64", "amd64.amd64", "freebsd:16:x86:64"),
        "arm64": ("arm64", "aarch64", "aarch64", "arm64.aarch64", "freebsd:16:aarch64:64"),
    }
    if name not in expected_values or tuple(value[key] for key in (
        "freebsd_target", "freebsd_target_arch", "package_arch", "poudriere_arch", "altabi"
    )) != expected_values[name]:
        raise SystemExit(f"target {name} does not use the canonical FreeBSD mapping")
    if not all(isinstance(value[key], bool) for key in ("build_enabled", "publish_enabled")):
        raise SystemExit(f"target {name} enable flags must be boolean")


def _validate_profile(name: str, value: Any, targets: dict[str, Any]) -> None:
    if not TARGET_NAME.fullmatch(name) or not isinstance(value, dict):
        raise SystemExit(f"invalid image profile {name!r}")
    required = {
        "target", "platform", "partition_scheme", "firmware", "installer",
        "filesystems", "formats", "devices", "capabilities", "variants",
    }
    if set(value) != required or value["target"] not in targets or value["platform"] != name:
        raise SystemExit(f"image profile {name} has invalid fields")
    if value["partition_scheme"] != "gpt" or value["filesystems"] != ["ufs", "zfs"]:
        raise SystemExit(f"image profile {name} must provide GPT UFS and ZFS images")
    if value["formats"] != ["qcow2", "raw"]:
        raise SystemExit(f"image profile {name} must provide QCOW2 and raw images")
    if not isinstance(value["capabilities"], dict) or not isinstance(value["variants"], dict):
        raise SystemExit(f"image profile {name} has invalid capabilities")


def target(policy: dict[str, Any], name: str | None) -> dict[str, Any]:
    selected = name or policy["default_target"]
    try:
        value = dict(policy["targets"][selected])
    except KeyError as error:
        raise SystemExit(f"unknown target: {selected}") from error
    value["name"] = selected
    return value


def image_profile(policy: dict[str, Any], name: str | None, target_name: str) -> dict[str, Any]:
    selected = name or policy["default_image_profiles"].get(target_name)
    try:
        value = dict(policy["image_profiles"][selected])
    except (KeyError, TypeError) as error:
        raise SystemExit(f"no image profile selected for target {target_name}") from error
    if value["target"] != target_name:
        raise SystemExit(f"image profile {selected} belongs to target {value['target']}")
    value["name"] = selected
    return value


def pin_target(lock: dict[str, Any], target_name: str) -> dict[str, Any]:
    if lock.get("schema_version") == "freesense.freebsd-pin/v2" and target_name == "amd64":
        return {"ready": bool(lock.get("ready")), "jail_seed": lock.get("jail_seed", {})}
    if lock.get("schema_version") != "freesense.freebsd-pin/v3":
        raise SystemExit("unsupported FreeBSD pin schema")
    try:
        value = lock["targets"][target_name]
    except (KeyError, TypeError) as error:
        raise SystemExit(f"FreeBSD pin has no {target_name} inputs") from error
    if not isinstance(value, dict) or not value.get("ready"):
        raise SystemExit(f"FreeBSD pin target {target_name} is not ready")
    return value


def manifest_name(target_value: dict[str, Any], legacy: bool = False) -> str:
    if legacy and target_value["name"] == "amd64":
        return "repos.manifest.json"
    return f"repos.{target_value['package_arch']}.manifest.json"
