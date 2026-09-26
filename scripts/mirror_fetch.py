#!/usr/bin/env python3
"""Materialise a frozen upstream mirror from its sealed plan.

Runs inside the native worker. Every package named in the plan is downloaded
from FreeBSD's repository and verified against the checksum and size recorded in
the signed catalogue at pin time -- so a package that changed underneath us, or
a truncated transfer, fails here rather than reaching a repository we sign.

The catalogue itself is generated afterwards by `pkg repo`, which is also what
signs it. Nothing here reimplements repository metadata: a hand-rolled catalogue
that FreeSense's own tooling accepts could still be one a pkg client rejects.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import urllib.request

from multiarch_pin import ARCHES, digest
from resolve_worker_tools import _safe_remote_path, verify_download

UPSTREAM = "https://pkg.freebsd.org"


def package_url(abi: str, upstream_path: str, *, base_url: str = UPSTREAM) -> str:
    if not base_url.startswith("https://"):
        raise ValueError("the mirror source must be https")
    if abi not in ARCHES.values():
        raise ValueError("unknown ABI")
    return f"{base_url.rstrip('/')}/{abi}/latest/{_safe_remote_path(upstream_path)}"


def download(url: str, destination: Path) -> None:
    """Stream one package to disk. Kept separate so tests can replace it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    with urllib.request.urlopen(url, timeout=300) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    partial.replace(destination)


def fetch(plan: dict, destination: Path, *, base_url: str = UPSTREAM, fetch_one=download) -> dict:
    if plan.get("schema_version") != "freesense.mirror-plan/v1":
        raise ValueError("invalid mirror plan")
    abi = plan.get("abi")
    packages = plan.get("packages")
    if abi not in ARCHES.values() or not isinstance(packages, list) or not packages:
        raise ValueError("mirror plan names no packages for a known ABI")
    if plan.get("fingerprint") != digest({key: value for key, value in plan.items() if key != "fingerprint"}):
        raise ValueError("mirror plan fingerprint does not match its contents")

    (destination / "All").mkdir(parents=True, exist_ok=True)
    recorded, seen = [], set()
    for package in packages:
        name = package["file"]
        if name in seen or not name.startswith("All/"):
            raise ValueError(f"mirror plan repeats or misplaces a package: {name}")
        seen.add(name)
        target = destination / name
        fetch_one(package_url(abi, package["upstream_path"], base_url=base_url), target)
        sha256 = verify_download(target, package["upstream_checksum"], package["size"])
        recorded.append({
            "name": package["name"], "version": package["version"], "origin": package["origin"],
            "abi": abi, "file": name, "sha256": sha256, "size": package["size"],
            "upstream_checksum": package["upstream_checksum"],
            "upstream_path": package["upstream_path"],
            "catalog_sha256": package["catalog_sha256"],
        })

    extra = sorted(path.name for path in (destination / "All").iterdir()
                   if f"All/{path.name}" not in seen)
    if extra:
        raise ValueError(f"the mirror directory holds packages the plan does not name: {extra}")
    return {
        "schema_version": "freesense.mirror-provenance/v1",
        "abi": abi,
        "architecture": plan["architecture"],
        "ports_commit": plan["ports_commit"],
        "catalog_sha256": plan["catalog_sha256"],
        "plan_fingerprint": plan["fingerprint"],
        "signed_catalog_verified": True,
        "packages": recorded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--base-url", default=UPSTREAM)
    args = parser.parse_args()
    provenance = fetch(json.loads(args.plan.read_text(encoding="utf-8")), args.destination,
                       base_url=args.base_url)
    args.provenance.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
    print(f"mirrored {len(provenance['packages'])} packages for {provenance['abi']}")


if __name__ == "__main__":
    main()
