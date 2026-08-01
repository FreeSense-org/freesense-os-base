#!/usr/bin/env python3
"""Inventory R2 and plan conservative FreeSense retention.

This first implementation is deliberately report-only. It computes exact
candidate keys but never issues a delete request.
"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


INVENTORY_SCHEMA = "freesense.r2-inventory/v1"
REPORT_SCHEMA = "freesense.r2-retention-report/v1"
STATE_SCHEMA = "freesense.r2-retention-state/v1"
ENVELOPE_SCHEMA = "freesense.repositories/v3"
PAYLOAD_SCHEMAS = {
    "freesense.channels/v1",
    "freesense.channels/v2",
    "freesense.channels/v3",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SYSTEM_KEY = re.compile(
    r"^v1/artifacts/system/(?P<fingerprint>[0-9a-f]{64})/(?P<relative>.+)$"
)
PACKAGES_KEY = re.compile(
    r"^v1/artifacts/packages/(?P<train>[0-9]+\.[0-9]+)/"
    r"(?P<fingerprint>[0-9a-f]{64})/(?P<relative>.+)$"
)
ISO_KEY = re.compile(
    r"^v1/artifacts/iso/(?P<fingerprint>[0-9a-f]{64})/(?P<relative>.+)$"
)
CLOUD_KEY = re.compile(
    r"^v1/artifacts/cloud/(?P<fingerprint>[0-9a-f]{64})/(?P<relative>.+)$"
)
DEVEL_DOWNLOAD = re.compile(
    r"^v1/releases/devel/(?P<release>[0-9]+\.[0-9]+\.[0-9]+-g(?P<generation>[1-9][0-9]*))/"
)
DOCUMENT_KEYS = {
    "v1/repos.manifest.json",
    "v1/releases/stable.json",
    "v1/releases/devel.json",
    "v1/state/retention.json",
}
MAX_DOCUMENT_SIZE = 1024 * 1024
MAX_DELETE_OBJECTS = 5000
MAX_DELETE_BYTES = 50 * 1024**3


def fail(message: str) -> None:
    raise SystemExit(message)


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        fail(f"invalid R2 object timestamp {value!r}")
    if parsed.tzinfo is None:
        fail(f"R2 object timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def aws(
    *args: str, output: Path | None = None, missing_ok: bool = False
) -> Any:
    command = ["aws", *args]
    if output is not None:
        command.append(str(output))
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        if missing_ok and any(
            value in completed.stderr
            for value in ("404", "Not Found", "NoSuchKey")
        ):
            return None
        fail(
            f"AWS CLI failed ({' '.join(command[:-1] if output else command)}): "
            f"{completed.stderr.strip()}"
        )
    if output is not None:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        fail("AWS CLI returned invalid JSON")


def snapshot(bucket: str, endpoint: str, kind: str, captured_at: datetime) -> dict[str, Any]:
    prefixes = (
        ("v1/artifacts/", "v1/inputs/sha256/", "v1/smoke/broker/")
        if kind == "build"
        else ("v1/releases/", "v1/smoke/broker/")
    )
    contents: list[Any] = []
    for prefix in prefixes:
        listed = aws(
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--endpoint-url",
            endpoint,
            "--page-size",
            "1000",
            "--output",
            "json",
        )
        page = listed.get("Contents", [])
        if not isinstance(page, list):
            fail("R2 listing has no object list")
        contents.extend(page)
    if kind == "build":
        for key in sorted(DOCUMENT_KEYS):
            headed = aws(
                "s3api",
                "head-object",
                "--bucket",
                bucket,
                "--key",
                key,
                "--endpoint-url",
                endpoint,
                "--output",
                "json",
                missing_ok=True,
            )
            if headed is not None:
                contents.append(
                    {
                        "Key": key,
                        "Size": headed.get("ContentLength"),
                        "LastModified": headed.get("LastModified"),
                        "ETag": headed.get("ETag", ""),
                    }
                )
    objects: list[dict[str, Any]] = []
    for item in contents:
        if not isinstance(item, dict):
            fail("R2 listing contains an invalid object")
        key = item.get("Key")
        size = item.get("Size")
        modified = item.get("LastModified")
        etag = item.get("ETag", "")
        if (
            not isinstance(key, str)
            or not key.startswith("v1/")
            or not isinstance(size, int)
            or size < 0
            or not isinstance(modified, str)
            or not isinstance(etag, str)
        ):
            fail("R2 listing contains invalid object metadata")
        parse_time(modified)
        objects.append(
            {
                "key": key,
                "size": size,
                "last_modified": modified,
                "etag": etag,
            }
        )

    documents: dict[str, Any] = {}
    if kind == "build":
        wanted = [
            item
            for item in objects
            if item["key"] in DOCUMENT_KEYS or item["key"].endswith("/complete.json")
        ]
        with tempfile.TemporaryDirectory(prefix="freesense-retention.") as directory:
            root = Path(directory)
            for number, item in enumerate(wanted):
                if item["size"] > MAX_DOCUMENT_SIZE:
                    fail(f"refusing oversized retention document {item['key']!r}")
                destination = root / f"{number}.json"
                aws(
                    "s3api",
                    "get-object",
                    "--bucket",
                    bucket,
                    "--key",
                    item["key"],
                    "--endpoint-url",
                    endpoint,
                    "--output",
                    "json",
                    output=destination,
                )
                try:
                    documents[item["key"]] = json.loads(
                        destination.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    fail(f"R2 document is not valid JSON: {item['key']!r}")

    return {
        "schema_version": INVENTORY_SCHEMA,
        "kind": kind,
        "bucket": bucket,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "objects": sorted(objects, key=lambda item: item["key"]),
        "documents": documents,
    }


def verify_manifest(envelope: Any, public_key: Path) -> dict[str, Any]:
    if not isinstance(envelope, dict) or envelope.get("schema_version") != ENVELOPE_SCHEMA:
        fail("repository manifest has an unsupported envelope")
    try:
        payload = base64.b64decode(envelope["payload"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (KeyError, TypeError, ValueError):
        fail("repository manifest has invalid canonical base64")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        fail("repository manifest payload is not JSON")
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema_version") not in PAYLOAD_SCHEMAS
        or not isinstance(decoded.get("channels"), dict)
    ):
        fail("repository manifest payload has an unsupported schema")

    with tempfile.TemporaryDirectory(prefix="freesense-manifest.") as directory:
        root = Path(directory)
        payload_path = root / "payload.json"
        signature_path = root / "signature.bin"
        payload_path.write_bytes(payload)
        signature_path.write_bytes(signature)
        checked = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(public_key),
                "-signature",
                str(signature_path),
                str(payload_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    if checked.returncode != 0:
        fail("repository manifest signature is invalid")
    return decoded


def classify_artifact_key(key: str) -> tuple[str, str, str | None, str] | None:
    match = SYSTEM_KEY.fullmatch(key)
    if match:
        return (
            "system",
            f"v1/artifacts/system/{match['fingerprint']}",
            None,
            match["fingerprint"],
        )
    match = PACKAGES_KEY.fullmatch(key)
    if match:
        return (
            "packages",
            f"v1/artifacts/packages/{match['train']}/{match['fingerprint']}",
            match["train"],
            match["fingerprint"],
        )
    match = ISO_KEY.fullmatch(key)
    if match:
        return (
            "iso",
            f"v1/artifacts/iso/{match['fingerprint']}",
            None,
            match["fingerprint"],
        )
    match = CLOUD_KEY.fullmatch(key)
    if match:
        return (
            "cloud",
            f"v1/artifacts/cloud/{match['fingerprint']}",
            None,
            match["fingerprint"],
        )
    return None


def marker_identity(
    prefix: str, kind: str, train: str | None, fingerprint: str, marker: Any
) -> dict[str, Any]:
    if not isinstance(marker, dict):
        fail(f"completion marker is not an object: {prefix}")
    generation = marker.get("generation")
    inputs = marker.get("inputs")
    if (
        marker.get("fingerprint") != fingerprint
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or not isinstance(inputs, dict)
    ):
        fail(f"completion marker conflicts with its artifact identity: {prefix}")
    if kind in {"system", "packages"}:
        if (
            marker.get("schema_version") != "freesense.artifact/v1"
            or marker.get("stage") != kind
        ):
            fail(f"repository completion marker has an invalid schema: {prefix}")
        if kind == "system" and inputs.get("system") != fingerprint:
            fail(f"System completion marker has an invalid closure: {prefix}")
        if kind == "packages" and inputs.get("package_train") != train:
            fail(f"Packages completion marker has an invalid train: {prefix}")
    elif kind == "iso":
        schema = marker.get("schema_version")
        legacy = schema == "freesense.iso/v1" and inputs.get("packages") is None
        current = schema == "freesense.iso/v2" and SHA256.fullmatch(
            str(inputs.get("packages", ""))
        )
        if (
            not (legacy or current)
            or not SHA256.fullmatch(str(marker.get("system", "")))
            or inputs.get("channel") not in {"stable", "devel"}
        ):
            fail(f"ISO completion marker has an invalid closure: {prefix}")
    else:
        files = marker.get("files")
        if (
            marker.get("schema_version") != "freesense.cloud-image/v1"
            or not SHA256.fullmatch(str(marker.get("bundle_fingerprint", "")))
            or marker.get("channel") not in {"stable", "devel"}
            or not isinstance(files, list)
            or {item.get("format") for item in files if isinstance(item, dict)}
            != {"qcow2", "raw"}
            or not SHA256.fullmatch(str(inputs.get("system", "")))
            or not SHA256.fullmatch(str(inputs.get("packages", "")))
        ):
            fail(f"cloud completion marker has an invalid closure: {prefix}")
    return {
        "prefix": prefix,
        "kind": kind,
        "train": train,
        "fingerprint": fingerprint,
        "generation": generation,
        "marker": marker,
    }


def collect_sha256(value: Any, output: set[str]) -> None:
    if isinstance(value, str):
        if SHA256.fullmatch(value):
            output.add(value)
        elif re.fullmatch(r"inputs/sha256/[0-9a-f]{64}", value):
            output.add(value.rsplit("/", 1)[1])
    elif isinstance(value, dict):
        for item in value.values():
            collect_sha256(item, output)
    elif isinstance(value, list):
        for item in value:
            collect_sha256(item, output)


def component_prefix(component: Any, kind: str, package_train: str) -> str | None:
    if not isinstance(component, dict):
        return None
    fingerprint = component.get("fingerprint")
    if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint):
        fail(f"signed {kind} channel component has an invalid fingerprint")
    if kind == "system":
        return f"v1/artifacts/system/{fingerprint}"
    return f"v1/artifacts/packages/{package_train}/{fingerprint}"


def candidate(
    bucket: str,
    prefix: str,
    reason: str,
    objects: list[dict[str, Any]],
) -> dict[str, Any]:
    keys = sorted(item["key"] for item in objects)
    return {
        "bucket": bucket,
        "prefix": prefix,
        "reason": reason,
        "object_count": len(keys),
        "bytes": sum(item["size"] for item in objects),
        "last_modified": max(parse_time(item["last_modified"]) for item in objects)
        .isoformat()
        .replace("+00:00", "Z"),
        "keys": keys,
    }


def candidate_totals(candidates: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "candidate_prefixes": len(candidates),
        "candidate_objects": sum(item["object_count"] for item in candidates),
        "candidate_bytes": sum(item["bytes"] for item in candidates),
    }


def candidate_group_digest(candidate: dict[str, Any]) -> str:
    bucket = candidate.get("bucket")
    prefix = candidate.get("prefix")
    keys = candidate.get("keys")
    if (
        not isinstance(bucket, str)
        or not bucket
        or not isinstance(prefix, str)
        or not prefix.startswith("v1/")
        or not isinstance(keys, list)
        or not keys
        or any(not isinstance(key, str) or not key.startswith("v1/") for key in keys)
    ):
        fail("retention report has an invalid candidate group")
    identity = {"bucket": bucket, "prefix": prefix, "keys": sorted(keys)}
    return hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def state_candidate_groups(previous: Any) -> list[str] | None:
    if not isinstance(previous, dict) or previous.get("schema_version") != STATE_SCHEMA:
        return None
    groups = previous.get("candidate_groups_sha256")
    if groups is None:
        return None
    if (
        not isinstance(groups, list)
        or len(groups) != len(set(groups))
        or any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in groups)
    ):
        fail("previous retention state has invalid candidate groups")
    return groups


def select_deletion_batch(
    candidates: list[dict[str, Any]],
    max_objects: int = MAX_DELETE_OBJECTS,
    max_bytes: int = MAX_DELETE_BYTES,
    previous_groups: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            parse_time(item["last_modified"]),
            item["bucket"],
            item["prefix"],
        ),
    )
    indexed = {candidate_group_digest(item): item for item in ordered}
    if len(indexed) != len(ordered):
        fail("retention candidates contain duplicate groups")
    if previous_groups and all(value in indexed for value in previous_groups):
        selected = [indexed[value] for value in previous_groups]
        selected_groups = set(previous_groups)
        deferred = [
            item
            for item in ordered
            if candidate_group_digest(item) not in selected_groups
        ]
        for bucket in {item["bucket"] for item in selected}:
            bucket_candidates = [item for item in selected if item["bucket"] == bucket]
            totals = candidate_totals(bucket_candidates)
            if (
                totals["candidate_objects"] > max_objects
                or totals["candidate_bytes"] > max_bytes
            ):
                fail("previous retention batch exceeds the per-run safety cap")
        return selected, deferred

    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    usage: dict[str, dict[str, int]] = defaultdict(
        lambda: {"objects": 0, "bytes": 0}
    )
    blocked_buckets: set[str] = set()
    for item in ordered:
        bucket = item["bucket"]
        objects = item["object_count"]
        size = item["bytes"]
        if objects > max_objects or size > max_bytes:
            fail("retention candidate group exceeds the per-run safety cap")
        bucket_usage = usage[bucket]
        exceeds_batch = (
            bucket_usage["objects"] + objects > max_objects
            or bucket_usage["bytes"] + size > max_bytes
        )
        if bucket in blocked_buckets or exceeds_batch:
            blocked_buckets.add(bucket)
            deferred.append(item)
            continue
        selected.append(item)
        bucket_usage["objects"] += objects
        bucket_usage["bytes"] += size
    return selected, deferred


def plan_retention(
    build: dict[str, Any],
    downloads: dict[str, Any],
    manifest: dict[str, Any],
    pinned_inputs: set[str],
    now: datetime,
    keep_devel: int,
    grace: timedelta,
    completed_grace: timedelta = timedelta(0),
    smoke_keep: int = 1,
    stable_train: str | None = None,
    development_train: str | None = None,
) -> dict[str, Any]:
    if keep_devel < 1:
        fail("Development retention must keep at least one completed build")
    if now.tzinfo is None:
        fail("retention planning time must have a timezone")
    now = now.astimezone(timezone.utc)
    if grace < timedelta(0) or completed_grace < timedelta(0):
        fail("retention grace periods cannot be negative")
    if smoke_keep < 1:
        fail("retention must keep at least one smoke marker per bucket")
    channels = manifest.get("channels", {})
    if stable_train is None:
        stable_train = channels.get("stable", {}).get("package_train")
    if development_train is None:
        development_train = channels.get("devel", {}).get("package_train")
    for name, train in (("Stable", stable_train), ("Development", development_train)):
        if train is not None and (
            not isinstance(train, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", train)
        ):
            fail(f"retention has an invalid {name} train policy")
    orphan_cutoff = now - grace
    completed_cutoff = now - completed_grace
    for inventory, kind in ((build, "build"), (downloads, "downloads")):
        if (
            inventory.get("schema_version") != INVENTORY_SCHEMA
            or inventory.get("kind") != kind
            or not isinstance(inventory.get("objects"), list)
            or not isinstance(inventory.get("documents"), dict)
        ):
            fail(f"invalid {kind} inventory")

    build_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_identity: dict[str, tuple[str, str | None, str]] = {}
    warnings: list[str] = []
    for item in build["objects"]:
        key = item.get("key")
        if not isinstance(key, str):
            fail("build inventory contains an object without a key")
        classified = classify_artifact_key(key)
        if classified is None:
            continue
        kind, prefix, train, fingerprint = classified
        build_groups[prefix].append(item)
        group_identity[prefix] = (kind, train, fingerprint)

    markers: dict[str, dict[str, Any]] = {}
    for prefix, objects in build_groups.items():
        marker_key = prefix + "/complete.json"
        if not any(item["key"] == marker_key for item in objects):
            continue
        document = build["documents"].get(marker_key)
        if document is None:
            fail(f"inventory omitted completion marker {marker_key}")
        kind, train, fingerprint = group_identity[prefix]
        markers[prefix] = marker_identity(prefix, kind, train, fingerprint, document)

    protected_prefixes: set[str] = set()
    devel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in markers.values():
        inputs = entry["marker"]["inputs"]
        if entry["kind"] == "system":
            train = inputs.get("package_train")
            if train == stable_train:
                protected_prefixes.add(entry["prefix"])
            elif train == development_train:
                devel["system"].append(entry)
            else:
                protected_prefixes.add(entry["prefix"])
                warnings.append(
                    f"protected System artifact with unknown package train at {entry['prefix']}"
                )
        elif entry["kind"] == "packages":
            if entry["train"] == stable_train:
                protected_prefixes.add(entry["prefix"])
            elif entry["train"] == development_train:
                devel["packages"].append(entry)
            else:
                protected_prefixes.add(entry["prefix"])
                warnings.append(
                    f"protected Packages artifact with unknown train at {entry['prefix']}"
                )
        elif inputs.get("channel") == "stable":
            protected_prefixes.add(entry["prefix"])
        elif entry["kind"] == "iso":
            devel["iso"].append(entry)
        else:
            devel["cloud"].append(entry)

    entries = devel["system"]
    entries.sort(key=lambda item: (item["generation"], item["prefix"]), reverse=True)
    protected_prefixes.update(item["prefix"] for item in entries[:keep_devel])

    # ISO and every filesystem-specific cloud result form one release bundle.
    # Retain complete generations as a unit instead of counting cloud variants
    # independently.
    bundles: dict[str, dict[str, Any]] = {}
    legacy_images = []
    for entry in devel["iso"] + devel["cloud"]:
        bundle = entry["marker"].get("bundle_fingerprint")
        if not isinstance(bundle, str) or not SHA256.fullmatch(bundle):
            legacy_images.append(entry)
            continue
        group = bundles.setdefault(bundle, {
            "generation": entry["generation"], "entries": [],
        })
        group["generation"] = max(group["generation"], entry["generation"])
        group["entries"].append(entry)
    ordered_bundles = sorted(
        bundles.values(),
        key=lambda item: (
            item["generation"],
            sorted(entry["prefix"] for entry in item["entries"]),
        ),
        reverse=True,
    )
    for group in ordered_bundles[:keep_devel]:
        protected_prefixes.update(entry["prefix"] for entry in group["entries"])
    legacy_images.sort(
        key=lambda item: (item["generation"], item["prefix"]), reverse=True
    )
    protected_prefixes.update(
        item["prefix"] for item in legacy_images[:keep_devel]
    )

    channels = manifest.get("channels")
    if not isinstance(channels, dict):
        fail("signed repository manifest has no channels")
    for channel_name in ("stable", "devel"):
        channel = channels.get(channel_name)
        if channel is None:
            continue
        if not isinstance(channel, dict):
            fail(f"signed {channel_name} channel is invalid")
        train = channel.get("package_train")
        if not isinstance(train, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", train):
            fail(f"signed {channel_name} channel has an invalid package train")
        for kind in ("system", "packages"):
            prefix = component_prefix(channel.get(kind), kind, train)
            if prefix is not None:
                protected_prefixes.add(prefix)

    legacy_development_iso = False
    for prefix in list(protected_prefixes):
        entry = markers.get(prefix)
        if entry is None or entry["kind"] != "iso":
            continue
        inputs = entry["marker"]["inputs"]
        if inputs.get("channel") != "devel":
            continue
        package_train = inputs.get("package_train")
        packages = inputs.get("packages")
        if isinstance(packages, str) and SHA256.fullmatch(packages):
            if not isinstance(package_train, str) or not re.fullmatch(
                r"[0-9]+\.[0-9]+", package_train
            ):
                fail(f"retained ISO has an invalid package train: {prefix}")
            protected_prefixes.add(
                f"v1/artifacts/packages/{package_train}/{packages}"
            )
        else:
            legacy_development_iso = True

    if legacy_development_iso:
        protected_prefixes.update(item["prefix"] for item in devel["packages"])
        warnings.append(
            "retained legacy Development ISO has no Packages fingerprint; "
            f"protected all {development_train} package repositories"
        )

    changed = True
    while changed:
        changed = False
        for prefix in list(protected_prefixes):
            entry = markers.get(prefix)
            if entry is None:
                continue
            marker = entry["marker"]
            references: list[str] = []
            if entry["kind"] == "packages":
                for name in ("built_against_system", "system"):
                    value = marker["inputs"].get(name)
                    if isinstance(value, str) and SHA256.fullmatch(value):
                        references.append(f"v1/artifacts/system/{value}")
            if entry["kind"] in {"iso", "cloud"}:
                image_system = marker.get("system", marker["inputs"].get("system"))
                if isinstance(image_system, str) and SHA256.fullmatch(image_system):
                    references.append(f"v1/artifacts/system/{image_system}")
                packages = marker["inputs"].get("packages")
                train = marker["inputs"].get("package_train")
                if isinstance(packages, str) and SHA256.fullmatch(packages) and train:
                    references.append(f"v1/artifacts/packages/{train}/{packages}")
            for reference in references:
                if reference not in protected_prefixes:
                    protected_prefixes.add(reference)
                    changed = True

    protected_inputs = set(pinned_inputs)
    for prefix in protected_prefixes:
        entry = markers.get(prefix)
        if entry is not None:
            collect_sha256(entry["marker"], protected_inputs)

    build_candidates: list[dict[str, Any]] = []
    for prefix, objects in sorted(build_groups.items()):
        if prefix in protected_prefixes:
            continue
        newest = max(parse_time(item["last_modified"]) for item in objects)
        cutoff = completed_cutoff if prefix in markers else orphan_cutoff
        if newest > cutoff:
            continue
        reason = "expired completed Development artifact"
        if prefix not in markers:
            reason = "abandoned incomplete artifact"
        build_candidates.append(
            candidate(build["bucket"], prefix + "/", reason, objects)
        )

    for item in build["objects"]:
        key = item["key"]
        match = re.fullmatch(r"v1/inputs/sha256/([0-9a-f]{64})", key)
        if match is None or match[1] in protected_inputs:
            continue
        if parse_time(item["last_modified"]) <= orphan_cutoff:
            build_candidates.append(
                candidate(
                    build["bucket"],
                    key,
                    "unreferenced immutable input",
                    [item],
                )
            )

    download_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    protected_downloads: set[str] = set()
    download_generations: dict[str, int] = {}
    for item in downloads["objects"]:
        key = item["key"]
        if key.startswith("v1/releases/stable/"):
            protected_downloads.add("v1/releases/stable/")
            continue
        match = DEVEL_DOWNLOAD.match(key)
        if match:
            prefix = f"v1/releases/devel/{match['release']}/"
            download_groups[prefix].append(item)
            download_generations[prefix] = int(match["generation"])
        elif key.startswith("v1/releases/devel/"):
            protected_downloads.add(key)
            warnings.append(f"protected unrecognized Development download object {key}")
        elif key.startswith("v1/smoke/broker/"):
            continue
        elif key.startswith("v1/"):
            protected_downloads.add(key)

    ordered_downloads = sorted(
        download_groups,
        key=lambda prefix: (download_generations[prefix], prefix),
        reverse=True,
    )
    protected_downloads.update(ordered_downloads[:keep_devel])
    devel_release = build["documents"].get("v1/releases/devel.json")
    if devel_release is not None:
        if not isinstance(devel_release, dict) or devel_release.get("channel") != "devel":
            fail("Development release document is invalid")
        release_id = devel_release.get("release_id")
        if not isinstance(release_id, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+-g[1-9][0-9]*", release_id
        ):
            fail("Development release document has an invalid release ID")
        protected_downloads.add(f"v1/releases/devel/{release_id}/")

    download_candidates: list[dict[str, Any]] = []
    for prefix, objects in sorted(download_groups.items()):
        if prefix in protected_downloads:
            continue
        if max(parse_time(item["last_modified"]) for item in objects) <= completed_cutoff:
            download_candidates.append(
                candidate(
                    downloads["bucket"],
                    prefix,
                    "expired Development download",
                    objects,
                )
            )

    smoke_candidates: list[dict[str, Any]] = []
    protected_smoke: list[str] = []
    for inventory in (build, downloads):
        smoke = sorted(
            (
                item
                for item in inventory["objects"]
                if item["key"].startswith("v1/smoke/broker/")
            ),
            key=lambda item: (parse_time(item["last_modified"]), item["key"]),
            reverse=True,
        )
        protected_smoke.extend(
            f"{inventory['bucket']}/{item['key']}" for item in smoke[:smoke_keep]
        )
        for item in smoke[smoke_keep:]:
            if parse_time(item["last_modified"]) <= completed_cutoff:
                smoke_candidates.append(
                    candidate(
                        inventory["bucket"],
                        item["key"],
                        "superseded broker smoke marker",
                        [item],
                    )
                )

    eligible_candidates = sorted(
        build_candidates + download_candidates + smoke_candidates,
        key=lambda item: (item["bucket"], item["prefix"]),
    )
    previous_groups = state_candidate_groups(
        build["documents"].get("v1/state/retention.json")
    )
    candidates, deferred_candidates = select_deletion_batch(
        eligible_candidates, previous_groups=previous_groups
    )
    return {
        "schema_version": REPORT_SCHEMA,
        "mode": "two-run-confirmation",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "policy": {
            "stable": "keep forever",
            "development_completed_per_component": keep_devel,
            "completed_candidate_age_hours": int(
                completed_grace.total_seconds() // 3600
            ),
            "orphan_candidate_age_hours": int(grace.total_seconds() // 3600),
            "smoke_markers_per_bucket": smoke_keep,
            "unknown_objects": "protect",
            "generation_records": "keep forever",
        },
        "protected": {
            "artifact_prefixes": sorted(protected_prefixes),
            "input_objects": sorted(
                f"v1/inputs/sha256/{value}" for value in protected_inputs
            ),
            "download_prefixes": sorted(protected_downloads),
            "smoke_objects": sorted(protected_smoke),
        },
        "candidates": candidates,
        "totals": candidate_totals(candidates),
        "eligible_totals": candidate_totals(eligible_candidates),
        "deferred_totals": candidate_totals(deferred_candidates),
        "warnings": sorted(set(warnings)),
    }


def candidate_digest(report: dict[str, Any]) -> str:
    if (
        report.get("schema_version") != REPORT_SCHEMA
        or report.get("mode") != "two-run-confirmation"
        or not isinstance(report.get("candidates"), list)
    ):
        fail("invalid retention report")
    identities: list[dict[str, str]] = []
    for item in report["candidates"]:
        if not isinstance(item, dict) or not isinstance(item.get("keys"), list):
            fail("retention report has an invalid candidate")
        bucket = item.get("bucket")
        if not isinstance(bucket, str) or not bucket:
            fail("retention report candidate has no bucket")
        for key in item["keys"]:
            if not isinstance(key, str) or not key.startswith("v1/"):
                fail("retention report candidate has an invalid key")
            identities.append({"bucket": bucket, "key": key})
    encoded = canonical_json(sorted(identities, key=lambda item: (item["bucket"], item["key"])))
    return hashlib.sha256(encoded.encode()).hexdigest()


def confirmation(
    report: dict[str, Any],
    previous: Any,
    now: datetime,
    minimum_interval: timedelta = timedelta(hours=20),
) -> dict[str, Any]:
    if now.tzinfo is None:
        fail("confirmation time must have a timezone")
    now = now.astimezone(timezone.utc)
    digest = candidate_digest(report)
    count = report["totals"]["candidate_objects"]
    first_observed = now
    last_observed = now
    observations = 1
    ready = False
    if previous is not None:
        if (
            not isinstance(previous, dict)
            or previous.get("schema_version") != STATE_SCHEMA
            or not SHA256.fullmatch(str(previous.get("candidate_sha256", "")))
            or not isinstance(previous.get("observations"), int)
            or previous["observations"] < 1
        ):
            fail("previous retention state is invalid")
        if previous["candidate_sha256"] == digest:
            try:
                previous_observed = parse_time(previous["last_observed_at"])
                first_observed = parse_time(previous["first_observed_at"])
            except KeyError:
                fail("previous retention state has no observation times")
            if now - previous_observed >= minimum_interval:
                observations = previous["observations"] + 1
                ready = count > 0 and observations >= 2
            else:
                observations = previous["observations"]
                last_observed = previous_observed
    state = {
        "schema_version": STATE_SCHEMA,
        "candidate_sha256": digest,
        "candidate_groups_sha256": [
            candidate_group_digest(item) for item in report["candidates"]
        ],
        "candidate_objects": count,
        "candidate_bytes": report["totals"]["candidate_bytes"],
        "observations": observations,
        "first_observed_at": first_observed.isoformat().replace("+00:00", "Z"),
        "last_observed_at": last_observed.isoformat().replace("+00:00", "Z"),
        "applied": False,
    }
    return {"ready": ready, "state": state}


def deletion_keys(
    report: dict[str, Any], bucket_kind: str, bucket: str
) -> list[str]:
    candidate_digest(report)
    if bucket_kind not in {"build", "downloads"}:
        fail("unknown retention bucket kind")
    keys: set[str] = set()
    total_bytes = 0
    for item in report["candidates"]:
        if item.get("bucket") != bucket:
            continue
        total_bytes += item.get("bytes", 0)
        for key in item["keys"]:
            if (
                "/stable/" in key
                or key.startswith("v1/state/")
                or key in DOCUMENT_KEYS
                or key == "v1/repos.manifest.json"
            ):
                fail(f"protected object entered deletion set: {key}")
            allowed = (
                key.startswith("v1/artifacts/")
                or key.startswith("v1/inputs/sha256/")
                or key.startswith("v1/smoke/broker/")
                if bucket_kind == "build"
                else key.startswith("v1/releases/devel/")
                or key.startswith("v1/smoke/broker/")
            )
            if not allowed:
                fail(f"object is outside the {bucket_kind} deletion boundary: {key}")
            keys.add(key)
    if len(keys) > MAX_DELETE_OBJECTS or total_bytes > MAX_DELETE_BYTES:
        fail("retention deletion exceeds the per-run safety cap")
    return sorted(keys)


def delete_report_keys(
    report: dict[str, Any], bucket_kind: str, bucket: str, endpoint: str
) -> int:
    keys = deletion_keys(report, bucket_kind, bucket)
    for offset in range(0, len(keys), 1000):
        batch = keys[offset : offset + 1000]
        with tempfile.TemporaryDirectory(prefix="freesense-delete.") as directory:
            request = Path(directory) / "delete.json"
            request.write_text(
                json.dumps(
                    {"Objects": [{"Key": key} for key in batch], "Quiet": False}
                ),
                encoding="utf-8",
            )
            response = aws(
                "s3api",
                "delete-objects",
                "--bucket",
                bucket,
                "--delete",
                f"file://{request}",
                "--endpoint-url",
                endpoint,
                "--output",
                "json",
            )
        errors = response.get("Errors", [])
        deleted = {item.get("Key") for item in response.get("Deleted", [])}
        if errors or deleted != set(batch):
            fail(
                f"R2 did not confirm the exact {bucket_kind} deletion batch"
            )
    return len(keys)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read JSON from {path}: {error}")


def pinned_inputs(config: Path) -> set[str]:
    values: set[str] = set()
    for path in sorted(config.rglob("*.json")):
        collect_sha256(load_json(path), values)
    return values


def markdown_summary(report: dict[str, Any]) -> str:
    totals = report["totals"]
    eligible = report["eligible_totals"]
    deferred = report["deferred_totals"]
    lines = [
        "## R2 retention report",
        "",
        "**Guarded cleanup:** candidates require two matching daily observations.",
        "",
        f"- Actionable prefixes/objects: {totals['candidate_prefixes']} / "
        f"{totals['candidate_objects']}",
        f"- Actionable storage: {totals['candidate_bytes'] / (1024**3):.2f} GiB",
        f"- Eligible prefixes/objects: {eligible['candidate_prefixes']} / "
        f"{eligible['candidate_objects']}",
        f"- Eligible storage: {eligible['candidate_bytes'] / (1024**3):.2f} GiB",
        f"- Deferred prefixes/objects: {deferred['candidate_prefixes']} / "
        f"{deferred['candidate_objects']}",
        f"- Deferred storage: {deferred['candidate_bytes'] / (1024**3):.2f} GiB",
        f"- Protected artifact prefixes: "
        f"{len(report['protected']['artifact_prefixes'])}",
        f"- Protected input objects: {len(report['protected']['input_objects'])}",
        f"- Warnings: {len(report['warnings'])}",
        "",
        "The exact candidate keys are printed in the workflow log.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--bucket", required=True)
    inventory_parser.add_argument("--endpoint", required=True)
    inventory_parser.add_argument("--kind", choices=("build", "downloads"), required=True)
    inventory_parser.add_argument("--output", type=Path, required=True)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--build-inventory", type=Path, required=True)
    plan_parser.add_argument("--download-inventory", type=Path, required=True)
    plan_parser.add_argument("--public-key", type=Path, required=True)
    plan_parser.add_argument("--config", type=Path, required=True)
    plan_parser.add_argument("--keep-devel", type=int, default=4)
    plan_parser.add_argument("--orphan-grace-hours", type=int, default=168)
    plan_parser.add_argument("--completed-grace-hours", type=int, default=0)
    plan_parser.add_argument("--keep-smoke", type=int, default=1)
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--github-summary", type=Path)

    confirm_parser = subparsers.add_parser("confirm")
    confirm_parser.add_argument("--report", type=Path, required=True)
    confirm_parser.add_argument("--previous", type=Path)
    confirm_parser.add_argument("--output", type=Path, required=True)
    confirm_parser.add_argument("--github-output", type=Path)

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("--report", type=Path, required=True)
    delete_parser.add_argument(
        "--bucket-kind", choices=("build", "downloads"), required=True
    )
    delete_parser.add_argument("--bucket", required=True)
    delete_parser.add_argument("--endpoint", required=True)

    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    if args.command == "inventory":
        result = snapshot(args.bucket, args.endpoint, args.kind, now)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return

    if args.command == "confirm":
        previous = None
        if args.previous is not None and args.previous.exists():
            previous = load_json(args.previous)
        decision = confirmation(load_json(args.report), previous, now)
        args.output.write_text(
            json.dumps(decision, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if args.github_output is not None:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"ready={str(decision['ready']).lower()}\n")
        return

    if args.command == "delete":
        deleted = delete_report_keys(
            load_json(args.report), args.bucket_kind, args.bucket, args.endpoint
        )
        print(f"Deleted {deleted} exact {args.bucket_kind} R2 objects.")
        return

    build = load_json(args.build_inventory)
    downloads = load_json(args.download_inventory)
    envelope = build.get("documents", {}).get("v1/repos.manifest.json")
    if envelope is None:
        fail("build inventory has no signed repository manifest")
    manifest = verify_manifest(envelope, args.public_key)
    policy = load_json(args.config / "build-policy.json")
    release_policy = policy.get("release", {})
    stable_train = release_policy.get("stable_train")
    development_train = release_policy.get("development_train")
    if (not isinstance(stable_train, str) or not isinstance(development_train, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+", stable_train)
            or not re.fullmatch(r"[0-9]+\.[0-9]+", development_train)
            or stable_train == development_train):
        fail("build policy has invalid release trains")
    report = plan_retention(
        build,
        downloads,
        manifest,
        pinned_inputs(args.config),
        now,
        args.keep_devel,
        timedelta(hours=args.orphan_grace_hours),
        timedelta(hours=args.completed_grace_hours),
        args.keep_smoke,
        stable_train,
        development_train,
    )
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if args.github_summary is not None:
        with args.github_summary.open("a", encoding="utf-8") as summary:
            summary.write(markdown_summary(report))


if __name__ == "__main__":
    main()
