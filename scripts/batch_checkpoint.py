#!/usr/bin/env python3
"""Cumulative immutable package-farm checkpoint contract."""
from __future__ import annotations

import hashlib
import json
import re

SHA256 = re.compile(r"^[0-9a-f]{64}$")
SCHEMA = "freesense.cumulative-batch-checkpoint/v1"


def identity(architecture: str, pin: str, component: str, policy: str,
             shard_count: int, shard: int, batch: int) -> str:
    if architecture not in ("amd64", "arm64") or not all(SHA256.fullmatch(x) for x in (pin, component)):
        raise ValueError("invalid checkpoint inputs")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", policy) or shard_count != 8 or not (0 <= shard < shard_count) or batch < 0:
        raise ValueError("invalid checkpoint namespace")
    payload = {"schema_version": SCHEMA, "architecture": architecture, "pin": pin,
               "component": component, "policy": policy, "shard_count": shard_count,
               "shard": shard, "batch": batch}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"checkpoints/{architecture}/{pin}/{component}/{policy}/farm-{shard_count}/shard-{shard}/batch-{batch}/{digest}"


def validate(marker: dict, expected_roots: list[str], previous: dict | None = None) -> None:
    if marker.get("schema_version") != SCHEMA or marker.get("roots") != expected_roots:
        raise ValueError("checkpoint does not match cumulative batch plan")
    packages = marker.get("packages")
    if not isinstance(packages, list) or any(not isinstance(item, dict) or not SHA256.fullmatch(str(item.get("sha256"))) for item in packages):
        raise ValueError("invalid checkpoint inventory")
    names = [item.get("name") for item in packages]
    if any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise ValueError("duplicate checkpoint package")
    if previous:
        if marker.get("batch") != previous.get("batch") + 1 or not set(previous.get("roots", [])) <= set(expected_roots):
            raise ValueError("checkpoint is not cumulative")
        old = {item["name"]: item["sha256"] for item in previous.get("packages", [])}
        new = {item["name"]: item["sha256"] for item in packages}
        if any(new.get(name) != digest for name, digest in old.items()):
            raise ValueError("cumulative checkpoint changed completed package bytes")
