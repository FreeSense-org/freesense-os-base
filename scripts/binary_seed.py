#!/usr/bin/env python3
"""Select and bundle official packages against evaluated FreeSense requirements.

Run only at pin time. Builds consume the resulting content-addressed tar, never
the upstream repository. Requirements must be collected from the configured
Poudriere ports tree, including build dependencies, on the target architecture.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request

from multiarch_pin import ARCHES, digest
from resolve_worker_tools import _load_json, _safe_name, _safe_origin, _safe_remote_path, _safe_version, verify_download

FLAGS = ("overlay", "custom_patches", "non_options_knobs", "kernel_sensitive", "base_package")


def options(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("OPTIONS must be explicit")
    result = {}
    for key, enabled in value.items():
        _safe_name(key, "option")
        if enabled not in ("on", "off", True, False) or type(enabled) not in (str, bool):
            raise ValueError("invalid OPTIONS value")
        result[key] = enabled in ("on", True)
    return result


def dependencies(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("dependencies must be explicit")
    result = {}
    for name, dep in value.items():
        _safe_name(name, "dependency")
        if not isinstance(dep, dict):
            raise ValueError("invalid dependency")
        result[name] = {
            "origin": _safe_origin(dep.get("origin"), "dependency origin"),
            "version": _safe_version(dep.get("version"), "dependency version"),
        }
    return result


def excluded(requirement: dict) -> str:
    name = requirement["name"].lower()
    origin = requirement["origin"].lower()
    if (name.startswith(("freesense", "freebsd-"))
            or origin.split("/")[1].startswith("freesense")
            or name.endswith("-kmod") or name in {"world", "kernel"}):
        return "mandatory source-only package"
    for flag in FLAGS:
        if type(requirement.get(flag)) is not bool:
            raise ValueError(f"missing explicit eligibility audit: {flag}")
        if requirement[flag]:
            return flag
    return ""


def select(requirements: dict, records: list[dict], architecture: str) -> dict:
    abi = ARCHES[architecture]
    if (requirements.get("schema_version") != "freesense.package-requirements/v1"
            or requirements.get("abi") != abi
            or set(requirements.get("roots", {})) != {"system", "packages"}
            or any(not isinstance(requirements["roots"][key], list) or not requirements["roots"][key]
                   for key in ("system", "packages"))):
        raise ValueError("requirements must cover System and Optional Packages for the target ABI")
    requested = {}
    for req in requirements.get("packages", []):
        name = _safe_name(req.get("name"), "requirement name")
        _safe_origin(req.get("origin"), "requirement origin")
        _safe_version(req.get("version"), "requirement version")
        if name in requested or req.get("abi") != abi:
            raise ValueError("duplicate or cross-architecture requirement")
        options(req.get("options"))
        dependencies(req.get("deps"))
        requested[name] = req
    if not requested:
        raise ValueError("empty requirements")
    for names in requirements["roots"].values():
        if any(name not in requested for name in names):
            raise ValueError("root missing from evaluated requirements")
    for req in requested.values():
        for name, dep in dependencies(req["deps"]).items():
            if name not in requested or any(requested[name][field] != dep[field] for field in ("version", "origin")):
                raise ValueError("requirements have an incomplete dependency closure")
    catalogue = {}
    ambiguous = set()
    for record in records:
        name = record.get("name")
        if name in catalogue:
            ambiguous.add(name)
        catalogue[name] = record
    accepted, rejected = {}, {}
    for name, req in sorted(requested.items()):
        reason = excluded(req)
        official = catalogue.get(name)
        if not reason:
            if official is None or name in ambiguous:
                reason = "missing or ambiguous official package"
            elif any(official.get(field) != req[field] for field in ("name", "version", "origin", "abi")):
                reason = "name/version/origin/ABI mismatch"
            elif options(official.get("options", {})) != options(req["options"]):
                reason = "OPTIONS mismatch"
            elif dependencies(official.get("deps", {})) != dependencies(req["deps"]):
                reason = "dependencies mismatch"
        if reason:
            rejected[name] = reason
        else:
            accepted[name] = official
    # A binary depending on a customized package is also source-only. Iterate
    # to a fixed point, so transitive ABI/OPTIONS differences cannot leak in.
    while True:
        invalid = [name for name, pkg in accepted.items()
                   if any(dep not in accepted for dep in pkg.get("deps", {}))]
        if not invalid:
            break
        for name in invalid:
            rejected[name] = "dependency is source-only"
            del accepted[name]
    if "rust" not in accepted or accepted["rust"]["origin"] != "lang/rust":
        raise ValueError("pin requires a compatible official lang/rust package")
    return {"accepted": accepted, "rejected": rejected, "requirements_sha256": digest(requirements)}


def verify_catalogue(archive: Path, trusted_key_sha256: str) -> list[dict]:
    """Verify FreeBSD's signature of the ASCII SHA-256 catalogue digest."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for member in ("packagesite.yaml", "packagesite.yaml.pub", "packagesite.yaml.sig"):
            data = subprocess.check_output(["tar", "--zstd", "-xOf", str(archive), member])
            (root / member).write_bytes(data)
        public = root / "packagesite.yaml.pub"
        if hashlib.sha256(public.read_bytes()).hexdigest() != trusted_key_sha256:
            raise ValueError("catalogue signer differs from pinned FreeBSD trust root")
        data = (root / "packagesite.yaml").read_bytes()
        subprocess.run([
            "openssl", "dgst", "-sha256", "-verify", str(public),
            "-signature", str(root / "packagesite.yaml.sig"),
        ], input=hashlib.sha256(data).hexdigest().encode(), check=True, capture_output=True)
        return [_load_json(line, "signed catalogue") for line in data.decode().splitlines() if line.strip()]


