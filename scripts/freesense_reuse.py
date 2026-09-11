#!/usr/bin/env python3
"""Exact package provenance for previous-FreeSense repository reuse."""
from __future__ import annotations

import hashlib
import json
import re

SHA256 = re.compile(r"^[0-9a-f]{64}$")
FIELDS = ("abi", "osversion", "origin", "version", "options", "dependencies",
          "port_directory_sha256", "patches_sha256", "mk_sha256",
          "make_configuration_sha256", "architecture_policy_sha256")


def provenance(package: dict) -> dict:
    result = {"schema_version": "freesense.package-provenance/v1"}
    for field in FIELDS:
        if field not in package:
            raise ValueError(f"missing provenance input: {field}")
        result[field] = package[field]
    result["patched"] = bool(package.get("patched", False))
    result["kernel_sensitive"] = bool(package.get("kernel_sensitive", False))
    if (not isinstance(result["osversion"], int) or result["osversion"] <= 0 or
            not all(SHA256.fullmatch(str(result[field])) for field in FIELDS if field.endswith("sha256")) or
            not isinstance(result["options"], dict) or not isinstance(result["dependencies"], dict)):
        raise ValueError("invalid package provenance")
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    result["digest"] = hashlib.sha256(raw).hexdigest()
    return result


def reusable(previous: dict, current: dict, *, pin_unchanged: bool) -> tuple[bool, str]:
    if not pin_unchanged:
        return False, "FreeBSD pin changed"
    try:
        before, after = provenance(previous), provenance(current)
    except ValueError as error:
        return False, str(error)
    if before != after:
        changed = next(field for field in (*FIELDS, "digest") if before.get(field) != after.get(field))
        return False, f"provenance changed: {changed}"
    # Kernel-coupled and locally patched packages retain a source-only rule
    # because their effective ABI is not fully represented by pkg metadata.
    if current.get("kernel_sensitive") or current.get("patched"):
        return False, "source-only architecture policy"
    return True, "exact provenance match"


def select(previous: list[dict], current: list[dict], *, pin_unchanged: bool) -> dict:
    old = {item.get("name"): item for item in previous}
    accepted, rejected = {}, {}
    for item in sorted(current, key=lambda value: str(value.get("name"))):
        name = item.get("name")
        if not isinstance(name, str) or name in accepted or name in rejected:
            raise ValueError("invalid or duplicate package name")
        ok, reason = reusable(old.get(name, {}), item, pin_unchanged=pin_unchanged)
        (accepted if ok else rejected)[name] = item if ok else reason
    return {"schema_version": "freesense.previous-seed/v1", "accepted": accepted,
            "rejected": rejected, "package_count": len(accepted)}


def merge_seeds(official: list[dict], previous: list[dict]) -> dict:
    """Merge seed inventories, rejecting every conflicting package identity."""
    merged, rejected = {}, {}
    for tier, records in (("official", official), ("previous-freesense", previous)):
        for record in records:
            name = record.get("name")
            if not isinstance(name, str) or not SHA256.fullmatch(str(record.get("sha256"))):
                raise ValueError("invalid seed package record")
            if name in rejected:
                continue
            if name in merged:
                existing = merged[name]
                identity = ("version", "origin", "abi", "sha256")
                if any(existing.get(field) != record.get(field) for field in identity):
                    rejected[name] = "conflicting seed package variants"
                    del merged[name]
                continue
            merged[name] = {**record, "tier": tier}
    return {"schema_version": "freesense.merged-binary-seed/v1",
            "packages": [merged[name] for name in sorted(merged)],
            "rejected": dict(sorted(rejected.items()))}
