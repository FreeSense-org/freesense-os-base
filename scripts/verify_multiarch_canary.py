#!/usr/bin/env python3
"""Verify both immutable release closures without publishing channel documents."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

from build_platform import load_policy, release_profiles
from multiarch_pin import ARCHES, SHA256
from publish_download import fetch_bytes, validate_download
from verify_multiarch_catalogues import inventory, verify_closure, verify_signature
from resolve_worker_tools import parse_checksum, zbase32


def verify_file(url: str, expected_sha: str, expected_size: int, *, catalog_checksum: str | None = None) -> None:
    checksum, size = hashlib.sha256(), 0
    signed_digest = None
    if catalog_checksum is not None:
        checksum_type, signed_expected = parse_checksum(catalog_checksum)
        signed_digest = (hashlib.blake2b(digest_size=64) if checksum_type == 2 else
                         hashlib.blake2s(digest_size=32) if checksum_type == 5 else hashlib.sha256())
    request = urllib.request.Request(url, headers={"User-Agent": "FreeSense-multiarch-canary/1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > expected_size:
                raise ValueError("immutable artifact exceeds its declared size")
            checksum.update(chunk)
            if signed_digest is not None:
                signed_digest.update(chunk)
    if size != expected_size or checksum.hexdigest() != expected_sha:
        raise ValueError("immutable artifact byte verification failed")
    if signed_digest is not None:
        actual = signed_digest.hexdigest() if checksum_type == 1 else zbase32(signed_digest.digest())
        if actual != signed_expected:
            raise ValueError("package bytes differ from their signed catalogue checksum")


def expected_artifacts(policy: dict, arch: str) -> set[tuple]:
    result = set()
    for profile in release_profiles(policy, arch):
        if profile.get("kind") == "appliance":
            result.add(("appliance", profile["name"], "ufs", "img"))
        else:
            result.add(("installer", profile["name"], None, profile["installer"]))
            for filesystem in profile["filesystems"]:
                for format in profile["formats"]:
                    result.add(("cloud", profile["name"], filesystem, format))
    return result


def verify(plan: dict, documents: dict[str, bytes], policy: dict, *, read=fetch_bytes, check_file=verify_file,
           check_signature=verify_signature) -> dict:
    if set(documents) != set(ARCHES) or set(plan.get("targets", {})) != set(ARCHES):
        raise ValueError("canary requires both complete architecture results")
    if type(plan.get("generation")) is not int or plan["generation"] <= 0:
        raise ValueError("canary requires the reserved shared pair generation")
    result = {"schema_version": "freesense.multiarch-canary/v1", "pair_fingerprint": plan["pair_fingerprint"],
              "freebsd_pin": plan["freebsd_pin"], "generation": plan["generation"],
              "publication_enabled": False, "architectures": {}}
    base = policy["public_base_url"]
    for arch in ARCHES:
        system_plan, package_plan = plan["targets"][arch]["system"], plan["targets"][arch]["packages"]
        release = json.loads(documents[arch])
        validate_download(release, "devel", base)
        if release["generation"] != plan["generation"]:
            raise ValueError("release document differs from the reserved shared pair generation")
        actual = {(item["kind"], item.get("platform", release.get("platform")), item.get("filesystem"), item["format"])
                  for item in release["artifacts"]}
        if (actual != expected_artifacts(policy, arch) or len(actual) != len(release["artifacts"])
                or release.get("architecture") != arch or release.get("system") != system_plan["system"]):
            raise ValueError("release is missing artifacts or belongs to a different target/System")
        for source, expected in {
            "source": system_plan["source_sha"], "ports": system_plan["ports_sha"],
            "os_definition": system_plan["os_base_sha"], "freebsd": system_plan["freebsd_sha"],
        }.items():
            if release.get("provenance", {}).get(source) != expected:
                raise ValueError("release source closure differs from frozen coordinator plan")
        markers, artifact_documents = {}, {}
        for item in release["artifacts"]:
            if (not isinstance(item.get("file"), str) or not item["file"]
                    or any(value in item["file"] for value in ("/", "\\", "..", "?", "#"))):
                raise ValueError("invalid release artifact filename")
            stage = {"installer": "iso", "cloud": "cloud", "appliance": "appliance"}[item["kind"]]
            fingerprint = item.get("artifact_fingerprint", item.get("build_fingerprint"))
            if not SHA256.fullmatch(str(fingerprint)):
                raise ValueError("invalid release artifact identity")
            url = f"{base}/artifacts/{stage}/{fingerprint}/complete.json"
            if item["marker_url"] != url:
                raise ValueError("release artifact points outside its immutable namespace")
            raw = read(url)
            marker = json.loads(raw)
            inputs = marker.get("inputs", {})
            if (marker.get("fingerprint") != fingerprint or marker.get("architecture") != arch
                    or (marker.get("system") or inputs.get("system")) != system_plan["system"]
                    or inputs.get("packages") != package_plan["packages"]
                    or inputs.get("platform") != system_plan["platform"]):
                raise ValueError("artifact marker does not bind the selected complete pair")
            expected_schema = {"cloud": "freesense.cloud-image/v1", "appliance": "freesense.appliance/v1",
                               "iso": "freesense.iso/v2" if arch == "amd64" else "freesense.installer/v1"}[stage]
            if marker.get("schema_version") != expected_schema:
                raise ValueError("artifact has no supported completion marker")
            files = marker.get("files", []) if stage == "cloud" else [marker]
            if not any(all(file.get(key) == item.get(key) for key in ("file", "sha256", "size")) for file in files):
                raise ValueError("release file differs from verified artifact marker")
            if stage == "appliance" and (marker.get("hardware_verification") != "unverified"
                                         or item.get("hardware_verification") != "unverified"):
                raise ValueError("Pi canary must retain structural-only verification labels")
            check_file(url.removesuffix("complete.json") + item["file"], item["sha256"], item["size"])
            markers[url] = hashlib.sha256(raw).hexdigest()
            identity = ("installer" if stage == "iso" else "cloud-" + item["filesystem"] if stage == "cloud"
                        else item["platform"])
            if identity in artifact_documents and artifact_documents[identity] != markers[url]:
                raise ValueError("image formats belong to different immutable artifact documents")
            artifact_documents[identity] = markers[url]
        reused = {}
        catalogues, catalogue_hashes = {}, {}
        for component, component_plan in (("system", system_plan), ("packages", package_plan)):
            suffix = f"packages/{component_plan['package_train']}" if component == "packages" else "system"
            component_url = f"{base}/artifacts/{suffix}/{component_plan[component]}"
            marker = json.loads(read(component_url + "/complete.json"))
            if (marker.get("stage") != component or marker.get("fingerprint") != component_plan[component]
                    or marker.get("architecture") != arch
                    or marker.get("inputs", {}).get("freebsd_pin_id") != component_plan["freebsd_pin_id"]):
                raise ValueError("component completion differs from planned pin/architecture")
            provenance_raw = read(component_url + f"/{component_plan['package_arch']}/upstream-provenance.json")
            catalogue_raw = read(component_url + f"/{component_plan['package_arch']}/packagesite.pkg")
            catalogues[component] = inventory(check_signature(catalogue_raw, component_plan["signing_public_key_sha256"]),
                                              component_plan["abi"])
            catalogue_hashes[component] = hashlib.sha256(catalogue_raw).hexdigest()
            if hashlib.sha256(provenance_raw).hexdigest() != marker["inputs"].get("upstream_provenance_sha256"):
                raise ValueError("component has no hash-bound upstream provenance")
            provenance = json.loads(provenance_raw)
            if (provenance.get("abi") != component_plan["abi"]
                    or provenance.get("binary_seed") != component_plan["binary_seed_object"]):
                raise ValueError("provenance belongs to a different seed or architecture")
            names = set()
            for package in provenance["packages"]:
                if package["name"] in names or package.get("abi") != component_plan["abi"]:
                    raise ValueError("duplicate or cross-architecture reused package")
                names.add(package["name"])
                signed = catalogues[component].get(package["name"])
                if signed is None or any(signed.get(key) != package.get(key) for key in ("version", "origin")):
                    raise ValueError("reused package is absent from its signed final catalogue")
                if signed["repopath"] != package["file"]:
                    raise ValueError("reused package path differs from its signed final catalogue")
                if not package["file"].startswith("All/") or "/" in package["file"][4:] or ".." in package["file"]:
                    raise ValueError("invalid provenance package path")
                check_file(component_url + f"/{component_plan['package_arch']}/" + package["file"], package["sha256"], package["size"],
                           catalog_checksum=signed["sum"])
            reused[component] = len(names)
        verify_closure(catalogues["system"], catalogues["packages"])
        result["architectures"][arch] = {"release_document_sha256": hashlib.sha256(documents[arch]).hexdigest(),
                                        "system_fingerprint": system_plan["system"],
                                        "packages_fingerprint": package_plan["packages"],
                                        "freebsd_pin_id": system_plan["freebsd_pin_id"],
                                        "artifact_documents": markers, "reused_packages": reused,
                                        "artifact_document_sha256": artifact_documents,
                                        "signed_catalogue_sha256": catalogue_hashes}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--amd64", type=Path, required=True)
    parser.add_argument("--arm64", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--pair-reservation", type=Path, required=True)
    args = parser.parse_args()
    from multiarch_job_timings import verify as verify_timings
    timings = verify_timings(json.loads(args.jobs.read_text()), args.run_id)
    plan = json.loads(args.plan.read_text())
    reservation = json.loads(args.pair_reservation.read_text())
    if (reservation.get("schema_version") != "freesense.generation/v1"
            or reservation.get("fingerprint") != plan["pair_fingerprint"]):
        raise SystemExit("pair reservation does not belong to the frozen coordinator plan")
    plan["generation"] = reservation["generation"]
    result = verify(plan, {arch: getattr(args, arch).read_bytes() for arch in ARCHES}, load_policy())
    result["job_timings"] = timings
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
