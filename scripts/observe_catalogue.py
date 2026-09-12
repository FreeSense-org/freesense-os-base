#!/usr/bin/env python3
"""Report the ports revision FreeBSD's published packages were built from.

The layered design needs the ports tree evaluated at the commit upstream
actually built from, not at one sampled nearby: a delta computed against a
different commit is an estimate. FreeBSD records the revision in each package's
annotations, so the answer is in the catalogue it signs.

This verifies that signature first and prints what the catalogue says, for a
workflow to hand to the worker before it evaluates anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from binary_seed import verify_catalogue
from multiarch_pin import ARCHES
from resolve_worker_tools import resolve_worker_tools


def observe(catalog: Path, trusted_key_sha256: str, architecture: str) -> dict:
    records = verify_catalogue(catalog, trusted_key_sha256)
    worker = resolve_worker_tools((json.dumps(record) for record in records), architecture)
    with catalog.open("rb") as stream:
        catalog_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "schema_version": "freesense.observed-catalogue/v1",
        "architecture": architecture,
        "abi": ARCHES[architecture],
        "ports_commit": worker["ports_sha"],
        "osversion": worker["osversion"],
        "catalog_sha256": catalog_sha256,
        "signature_verified": True,
        "package_count": len(records),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--trusted-key-sha256", required=True)
    parser.add_argument("--architecture", choices=tuple(ARCHES), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    document = observe(args.catalog, args.trusted_key_sha256, args.architecture)
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