def bundle(selection: dict, directory: Path, output: Path, *, abi: str, catalog_sha256: str) -> dict:
    packages = []
    for name, record in sorted(selection["accepted"].items()):
        local = f"All/{name}-{record['version']}.pkg"
        sha = verify_download(directory / local, record["sum"], record["pkgsize"])
        packages.append({
            "name": name, "version": record["version"], "origin": record["origin"],
            "abi": abi, "file": local, "sha256": sha, "size": record["pkgsize"],
            "catalog_sha256": catalog_sha256, "upstream_checksum": record["sum"],
            "upstream_path": _safe_remote_path(record["repopath"]),
            "options": options(record.get("options", {})), "deps": record.get("deps", {}),
        })
    manifest = {
        "schema_version": "freesense.binary-seed/v1", "abi": abi,
        "catalog_sha256": catalog_sha256,
        "requirements_sha256": selection["requirements_sha256"],
        "packages": packages, "rejected": selection["rejected"],
    }
    encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    # Stable tar metadata makes identical pin attempts produce identical bytes.
    with tarfile.open(output, "w", format=tarfile.USTAR_FORMAT) as archive:
        for package in packages:
            data_path = directory / package["file"]
            info = tarfile.TarInfo(package["file"])
            info.size, info.mode = package["size"], 0o644
            with data_path.open("rb") as stream:
                archive.addfile(info, stream)
        info = tarfile.TarInfo("provenance.json")
        info.size, info.mode = len(encoded), 0o644
        archive.addfile(info, io.BytesIO(encoded))
    with output.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    reasons = {}
    for reason in selection["rejected"].values():
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "object": f"inputs/sha256/{sha}", "sha256": sha, "size": output.stat().st_size,
        "abi": abi, "catalog_sha256": catalog_sha256, "verified": True,
        "requirements_sha256": selection["requirements_sha256"],
        "requirements_components": ["system", "packages"],
        "provenance_sha256": hashlib.sha256(encoded).hexdigest(),
        "verified_roots": ["rust"], "package_count": len(packages),
        "accepted_count": len(packages), "rejected_count": len(selection["rejected"]),
        "rejection_reasons": dict(sorted(reasons.items())),
        "rejected": dict(sorted(selection["rejected"].items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--trusted-key-sha256", required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--architecture", choices=ARCHES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    records = verify_catalogue(args.catalog, args.trusted_key_sha256)
    requirements = _load_json(args.requirements.read_text(), "requirements")
    selected = select(requirements, records, args.architecture)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "All").mkdir()
        for name, pkg in sorted(selected["accepted"].items()):
            url = f"https://pkg.freebsd.org/{ARCHES[args.architecture]}/latest/{_safe_remote_path(pkg['repopath'])}"
            destination = root / "All" / f"{name}-{pkg['version']}.pkg"
            with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            verify_download(destination, pkg["sum"], pkg["pkgsize"])
        with args.catalog.open("rb") as stream:
            catalogue_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        report = bundle(selected, root, args.output, abi=ARCHES[args.architecture], catalog_sha256=catalogue_sha)
        args.report.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
