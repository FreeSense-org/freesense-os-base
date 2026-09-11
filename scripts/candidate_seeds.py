#!/usr/bin/env python3
"""Evaluate and materialize all bounded ports candidates with one download cache."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request

from binary_seed import bundle, select, verify_catalogue
from multiarch_pin import ARCHES
from resolve_worker_tools import _load_json, _safe_remote_path, verify_download


def build_candidates(candidates: list[dict], records: list[dict], architecture: str,
                     cache: Path, output: Path, base_url: str, uploader, catalog_sha: str | None = None) -> list[dict]:
    if not candidates or len(candidates) > 14:
        raise ValueError("candidate window must contain 1..14 revisions")
    selections, rejected_candidates = {}, {}
    needed = {}
    for candidate in candidates:
        commit, requirements = candidate.get("commit"), candidate.get("requirements")
        try:
            chosen = select(requirements, records, architecture)
        except ValueError as error:
            rejected_candidates[commit] = str(error); continue
        selections[commit] = chosen
        for package in chosen["accepted"].values(): needed[package["repopath"]] = package
    (cache / "All").mkdir(parents=True, exist_ok=True)
    for remote, package in sorted(needed.items()):
        destination = cache / "All" / f"{package['name']}-{package['version']}.pkg"
        if not destination.exists():
            with urllib.request.urlopen(base_url.rstrip("/") + "/" + _safe_remote_path(remote), timeout=120) as response, destination.open("wb") as stream:
                while chunk := response.read(1024 * 1024): stream.write(chunk)
        verify_download(destination, package["sum"], package["pkgsize"])
    reports = []
    catalog_sha = catalog_sha or candidate_catalog_sha(records)
    for candidate in candidates:
        commit = candidate["commit"]
        if commit not in selections:
            reports.append({"commit": commit, "committed_at": candidate["committed_at"],
                            "accepted_count": 0, "rejected_count": len(records),
                            "candidate_error": rejected_candidates.get(commit, "incompatible")})
            continue
        tar_path = output / f"{commit}.tar"
        report = bundle(selections[commit], cache, tar_path, abi=ARCHES[architecture], catalog_sha256=catalog_sha)
        blob = uploader(tar_path)
        reports.append({"commit": commit, "committed_at": candidate["committed_at"], **report,
                        "object": blob.get("object", blob.get("key")), "sha256": blob["sha256"], "size": blob["size"]})
    return reports


def candidate_catalog_sha(records: list[dict]) -> str:
    # The archive digest is supplied by main after signature verification; this
    # deterministic fallback exists for pure unit tests.
    from multiarch_pin import digest
    return digest(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True); parser.add_argument("--trusted-key-sha256", required=True)
    parser.add_argument("--candidates", type=Path, required=True); parser.add_argument("--architecture", choices=ARCHES, required=True)
    parser.add_argument("--fsbuild", required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    records = verify_catalogue(args.catalog, args.trusted_key_sha256)
    candidates = _load_json(args.candidates.read_text(), "candidate requirements")
    if not isinstance(candidates, list): raise SystemExit("candidate requirements must be an array")
    def upload(path: Path) -> dict:
        return json.loads(subprocess.check_output([args.fsbuild, "blob", "put", "--file", str(path)], text=True))
    package_arch = "amd64" if args.architecture == "amd64" else "aarch64"
    with tempfile.TemporaryDirectory() as directory:
        reports = build_candidates(candidates, records, args.architecture, Path(directory), args.output,
                                   f"https://pkg.freebsd.org/FreeBSD:16:{package_arch}/latest", upload,
                                   hashlib.sha256(args.catalog.read_bytes()).hexdigest())
    args.output.joinpath("candidates.json").write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__": main()
