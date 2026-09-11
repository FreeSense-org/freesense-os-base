#!/usr/bin/env python3
"""Assemble a v4 pin only from two verified, already-mirrored target reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from multiarch_pin import ARCHES, blob, rollover, validate
from resolve_multiarch_pin import score_candidates


def assemble(common: dict, reports: dict[str, dict]) -> dict:
    if (common.get("schema_version") != "freesense.freebsd-pin-common/v1"
            or set(reports) != set(ARCHES)):
        raise ValueError("complete pin common data and target reports are required")
    common = dict(common)
    if all(isinstance(report.get("candidates"), list) for report in reports.values()):
        indexed = {}
        for arch in ARCHES:
            values = reports[arch]["candidates"]
            indexed[arch] = {item.get("commit"): item for item in values}
            if len(indexed[arch]) != len(values):
                raise ValueError(f"duplicate {arch} ports candidate evidence")
        expected = [item.get("commit") for item in common.get("ports_candidates", [])]
        if set(indexed["amd64"]) != set(expected) or set(indexed["arm64"]) != set(expected):
            raise ValueError("architectures did not evaluate the complete frozen candidate window")
        commits = set(expected)
        evidence = []
        for source in common.get("ports_candidates", []):
            commit = source.get("commit")
            if commit not in commits:
                raise ValueError("invalid common ports candidate")
            evidence.append({**source, "accepted": {arch: indexed[arch][commit].get("accepted_count", 0) for arch in ARCHES},
                             "rejected": {arch: {"count": indexed[arch][commit].get("rejected_count", 0),
                                                  "reasons": indexed[arch][commit].get("rejection_reasons", {}),
                                                  "error": indexed[arch][commit].get("candidate_error", "")} for arch in ARCHES}})
        selected = score_candidates(evidence)
        common["freebsd_ports"] = {"commit": selected["commit"]}
        common["pin_evidence"] = {"schema_version":"freesense.ports-candidate-selection/v1",
                                  "candidate_count":len(evidence), "selected":selected}
        for arch in ARCHES:
            chosen = indexed[arch][selected["commit"]]
            if "object" not in chosen: raise ValueError(f"selected {arch} candidate has no binary seed")
            reports[arch] = {**reports[arch], "binary_seed":chosen,
                             "evidence":{**reports[arch]["evidence"], "requirements_sha256":chosen["requirements_sha256"]}}
    candidate = {
        "schema_version": "freesense.freebsd-pin/v4",
        "valid_from": common["valid_from"],
        "valid_until": common["valid_until"],
        "freebsd_source": common["freebsd_source"],
        "bootstrap_snapshot": common["bootstrap_snapshot"],
        "freebsd_ports": common["freebsd_ports"],
        **({"pin_evidence": common["pin_evidence"]} if "pin_evidence" in common else {}),
        "targets": {},
    }
    for arch, abi in ARCHES.items():
        report = reports[arch]
        if (report.get("schema_version") != "freesense.freebsd-pin-target/v1"
                or report.get("architecture") != arch or report.get("abi") != abi
                or report.get("ready") is not True):
            raise ValueError(f"invalid {arch} pin report")
        evidence = report.get("evidence", {})
        if (evidence.get("schema_version") != "freesense.worker-evidence/v1"
                or evidence.get("architecture") != arch
                or evidence.get("boot") is not True or evidence.get("tools") is not True):
            raise ValueError(f"{arch} lacks native worker evidence")
        target = {"ready": True, "abi": abi}
        for field in ("jail_seed", "package_catalog", "binary_seed", "worker_image", "worker_tools"):
            target[field] = blob(report.get(field), f"{arch} {field}")
        if (evidence.get("worker_image_sha256") != target["worker_image"]["sha256"]
                or evidence.get("worker_tools_sha256") != target["worker_tools"]["sha256"]
                or evidence.get("requirements_sha256") != target["binary_seed"].get("requirements_sha256")):
            raise ValueError(f"{arch} evidence does not bind its immutable inputs")
        if (target["binary_seed"].get("accepted_count") != target["binary_seed"].get("package_count")
                or not isinstance(target["binary_seed"].get("rejected_count"), int)
                or not isinstance(target["binary_seed"].get("rejection_reasons"), dict)):
            raise ValueError(f"{arch} seed lacks accepted/rejected coverage evidence")
        candidate["targets"][arch] = target
    validate(candidate)
    return candidate


def verify_mirrors(candidate: dict, fsbuild: str) -> None:
    for arch in ARCHES:
        for field in ("jail_seed", "package_catalog", "binary_seed", "worker_image", "worker_tools"):
            expected = candidate["targets"][arch][field]
            raw = subprocess.check_output(
                [fsbuild, "blob", "check", "--sha256", expected["sha256"]], text=True
            )
            actual = json.loads(raw)
            if ((actual.get("key") != expected["object"] and actual.get("object") != expected["object"])
                    or actual.get("sha256") != expected["sha256"]
                    or actual.get("size") != expected["size"]):
                raise ValueError(f"mirrored {arch} {field} differs from its report")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--amd64", type=Path, required=True)
    parser.add_argument("--arm64", type=Path, required=True)
    parser.add_argument("--fsbuild", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--security-rollover", action="store_true")
    args = parser.parse_args()
    candidate = assemble(
        json.loads(args.common.read_text()),
        {arch: json.loads(getattr(args, arch).read_text()) for arch in ARCHES},
    )
    rollover(json.loads(args.previous.read_text()), candidate,
             security_rollover=args.security_rollover)
    verify_mirrors(candidate, args.fsbuild)
    args.output.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
