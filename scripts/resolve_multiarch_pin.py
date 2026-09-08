#!/usr/bin/env python3
"""Resolve one same-revision FreeBSD snapshot into shared dual-arch pin inputs."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from binary_seed import verify_catalogue
from resolve_worker_tools import resolve_worker_tools

TARGETS = {"amd64": ("amd64", "amd64"), "arm64": ("arm64", "aarch64")}


def manifest_sha(path: Path, filename: str) -> str:
    matches = [line.split("\t")[1] for line in path.read_text().splitlines()
               if line.split("\t", 1)[0] == filename]
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0]):
        raise ValueError(f"manifest has no unique {filename} digest")
    return matches[0]


def image(checksums: Path, directory_url: str, arch: str, package_arch: str,
          build_date: str, revision: str) -> dict:
    pattern = re.compile(
        rf"^SHA256 \((FreeBSD-16\.0-CURRENT-{arch}(?:-{package_arch})?-BASIC-CLOUDINIT-"
        rf"{build_date}-{revision}-[0-9]+-ufs\.qcow2\.xz)\) = ([0-9a-f]{{64}})$"
    )
    matches = [pattern.fullmatch(line) for line in checksums.read_text().splitlines()]
    matches = [match for match in matches if match]
    if len(matches) != 1:
        raise ValueError(f"no unique {arch} cloud worker image")
    return {"url": directory_url.rstrip("/") + "/" + matches[0][1],
            "sha256": matches[0][2]}


def resolve(previous: dict, directory: Path, metadata: dict, sources: dict,
            security_rollover: bool, now: datetime) -> dict:
    revision, build_date = metadata.get("revision"), metadata.get("build_date")
    if (not re.fullmatch(r"[0-9a-f]{12}", str(revision))
            or not re.fullmatch(r"[0-9]{8}", str(build_date))
            or not re.fullmatch(r"[0-9a-f]{40}", str(metadata.get("source_commit")))):
        raise ValueError("invalid shared FreeBSD snapshot identity")
    if any(not re.fullmatch(r"[0-9a-f]{40}", str(value)) for value in sources.values()):
        raise ValueError("project sources must be frozen to full commits")
    reports, ports = {}, {}
    for arch, (dist_arch, package_arch) in TARGETS.items():
        target = directory / arch
        records = verify_catalogue(target / "packagesite.pkg", metadata["trusted_key_sha256"])
        catalog_sha = hashlib.sha256((target / "packagesite.pkg").read_bytes()).hexdigest()
        worker = resolve_worker_tools((json.dumps(record) for record in records), arch)
        ports[arch] = worker["ports_sha"]
        catalog_osversion = worker["osversion"]
        max_bootstrap_osversion_delta = 2
        if (catalog_osversion < metadata["osversion"] - 1
                or catalog_osversion > metadata["osversion"] + max_bootstrap_osversion_delta):
            raise ValueError(f"{arch} catalog OSVERSION is outside the bounded bootstrap window")
        reports[arch] = {
            "abi": f"FreeBSD:16:{package_arch}",
            "jail_seed": {
                "url": metadata["dist_urls"][arch].rstrip("/") + "/base.txz",
                "sha256": manifest_sha(target / "MANIFEST", "base.txz"),
            },
            "package_catalog": {
                "url": f"https://pkg.freebsd.org/FreeBSD:16:{package_arch}/latest/packagesite.pkg",
                "sha256": catalog_sha,
            },
            "worker_image_compressed": image(
                target / "CHECKSUM.SHA256", metadata["vm_urls"][arch], dist_arch,
                package_arch, build_date, revision),
            "catalog_osversion": catalog_osversion,
        }
    ports_commit = ports.get("amd64") or next(iter(ports.values()))
    if not re.fullmatch(r"[0-9a-f]{40}", str(ports_commit)):
        raise ValueError("invalid official ports revision")
    start = now.replace(microsecond=0) if security_rollover else datetime.fromisoformat(
        previous["valid_until"].replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("pin boundary has no timezone")
    start = start.astimezone(timezone.utc)
    result = {
        "schema_version": "freesense.freebsd-pin-common/v1",
        "valid_from": start.isoformat().replace("+00:00", "Z"),
        "valid_until": (start + timedelta(days=14)).isoformat().replace("+00:00", "Z"),
        "freebsd_source": {"commit": metadata["source_commit"], "osversion": metadata["osversion"]},
        "bootstrap_snapshot": {"commit": metadata["source_commit"], "revision_prefix": revision,
                               "build_date": build_date, "osversion": metadata["osversion"]},
        "freebsd_ports": {"commit": ports_commit}, "sources": sources, "targets": reports,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--security-rollover", action="store_true")
    args = parser.parse_args()
    result = resolve(json.loads(args.previous.read_text()), args.directory,
                     json.loads(args.metadata.read_text()), json.loads(args.sources.read_text()),
                     args.security_rollover, datetime.now(timezone.utc))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
