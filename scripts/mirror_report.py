#!/usr/bin/env python3
"""Compute the delta and the mirror manifest from a pin's own evidence.

Reporting only: it writes documents and publishes nothing. Its purpose is to run
the layered-repository decision against real pin-time data -- the requirements
the native workers evaluated and the catalogue FreeBSD signed -- so the numbers
can be checked before anything depends on them.

It also answers the question the layered design rests on: whether the ports
commit can simply be *observed* from the signed catalogue rather than chosen.
`selection` records whether the commit upstream says it built from was among the
candidates the workers evaluated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import delta_closure
import mirror_plan
from binary_seed import verify_catalogue
from multiarch_pin import ARCHES
from resolve_worker_tools import resolve_worker_tools


def observed_commit(records: list[dict], architecture: str) -> str:
    """The ports revision upstream's newest package generation was built from."""
    return resolve_worker_tools((json.dumps(record) for record in records), architecture)["ports_sha"]


def select(candidates: list[dict], commit: str) -> tuple[dict, str]:
    """Return the evaluation to use and how faithful it is.

    "observed" means the ports tree was evaluated at exactly the commit upstream
    built from, so the delta is exact. "estimated" means it was not, and the
    numbers are indicative only.

    The fallback is not a fallback for production. Upstream builds from whatever
    commit its run started at, which is almost never one of the daily commits a
    candidate window samples -- so a pin that wants an exact delta has to check
    out the observed commit rather than pick a nearby one.
    """
    if not candidates:
        raise ValueError("the pin evaluated no ports candidates")
    for candidate in candidates:
        if candidate.get("commit") == commit:
            return candidate, "observed"
    return candidates[0], "estimated"


def report(candidates: list[dict], records: list[dict], make_conf: str, *,
           architecture: str, catalog_sha256: str, ceilings: dict) -> tuple[dict, dict, dict]:
    commit = observed_commit(records, architecture)
    candidate, selection = select(candidates, commit)
    delta = delta_closure.plan(candidate["requirements"], records, make_conf)
    mirror = mirror_plan.plan(delta, records, architecture=architecture,
                              catalog_sha256=catalog_sha256, ports_commit=candidate["commit"],
                              ceilings=ceilings)
    summary = {
        "schema_version": "freesense.mirror-report/v1",
        "architecture": architecture,
        "observed_ports_commit": commit,
        "evaluated_ports_commit": candidate["commit"],
        "selection": selection,
        "exact": selection == "observed",
        "mirror_fingerprint": mirror["fingerprint"],
        "mirror_bytes": sum(package["size"] for package in mirror["packages"]),
        "counts": dict(delta["counts"], mirror=mirror["counts"]["mirror"],
                       collisions=len(delta["collisions"]), churn=len(delta["churn"])),
    }
    return delta, mirror, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--trusted-key-sha256", required=True)
    parser.add_argument("--candidates", type=Path, required=True,
                        help="merged candidate requirements, newest first")
    parser.add_argument("--make-conf", type=Path, required=True)
    parser.add_argument("--architecture", choices=tuple(ARCHES), required=True)
    parser.add_argument("--policy", type=Path, default=Path("config/mirror-policy.json"))
    parser.add_argument("--output", type=Path, required=True, help="directory for the documents")
    args = parser.parse_args()
    records = verify_catalogue(args.catalog, args.trusted_key_sha256)
    with args.catalog.open("rb") as stream:
        catalog_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    delta, mirror, summary = report(
        json.loads(args.candidates.read_text(encoding="utf-8")), records,
        args.make_conf.read_text(encoding="utf-8"),
        architecture=args.architecture, catalog_sha256=catalog_sha256,
        ceilings=mirror_plan.policy(json.loads(args.policy.read_text(encoding="utf-8"))))
    args.output.mkdir(parents=True, exist_ok=True)
    for name, document in (("delta-closure", delta), ("mirror-plan", mirror), ("summary", summary)):
        (args.output / f"{name}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
