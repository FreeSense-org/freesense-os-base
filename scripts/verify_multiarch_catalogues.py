#!/usr/bin/env python3
"""Verify final catalogue signatures and the combined System/Optional closure."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from resolve_worker_tools import _safe_name, _safe_origin, _safe_remote_path, _safe_version, parse_checksum

PUBLIC_KEY = Path(__file__).resolve().parents[1] / "config/channel-signing-public.pem"


def verify_signature(archive: bytes, expected_key_sha256: str, public_key: Path = PUBLIC_KEY) -> list[dict]:
    if hashlib.sha256(public_key.read_bytes()).hexdigest() != expected_key_sha256:
        raise ValueError("final catalogue key differs from the frozen build plan")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "packagesite.pkg"
        package.write_bytes(archive)
        # Extract members to stdout; archive-controlled paths never reach disk.
        data = subprocess.check_output(["tar", "--zstd", "-xOf", str(package), "packagesite.yaml"])
        signature = subprocess.check_output(["tar", "--zstd", "-xOf", str(package), "packagesite.yaml.sig"])
        signature_path = root / "catalogue.sig"
        signature_path.write_bytes(signature)
        subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public_key),
                        "-signature", str(signature_path)],
                       input=hashlib.sha256(data).hexdigest().encode(), check=True, capture_output=True)
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


def inventory(records: list[dict], abi: str) -> dict[str, dict]:
    if not records:
        raise ValueError("final signed package catalogue is empty")
    result, paths = {}, set()
    arch = abi.rsplit(":", 1)[1]
    altabi = "freebsd:16:" + ("x86:64" if arch == "amd64" else "aarch64:64")
    for record in records:
        name = _safe_name(record.get("name"), "catalogue package")
        _safe_version(record.get("version"), "catalogue version")
        _safe_origin(record.get("origin"), "catalogue origin")
        path = _safe_remote_path(record.get("repopath"))
        parse_checksum(record.get("sum"))
        if (name in result or path in paths or record.get("arch", record.get("abi")) not in
                {abi, altabi, "freebsd:16:*", "FreeBSD:16:*", "*:*"}):
            raise ValueError("duplicate or cross-architecture signed catalogue package")
        if not isinstance(record.get("deps", {}), dict):
            raise ValueError("invalid signed package dependencies")
        result[name] = record
        paths.add(path)
    return result


def verify_closure(system: dict, packages: dict) -> None:
    combined = dict(system)
    for name, record in packages.items():
        if name in combined and combined[name] != record:
            raise ValueError("System and Optional signed catalogues disagree on a package")
        combined[name] = record
    for record in combined.values():
        for name, dependency in record.get("deps", {}).items():
            actual = combined.get(name)
            if actual is None or any(actual.get(key) != dependency.get(key) for key in ("version", "origin")):
                raise ValueError("signed System/Optional dependency closure is incomplete")
