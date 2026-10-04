#!/usr/bin/env python3
"""Fail-closed validation and executor selection for immutable v4 pins.

The checked v3 pin remains usable by the legacy pipeline. A candidate is never
promoted to v4 merely by copying the amd64 host fields to the ARM64 target.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

ARCHES = {"amd64": "FreeBSD:16:amd64", "arm64": "FreeBSD:16:aarch64"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_REPOSITORIES = {"freebsd/freebsd-src", "freebsd/freebsd-ports"}
SOURCE_ARCHIVE_FORMAT = "freesense.git-bare-shallow-tar/v1"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def blob(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"missing {label}")
    sha = value.get("sha256")
    size = value.get("size")
    obj = value.get("object") or value.get("key")
    if (not isinstance(sha, str) or not SHA256.fullmatch(sha)
            or obj != f"inputs/sha256/{sha}"
            or type(size) is not int or size <= 0):
        raise ValueError(f"invalid immutable {label}")
    if value.get("object") != obj:
        value["object"] = obj
    return value


def validate(pin: dict, *, now: datetime | None = None) -> None:
    if pin.get("schema_version") != "freesense.freebsd-pin/v4":
        raise ValueError("multiarch requires a verified FreeBSD v4 pin")
    start = datetime.fromisoformat(pin["valid_from"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(pin["valid_until"].replace("Z", "+00:00"))
    if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0):
        raise ValueError("pin window must use UTC")
    if end - start != timedelta(days=14):
        raise ValueError("pin window must remain exactly 14 days")
    if now is not None and not start <= now < end:
        raise ValueError("pin is outside its active window")
    for field in ("freebsd_source", "freebsd_ports"):
        if not re.fullmatch(r"[0-9a-f]{40}", pin.get(field, {}).get("commit", "")):
            raise ValueError(f"invalid {field} commit")
    if set(pin.get("targets", {})) != set(ARCHES):
        raise ValueError("pin must contain both architectures")
    for arch, abi in ARCHES.items():
        target = pin["targets"][arch]
        if target.get("ready") is not True or target.get("abi") != abi:
            raise ValueError(f"{arch} pin is not ready for its native ABI")
        for field in ("jail_seed", "package_catalog", "binary_seed", "worker_image", "worker_tools"):
            blob(target.get(field), f"{arch} {field}")
        catalog = target["package_catalog"]
        if (catalog.get("signature_verified") is not True
                or not SHA256.fullmatch(str(catalog.get("trusted_key_sha256", "")))):
            raise ValueError(f"{arch} catalogue has no verified trust root")
        seed = target["binary_seed"]
        if (seed.get("abi") != abi or seed.get("catalog_sha256") != catalog["sha256"]
                or seed.get("verified") is not True
                or seed.get("requirements_components") != ["system", "packages"]
                or not SHA256.fullmatch(str(seed.get("requirements_sha256", "")))
                or not SHA256.fullmatch(str(seed.get("provenance_sha256", "")))):
            raise ValueError(f"{arch} has no verified union seed")
        for field in ("worker_image", "worker_tools"):
            if target[field].get("architecture") != arch or target[field].get("boot_verified") is not True:
                raise ValueError(f"{arch} {field} has not passed a native boot test")
        # Optional until the pin cycle cuts one. When present it binds the
        # mirror, the plan it came from and the ports commit both were
        # computed at, so the build cannot mix one snapshot with another.
        mirror = target.get("mirror")
        if mirror is not None:
            if (not isinstance(mirror, dict)
                    or not str(mirror.get("object", "")).startswith("inputs/sha256/")
                    or not SHA256.fullmatch(str(mirror.get("fingerprint", "")))
                    or not re.fullmatch(r"[0-9a-f]{40}", str(mirror.get("ports_commit", "")))):
                raise ValueError(f"{arch} mirror pin is incomplete")
    if not any("rust" in pin["targets"][arch]["binary_seed"].get("verified_roots", []) for arch in ARCHES):
        raise ValueError("pin has no official lang/rust package on any architecture")
    validate_source_archives(pin)


def validate_source_archives(pin: dict) -> None:
    # Optional until a pin cycle stores them. Each one is a bare one-commit
    # repository whose main branch is exactly the commit it is keyed by.
    archives = pin.get("source_archives")
    if archives is None:
        return
    if not isinstance(archives, dict):
        raise ValueError("source archives must map commits to immutable blobs")
    for commit, archive in archives.items():
        if not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
            raise ValueError("source archive is not keyed by a full Git commit")
        blob(archive, f"source archive {commit}")
        if (archive.get("repository") not in SOURCE_REPOSITORIES
                or archive.get("format") != SOURCE_ARCHIVE_FORMAT
                or archive.get("ref") != "refs/heads/main"):
            raise ValueError(f"source archive {commit} has an unsupported layout")


def archivable_commits(pin: dict) -> dict[str, str]:
    """Every upstream commit a build of this pin can clone, keyed to its repository.

    Builds take ports from their target's mirror when one is pinned, so each
    mirror's ports commit counts alongside the pin's own.
    """
    commits = {pin["freebsd_source"]["commit"]: "freebsd/freebsd-src",
               pin["freebsd_ports"]["commit"]: "freebsd/freebsd-ports"}
    for target in pin.get("targets", {}).values():
        ports = (target.get("mirror") or {}).get("ports_commit")
        if ports:
            commits[ports] = "freebsd/freebsd-ports"
    return commits


def with_source_archives(pin: dict, additions: dict[str, dict]) -> dict:
    """Merge archives into a pin, keeping only those its builds can still use."""
    merged = {**(pin.get("source_archives") or {}), **additions}
    wanted = archivable_commits(pin)
    kept = {commit: archive for commit, archive in sorted(merged.items())
            if wanted.get(commit) == archive.get("repository")}
    updated = {key: value for key, value in pin.items() if key != "source_archives"}
    if kept:
        updated["source_archives"] = kept
    validate_source_archives(updated)
    return updated


def source_archive(pin: dict, commit: str, repository: str) -> str:
    """The pinned archive object for one upstream commit, or '' to fetch it."""
    archive = (pin.get("source_archives") or {}).get(commit) or {}
    return archive.get("object", "") if archive.get("repository") == repository else ""


def worker(pin: dict, target: str, host: str) -> dict:
    validate(pin)
    allowed = {
        "amd64": {"github-amd64", "dedicated"},
        "arm64": {"github-arm64", "github-amd64", "dedicated"},
    }
    if host not in allowed.get(target, set()):
        raise ValueError("host does not support selected target")
    native_arch = "amd64" if host in {"dedicated", "github-amd64"} else target
    inputs = pin["targets"][native_arch]
    return {
        "host": host,
        "host_architecture": native_arch,
        "executor": "amd64-cross-qemu-user" if target != native_arch else f"native-{target}",
        "worker_image": inputs["worker_image"],
        "worker_tools": inputs["worker_tools"],
        "binary_seed": pin["targets"][target]["binary_seed"],
        # Per target, not per host: the mirror is the ABI's lower layer.
        "mirror": pin["targets"][target].get("mirror", {}),
    }


def select_arm_host(probe: dict, pin: dict, *, force_dedicated: bool = False) -> str:
    validate(pin)
    expected = pin["targets"]["arm64"]["worker_image"]["sha256"]
    passed = (
        probe.get("schema_version") == "freesense.arm64-capability/v1"
        and probe.get("image_sha256") == expected
        and all(probe.get(key) is True for key in ("kvm", "memory", "disk", "qemu", "firmware", "boot"))
    )
    if force_dedicated:
        return "dedicated"
    return "github-arm64" if passed else "github-amd64"


def rollover(previous: dict, candidate: dict, *, security_rollover: bool = False) -> dict:
    """Return a complete candidate; callers must leave the old pin on errors."""
    validate(candidate)
    start = datetime.fromisoformat(candidate["valid_from"].replace("Z", "+00:00"))
    boundary = datetime.fromisoformat(previous["valid_until"].replace("Z", "+00:00"))
    if not security_rollover and start != boundary:
        raise ValueError("normal rollover must start at the previous 14-day boundary")
    if security_rollover:
        old_start = datetime.fromisoformat(previous["valid_from"].replace("Z", "+00:00"))
        if start <= old_start:
            raise ValueError("security rollover must advance the pin")
    return candidate
