#!/usr/bin/env python3
"""Turn a delta classification into the frozen upstream mirror's manifest.

The mirror is the bottom layer of the published repository stack: every package
in FreeSense's dependency closure that FreeSense does not build itself, pinned to
one signed upstream catalogue and served from R2 under FreeSense's own signature.

This runs at pin time on the host. It decides *what* the mirror contains and
fails closed when the shape of the result is surprising -- an unexpectedly large
delta, an unexpectedly small mirror, or upstream drift beyond the policy ceiling.
Downloading, cataloguing and signing happen afterwards on the native worker,
which needs FreeBSD's pkg to do it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from multiarch_pin import ARCHES, digest
from resolve_worker_tools import _safe_name, _safe_origin, _safe_remote_path, _safe_version, parse_checksum

REQUIRED_MIRROR_PACKAGES = ("pkg",)


def policy(document: dict) -> dict:
    if document.get("schema_version") != "freesense.mirror-policy/v1":
        raise ValueError("invalid mirror policy")
    ceilings = document.get("ceilings")
    if not isinstance(ceilings, dict) or set(ceilings) != set(ARCHES):
        raise ValueError("mirror policy must bound every architecture")
    for value in ceilings.values():
        if (not isinstance(value, dict)
                or set(value) != {"max_delta", "min_mirror", "max_churn"}
                or any(type(bound) is not int or bound < 1 for bound in value.values())):
            raise ValueError("each architecture needs max_delta, min_mirror and max_churn")
    return ceilings


def catalogue_index(records: list[dict]) -> dict[str, dict]:
    """Index the signed catalogue by name, dropping ambiguous entries.

    The caller has already verified FreeBSD's signature over the whole
    catalogue; this only reshapes it and rejects records we could not act on.
    """
    index, ambiguous = {}, set()
    for record in records:
        name = record.get("name")
        if not isinstance(name, str):
            raise ValueError("catalogue record has no name")
        if name in index:
            ambiguous.add(name)
        index[name] = record
    for name in ambiguous:
        del index[name]
    return index


def entry(name: str, upstream: dict, abi: str, catalog_sha256: str) -> dict:
    """Describe one mirrored package precisely enough to fetch and verify it."""
    checksum = upstream.get("sum")
    parse_checksum(checksum)
    size = upstream.get("pkgsize")
    if type(size) is not int or size < 1:
        raise ValueError(f"upstream package has no usable size: {name}")
    return {
        "name": _safe_name(name, "mirror package name"),
        "version": _safe_version(upstream.get("version"), "mirror package version"),
        "origin": _safe_origin(upstream.get("origin"), "mirror package origin"),
        "abi": abi,
        "file": f"All/{name}-{upstream['version']}.pkg",
        "upstream_path": _safe_remote_path(upstream.get("repopath")),
        "upstream_checksum": checksum,
        "size": size,
        "catalog_sha256": catalog_sha256,
    }


def plan(delta: dict, catalogue: list[dict], *, architecture: str, catalog_sha256: str,
         ports_commit: str, ceilings: dict) -> dict:
    if delta.get("schema_version") != "freesense.delta-closure/v1":
        raise ValueError("invalid delta closure document")
    abi = ARCHES[architecture]
    if delta.get("abi") != abi:
        raise ValueError("delta closure is for a different architecture")
    upstream = catalogue_index(catalogue)
    build = {item["name"] for item in delta["build"]}
    take = set(delta["take"])
    if take & build:
        raise ValueError("delta closure claims the same package on both layers")
    unpublished = sorted(take - set(upstream))
    if unpublished:
        raise ValueError(f"the closure needs packages upstream does not publish: {unpublished}")
    mirror = [entry(name, upstream[name], abi, catalog_sha256) for name in sorted(take)]

    bound = ceilings[architecture]
    if len(build) > bound["max_delta"]:
        raise ValueError(f"{architecture} delta of {len(build)} exceeds the {bound['max_delta']} ceiling")
    if len(mirror) < bound["min_mirror"]:
        raise ValueError(f"{architecture} mirror of {len(mirror)} is below the {bound['min_mirror']} floor")
    if len(delta["churn"]) > bound["max_churn"]:
        raise ValueError(
            f"{architecture} upstream drift of {len(delta['churn'])} packages exceeds the "
            f"{bound['max_churn']} ceiling; the observed ports commit does not match what was published")
    missing = [name for name in REQUIRED_MIRROR_PACKAGES if name not in {item["name"] for item in mirror}]
    if missing:
        raise ValueError(f"mirror is missing packages the build cannot start without: {missing}")

    document = {
        "schema_version": "freesense.mirror-plan/v1",
        "abi": abi,
        "architecture": architecture,
        "ports_commit": ports_commit,
        "catalog_sha256": catalog_sha256,
        "delta_sha256": digest(delta),
        "packages": mirror,
        "delta_roots": sorted({item["origin"] for item in delta["build"]}),
        "collisions": delta["collisions"],
        "counts": {"mirror": len(mirror), "delta": len(build), "churn": len(delta["churn"])},
    }
    document["fingerprint"] = digest(document)
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--catalogue", type=Path, required=True,
                        help="signed upstream catalogue records, one JSON object per line")
    parser.add_argument("--architecture", choices=tuple(ARCHES), required=True)
    parser.add_argument("--catalog-sha256", required=True)
    parser.add_argument("--ports-commit", required=True)
    parser.add_argument("--policy", type=Path, default=Path("config/mirror-policy.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalogue = [json.loads(line) for line in
                 args.catalogue.read_text(encoding="utf-8").splitlines() if line.strip()]
    document = plan(
        json.loads(args.delta.read_text(encoding="utf-8")), catalogue,
        architecture=args.architecture, catalog_sha256=args.catalog_sha256,
        ports_commit=args.ports_commit,
        ceilings=policy(json.loads(args.policy.read_text(encoding="utf-8"))))
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
