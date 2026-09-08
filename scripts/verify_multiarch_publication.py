#!/usr/bin/env python3
"""Bind local or publicly visible qualified documents to a verified completion."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

ARCHES = {"amd64": "amd64", "arm64": "aarch64"}


def read_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "FreeSense-multiarch-publisher/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(64 * 1024 * 1024)


def verify(completion: dict, repositories: dict[str, bytes], releases: dict[str, bytes]) -> None:
    if (completion.get("schema_version") != "freesense.multiarch-release/v1"
            or completion.get("channel") != "devel" or set(completion.get("architectures", {})) != set(ARCHES)):
        raise ValueError("invalid multiarch completion payload")
    for arch in ARCHES:
        expected = completion["architectures"][arch]
        if (hashlib.sha256(repositories[arch]).hexdigest() != expected["repository_document_sha256"]
                or hashlib.sha256(releases[arch]).hexdigest() != expected["release_document_sha256"]):
            raise ValueError(f"{arch} qualified document differs from authoritative completion")


def verify_public(public_base_url: str, completion: dict, reader=read_url) -> None:
    public = public_base_url.rstrip("/")
    visible_repositories = {arch: reader(f"{public}/repos.{package_arch}.manifest.json?multiarch=verify")
                            for arch, package_arch in ARCHES.items()}
    visible_releases = {arch: reader(f"{public}/releases/devel.{arch}.json?multiarch=verify") for arch in ARCHES}
    verify(completion, visible_repositories, visible_releases)


def resolve_authoritative_release(
    completion: dict,
    repositories: dict[str, bytes],
    releases: dict[str, bytes],
) -> dict[str, dict]:
    """Verify and resolve per-architecture authoritative documents.
    Raises ValueError if completion is invalid or any document differs from authoritative hashes."""
    verify(completion, repositories, releases)
    return {
        arch: {
            "repository": json.loads(repositories[arch]),
            "release": json.loads(releases[arch]),
        }
        for arch in ARCHES
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--completion", type=Path, required=True)
    parser.add_argument("--public-base-url")
    for arch in ARCHES:
        parser.add_argument(f"--{arch}-repositories", type=Path, required=True)
        parser.add_argument(f"--{arch}-release", type=Path, required=True)
    args = parser.parse_args()
    repositories = {arch: getattr(args, f"{arch}_repositories").read_bytes() for arch in ARCHES}
    releases = {arch: getattr(args, f"{arch}_release").read_bytes() for arch in ARCHES}
    completion_payload = json.loads(args.completion.read_text())
    verify(completion_payload, repositories, releases)
    if args.public_base_url:
        verify_public(args.public_base_url, completion_payload)
