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
import re

from multiarch_pin import ARCHES, digest
from resolve_worker_tools import _safe_name, _safe_origin, _safe_remote_path, _safe_version, parse_checksum

REQUIRED_MIRROR_PACKAGES = ("pkg",)
SUFFIX = re.compile(r"^-[a-z0-9]+$")


def policy(document: dict) -> dict:
    if document.get("schema_version") != "freesense.mirror-policy/v1":
        raise ValueError("invalid mirror policy")
    if not SUFFIX.fullmatch(str(document.get("delta_suffix", ""))):
        raise ValueError("mirror policy has no usable delta suffix")
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


def component_roots(delta: dict) -> dict[str, list[str]]:
    """Split the delta's port origins between the two build stages.

    delta_closure records which component each delta package belongs to, but as
    package NAMES; a Poudriere bulk list is addressed by ORIGIN. Resolving that
    here, against the same document the membership decision came from, is what
    lets the System stage build System's roots and the Optional stage build
    Optional's -- rather than each one compiling the whole delta because the
    flat root list cannot tell them apart.

    An origin may legitimately appear in both: a package in both closures is
    built once by System and taken from the System repository by Optional.

    These are ROOTS, so they are a subset of delta_roots rather than a
    partition of it. The difference is the reverse-dependency cascade -- ports
    like devel/doxygen and print/texlive-base that are in our layer only
    because something we customize reaches them. They are nobody's root, and
    Poudriere builds them when a root needs them, so putting them in a bulk
    list would only make each stage build the other stage's cascade.
    """
    origins = {item["name"]: item["origin"] for item in delta["build"]}
    components = delta.get("components")
    if not isinstance(components, dict) or set(components) != {"system", "optional"}:
        raise ValueError("the delta closure does not name both components")
    resolved = {}
    for component, names in components.items():
        missing = [name for name in names if name not in origins]
        if missing:
            raise ValueError(f"{component} names packages outside the delta: {sorted(missing)[:5]}")
        resolved[component] = sorted({origins[name] for name in names})
    covered = set(resolved["system"]) | set(resolved["optional"])
    if not covered <= set(origins.values()):
        raise ValueError("the component split names origins outside the delta")
    if origins and not covered:
        # Nothing would be built by either stage although the delta is not
        # empty, so every delta package is cascade with no root reaching it.
        raise ValueError("the delta has packages but the component split is empty")
    return resolved


def unclaimed(names: set[str], upstream: dict, suffix: str) -> None:
    """Fail unless upstream claims neither shape of the names we are about to use.

    Renaming our layer with PKGNAMESUFFIX is only a separation if the renamed
    name is free. It is also only a separation if upstream publishes nothing
    called FreeSense*, because FreeSense's own ports are deliberately left
    unsuffixed -- exact names are load-bearing across the product.

    Measured against the live catalogues (37,908 amd64 / 35,276 aarch64): no
    FreeSense* package exists on either, and four packages already end in -fs
    (R-cran-fs, py312-fs, rubygem-chef-winrm-fs, rubygem-winrm-fs), two of them
    alongside their own unsuffixed stem. None is in the delta, so this is checked
    against the names we actually rename rather than against the suffix: a
    catalogue-wide ban on the suffix would refuse today's mirror over packages
    we never touch.
    """
    taken = sorted(name for name in names if name + suffix in upstream)
    if taken:
        raise ValueError(f"upstream already publishes the renamed packages: {taken}")
    branded = sorted(name for name in upstream if name.startswith("FreeSense"))
    if branded:
        raise ValueError(f"upstream publishes FreeSense-named packages: {branded}")


def plan(delta: dict, catalogue: list[dict], *, architecture: str, catalog_sha256: str,
         ports_commit: str, ceilings: dict, suffix: str) -> dict:
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
    unclaimed(build | {item["name"] for item in delta["collisions"]}, upstream, suffix)
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
        "component_roots": component_roots(delta),
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
    rules = json.loads(args.policy.read_text(encoding="utf-8"))
    catalogue = [json.loads(line) for line in
                 args.catalogue.read_text(encoding="utf-8").splitlines() if line.strip()]
    document = plan(
        json.loads(args.delta.read_text(encoding="utf-8")), catalogue,
        architecture=args.architecture, catalog_sha256=args.catalog_sha256,
        ports_commit=args.ports_commit,
        ceilings=policy(rules), suffix=rules["delta_suffix"])
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
