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


def score_candidates(candidates: list[dict]) -> dict:
    """Choose the best common ports revision without architecture bias.

    Candidate reports are produced after applying the normal binary-seed
    eligibility checks to both signed catalogues.  The tuple is deliberately
    explicit so reruns select identical input even if report ordering differs.
    """
    if not candidates:
        raise ValueError("no ports candidates in the pin window")
    ranked = []
    for candidate in candidates:
        commit = candidate.get("commit")
        accepted = candidate.get("accepted")
        committed_at = candidate.get("committed_at")
        if (not re.fullmatch(r"[0-9a-f]{40}", str(commit)) or
                not isinstance(accepted, dict) or set(accepted) != set(TARGETS) or
                any(type(accepted[a]) is not int or accepted[a] < 0 for a in TARGETS) or
                not isinstance(committed_at, str)):
            raise ValueError("invalid ports candidate evidence")
        try:
            stamp = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("invalid ports candidate timestamp") from error
        if stamp.tzinfo is None:
            raise ValueError("ports candidate timestamp has no timezone")
        rust = candidate.get("rust_official") or {arch: True for arch in TARGETS}
        if set(rust) != set(TARGETS) or any(type(rust[arch]) is not bool for arch in TARGETS):
            rust = {arch: True for arch in TARGETS}
        ranked.append((
            (int(rust["arm64"]), int(rust["amd64"]), min(accepted.values()),
             sum(accepted.values()), stamp, commit),
            candidate,
        ))
    winner = max(ranked, key=lambda item: item[0])[1]
    # Preserve full rejection evidence; callers seal this alongside the pin.
    return {**winner, "score": {
        "minimum_accepted": min(winner["accepted"].values()),
        "combined_accepted": sum(winner["accepted"].values()),
    }}


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
            "catalog_ports_commit": ports[arch],
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
    start = now.replace(microsecond=0) if security_rollover else datetime.fromisoformat(
        previous["valid_until"].replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("pin boundary has no timezone")
    start = start.astimezone(timezone.utc)
    selection = None
    candidates = metadata.get("candidate_commits")
    if candidates is not None:
        if (not isinstance(candidates, list) or not 1 <= len(candidates) <= 14 or
                any(not re.fullmatch(r"[0-9a-f]{40}", str(item.get("commit"))) or not isinstance(item.get("committed_at"), str) for item in candidates)):
            raise ValueError("invalid bounded ports candidate window")
        commits, stamps = set(), []
        for item in candidates:
            try:
                stamp = datetime.fromisoformat(item["committed_at"].replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("invalid ports candidate timestamp") from error
            if stamp.tzinfo is None:
                raise ValueError("ports candidate timestamp has no timezone")
            stamp = stamp.astimezone(timezone.utc)
            if item["commit"] in commits or not start - timedelta(days=14) <= stamp <= start:
                raise ValueError("ports candidate is duplicated or outside the bounded window")
            commits.add(item["commit"]); stamps.append(stamp)
        if stamps != sorted(stamps, reverse=True):
            raise ValueError("ports candidates are not deterministically ordered")
        ports_commit = candidates[0]["commit"]
    elif "ports_candidates" in metadata:
        selection = score_candidates(metadata["ports_candidates"])
        ports_commit = selection["commit"]
    else:
        if len(set(ports.values())) != 1:
            raise ValueError("signed catalogues do not identify one shared ports revision")
        ports_commit = next(iter(ports.values()))
    for report in reports.values():
        report["ports_commit"] = ports_commit
    if not re.fullmatch(r"[0-9a-f]{40}", str(ports_commit)):
        raise ValueError("invalid official ports revision")
    result = {
        "schema_version": "freesense.freebsd-pin-common/v1",
        "valid_from": start.isoformat().replace("+00:00", "Z"),
        "valid_until": (start + timedelta(days=14)).isoformat().replace("+00:00", "Z"),
        "freebsd_source": {"commit": metadata["source_commit"], "osversion": metadata["osversion"]},
        "bootstrap_snapshot": {"commit": metadata["source_commit"], "revision_prefix": revision,
                               "build_date": build_date, "osversion": metadata["osversion"]},
        "freebsd_ports": {"commit": ports_commit}, "sources": sources, "targets": reports,
    }
    if selection is not None:
        result["pin_evidence"] = {"schema_version": "freesense.ports-candidate-selection/v1",
                                  "selected": selection, "candidate_count": len(metadata["ports_candidates"])}
    if candidates is not None:
        result["ports_candidates"] = candidates
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
