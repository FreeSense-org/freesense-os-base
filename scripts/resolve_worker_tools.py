#!/usr/bin/env python3
"""Resolve and verify the pinned FreeBSD worker-tool package closure."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import stat
import sys
from typing import Iterable, NamedTuple, TextIO


ROOT_NAMES = (
    "gtar",
    "git-tiny",
    "rclone",
    "jq",
    "poudriere-devel",
    "xmlstarlet",
    "qemu-user-static",
    "qemu-tools",
)
COMMANDS = ("gtar", "git", "rclone", "jq", "poudriere", "xml", "indexinfo", "qemu-aarch64-static", "qemu-img")
SCHEMA_VERSION = "freesense.worker-tools/v1"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+,.@_~-]*")
SAFE_VERSION = re.compile(r"[A-Za-z0-9+,.@_~-]+")
SAFE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9+$,.@_~-]*")
HEX64 = re.compile(r"[0-9a-f]{64}")
SHA1 = re.compile(r"[0-9a-f]{40}")
OSVERSION = re.compile(r"16[0-9]{5}")
ZBASE32_ALPHABET = "ybndrfg8ejkmcpqxot1uwisza345h769"
CHECKSUM_LENGTHS = {0: 52, 1: 64, 2: 103, 5: 52}


class Package(NamedTuple):
    name: str
    version: str
    origin: str
    remote_path: str
    checksum: str
    size: int
    dependencies: tuple[tuple[str, str, str], ...]
    built_at: datetime
    ports_commit: str
    osversion: int | None


class DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _load_json(value: str, context: str) -> object:
    try:
        return json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, DuplicateKeyError, ValueError) as error:
        raise ValueError(f"invalid JSON in {context}: {error}") from error


def _safe_name(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 255 or not SAFE_NAME.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _safe_version(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 255 or not SAFE_VERSION.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _safe_origin(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 511:
        raise ValueError(f"invalid {field}")
    parts = value.split("/")
    if len(parts) != 2 or any(not SAFE_NAME.fullmatch(part) for part in parts):
        raise ValueError(f"invalid {field}")
    return value


def _safe_remote_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096 or "\\" in value:
        raise ValueError("invalid package repository path")
    parts = value.split("/")
    if (
        len(parts) < 2
        or parts[0] != "All"
        or any(not SAFE_PATH_SEGMENT.fullmatch(part) for part in parts)
        or not parts[-1].endswith(".pkg")
    ):
        raise ValueError("invalid package repository path")
    return value


def parse_checksum(value: object) -> tuple[int, str]:
    if not isinstance(value, str):
        raise ValueError("invalid package checksum")
    if "$" not in value:
        if not HEX64.fullmatch(value):
            raise ValueError("invalid legacy SHA-256 package checksum")
        return 1, value
    if value.count("$") != 1:
        raise ValueError("invalid package checksum")
    type_text, digest = value.split("$", 1)
    if type_text not in {"0", "1", "2", "5"}:
        raise ValueError(f"unsupported package checksum type {type_text!r}")
    checksum_type = int(type_text)
    if len(digest) != CHECKSUM_LENGTHS[checksum_type]:
        raise ValueError("invalid package checksum length")
    if checksum_type == 1:
        if not re.fullmatch(r"[0-9a-f]+", digest):
            raise ValueError("invalid hexadecimal package checksum")
    elif any(character not in ZBASE32_ALPHABET for character in digest):
        raise ValueError("invalid z-base32 package checksum")
    return checksum_type, digest


def _parse_package(record: object, line_number: int) -> Package:
    if not isinstance(record, dict):
        raise ValueError(f"package record at line {line_number} is not an object")
    prefix = f"package record at line {line_number}"
    try:
        name = _safe_name(record.get("name"), f"name in {prefix}")
        version = _safe_version(record.get("version"), f"version in {prefix}")
        origin = _safe_origin(record.get("origin"), f"origin in {prefix}")
        remote_path = _safe_remote_path(record.get("repopath"))
        checksum = record.get("sum")
        parse_checksum(checksum)
        if not isinstance(checksum, str):  # Narrow the static type after validation.
            raise AssertionError("validated checksum is not a string")
        size = record.get("pkgsize")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size < 2**63:
            raise ValueError(f"invalid package size in {prefix}")
        raw_dependencies = record.get("deps", {})
        if not isinstance(raw_dependencies, dict):
            raise ValueError(f"invalid dependencies in {prefix}")
        dependencies: list[tuple[str, str, str]] = []
        for dependency_name in sorted(raw_dependencies):
            safe_name = _safe_name(dependency_name, f"dependency name in {prefix}")
            dependency = raw_dependencies[dependency_name]
            if not isinstance(dependency, dict):
                raise ValueError(f"invalid dependency {safe_name!r} in {prefix}")
            if "name" in dependency and dependency["name"] != safe_name:
                raise ValueError(f"dependency name mismatch for {safe_name!r} in {prefix}")
            dependency_version = _safe_version(
                dependency.get("version"), f"dependency version for {safe_name!r} in {prefix}"
            )
            dependency_origin = _safe_origin(
                dependency.get("origin"), f"dependency origin for {safe_name!r} in {prefix}"
            )
            dependencies.append((safe_name, dependency_version, dependency_origin))
        annotations = record.get("annotations")
        if not isinstance(annotations, dict):
            raise ValueError(f"invalid annotations in {prefix}")
        timestamp = annotations.get("build_timestamp")
        if not isinstance(timestamp, str):
            raise ValueError(f"invalid package build timestamp in {prefix}")
        try:
            built_at = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError as error:
            raise ValueError(f"invalid package build timestamp in {prefix}") from error
        ports_commit = annotations.get("ports_top_git_hash")
        if not isinstance(ports_commit, str) or not SHA1.fullmatch(ports_commit):
            raise ValueError(f"invalid package ports commit in {prefix}")
        osversion_text = annotations.get("FreeBSD_version")
        if osversion_text is not None and (
            not isinstance(osversion_text, str) or not OSVERSION.fullmatch(osversion_text)
        ):
            raise ValueError(f"invalid package OSVERSION in {prefix}")
    except KeyError as error:
        raise ValueError(f"missing field {error.args[0]!r} in {prefix}") from error
    return Package(
        name, version, origin, remote_path, checksum, size, tuple(dependencies),
        built_at, ports_commit, int(osversion_text) if osversion_text is not None else None,
    )


def _parse_catalog(
    lines: Iterable[str],
) -> tuple[dict[str, Package], list[Package], set[str]]:
    packages: dict[str, Package] = {}
    all_packages: list[Package] = []
    duplicate_names: set[str] = set()
    remote_paths: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        record = _load_json(line, f"catalog line {line_number}")
        package = _parse_package(record, line_number)
        all_packages.append(package)
        if package.name in packages:
            duplicate_names.add(package.name)
        previous = remote_paths.get(package.remote_path)
        if previous is not None:
            if previous == package.name:
                raise ValueError(f"duplicate package name {package.name!r}")
            raise ValueError(
                f"duplicate repository path {package.remote_path!r} for {previous!r} and {package.name!r}"
            )
        packages.setdefault(package.name, package)
        remote_paths[package.remote_path] = package.name
    if not all_packages:
        raise ValueError("package catalog is empty")
    return packages, all_packages, duplicate_names


def read_catalog(lines: Iterable[str]) -> dict[str, Package]:
    packages, _, duplicate_names = _parse_catalog(lines)
    if duplicate_names:
        name = min(duplicate_names)
        raise ValueError(f"duplicate package name {name!r}")
    return packages


def _dependency_order(
    packages: dict[str, Package], duplicate_names: set[str] | None = None,
) -> list[Package]:
    order: list[Package] = []
    state: dict[str, int] = {}
    stack: list[str] = []
    ambiguous = duplicate_names or set()

    def visit(name: str, required_by: str | None = None) -> None:
        if name in ambiguous:
            raise ValueError(f"duplicate package name {name!r} in worker-tool closure")
        package = packages.get(name)
        if package is None:
            if required_by is None:
                raise ValueError(f"missing worker-tool root package {name!r}")
            raise ValueError(f"missing dependency {name!r} required by {required_by!r}")
        status = state.get(name, 0)
        if status == 2:
            return
        if status == 1:
            first = stack.index(name)
            raise ValueError(f"dependency cycle: {' -> '.join(stack[first:] + [name])}")
        state[name] = 1
        stack.append(name)
        for dependency_name, dependency_version, dependency_origin in package.dependencies:
            dependency = packages.get(dependency_name)
            if dependency is None:
                raise ValueError(
                    f"missing dependency {dependency_name!r} required by {package.name!r}"
                )
            if dependency.version != dependency_version:
                raise ValueError(
                    f"dependency version mismatch for {dependency_name!r}: "
                    f"{package.name!r} requires {dependency_version!r}, catalog has {dependency.version!r}"
                )
            if dependency.origin != dependency_origin:
                raise ValueError(
                    f"dependency origin mismatch for {dependency_name!r}: "
                    f"{package.name!r} requires {dependency_origin!r}, catalog has {dependency.origin!r}"
                )
            visit(dependency_name, package.name)
        stack.pop()
        state[name] = 2
        order.append(package)

    for root in ROOT_NAMES:
        visit(root)
    return order


def _select_ports_commit(packages: Iterable[Package]) -> str:
    package_list = list(packages)
    newest = max(package.built_at for package in package_list)
    commits = {
        package.ports_commit for package in package_list
        if package.built_at == newest
    }
    if len(commits) != 1:
        raise ValueError("newest package generation has ambiguous ports commits")
    return next(iter(commits))


def resolve_worker_tools(lines: Iterable[str]) -> dict[str, object]:
    packages, all_packages, duplicate_names = _parse_catalog(lines)
    ports_sha = _select_ports_commit(all_packages)
    order = _dependency_order(packages, duplicate_names)
    osversions = {package.osversion for package in order if package.osversion is not None}
    if len(osversions) != 1:
        raise ValueError("worker-tool closure has inconsistent package OSVERSION values")
    osversion = next(iter(osversions))
    local_files: set[str] = set()
    resolved: list[dict[str, object]] = []
    for package in order:
        local_file = f"All/{package.name}-{package.version}.pkg"
        if local_file in local_files:
            raise ValueError(f"duplicate deterministic package filename {local_file!r}")
        local_files.add(local_file)
        resolved.append(
            {
                "name": package.name,
                "version": package.version,
                "origin": package.origin,
                "remote_path": package.remote_path,
                "local_file": local_file,
                "checksum": package.checksum,
                "size": package.size,
                "dependencies": [
                    {"name": name, "version": version, "origin": origin}
                    for name, version, origin in package.dependencies
                ],
            }
        )
    install_order = [package["local_file"] for package in resolved]
    return {
        "schema_version": SCHEMA_VERSION,
        "ports_sha": ports_sha,
        "osversion": osversion,
        "roots": list(ROOT_NAMES),
        "commands": list(COMMANDS),
        "package_count": len(resolved),
        "packages": resolved,
        "install_order": install_order,
    }


def zbase32(digest: bytes) -> str:
    output: list[str] = []
    remain = -1
    for index, byte in enumerate(digest):
        position = index % 5
        if position == 0:
            value = byte
            remain = byte >> 5
            output.append(ZBASE32_ALPHABET[value & 0x1F])
        elif position == 1:
            value = remain | byte << 3
            output.extend(
                (ZBASE32_ALPHABET[value & 0x1F], ZBASE32_ALPHABET[(value >> 5) & 0x1F])
            )
            remain = value >> 10
        elif position == 2:
            value = remain | byte << 1
            output.append(ZBASE32_ALPHABET[value & 0x1F])
            remain = value >> 5
        elif position == 3:
            value = remain | byte << 4
            output.extend(
                (ZBASE32_ALPHABET[value & 0x1F], ZBASE32_ALPHABET[(value >> 5) & 0x1F])
            )
            remain = (value >> 10) & 0x3
        else:
            value = remain | byte << 2
            output.extend(
                (ZBASE32_ALPHABET[value & 0x1F], ZBASE32_ALPHABET[(value >> 5) & 0x1F])
            )
            remain = -1
    if remain >= 0:
        output.append(ZBASE32_ALPHABET[remain])
    return "".join(output)


def _digest_file(path: Path, checksum_type: int) -> tuple[str, str]:
    if checksum_type in (0, 1):
        digest = hashlib.sha256()
        sha256 = digest
    elif checksum_type == 2:
        digest = hashlib.blake2b(digest_size=64)
        sha256 = hashlib.sha256()
    elif checksum_type == 5:
        digest = hashlib.blake2s(digest_size=32)
        sha256 = hashlib.sha256()
    else:  # parse_checksum prevents this branch.
        raise ValueError(f"unsupported package checksum type {checksum_type}")
    with path.open("rb") as package_file:
        for chunk in iter(lambda: package_file.read(1024 * 1024), b""):
            digest.update(chunk)
            if sha256 is not digest:
                sha256.update(chunk)
    raw = digest.digest()
    encoded = raw.hex() if checksum_type == 1 else zbase32(raw)
    return encoded, sha256.hexdigest()


def verify_download(path: Path, checksum: str, expected_size: int | None = None) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"downloaded package is missing: {path}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"downloaded package is not a regular file: {path}")
    if expected_size is not None:
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
            raise ValueError("invalid expected package size")
        if metadata.st_size != expected_size:
            raise ValueError(f"downloaded package size mismatch: {path}")
    checksum_type, expected = parse_checksum(checksum)
    actual, sha256 = _digest_file(path, checksum_type)
    if not hmac.compare_digest(actual, expected):
        raise ValueError(f"downloaded package checksum mismatch: {path}")
    return sha256


def verify_manifest_downloads(manifest: object, directory: Path) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid worker-tool manifest schema")
    if manifest.get("roots") != list(ROOT_NAMES):
        raise ValueError("worker-tool manifest has unexpected roots")
    if manifest.get("commands") != list(COMMANDS):
        raise ValueError("worker-tool manifest has unexpected command checks")
    if not isinstance(manifest.get("ports_sha"), str) or not SHA1.fullmatch(manifest["ports_sha"]):
        raise ValueError("worker-tool manifest has an invalid ports commit")
    if (isinstance(manifest.get("osversion"), bool)
            or not isinstance(manifest.get("osversion"), int)
            or not OSVERSION.fullmatch(str(manifest["osversion"]))):
        raise ValueError("worker-tool manifest has an invalid OSVERSION")
    packages = manifest.get("packages")
    install_order = manifest.get("install_order")
    if not isinstance(packages, list) or not isinstance(install_order, list):
        raise ValueError("invalid worker-tool manifest package list")
    if manifest.get("package_count") != len(packages):
        raise ValueError("worker-tool manifest package count mismatch")
    expected_files: list[str] = []
    seen_names: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("invalid worker-tool manifest package")
        name = _safe_name(package.get("name"), "worker-tool package name")
        version = _safe_version(package.get("version"), "worker-tool package version")
        if name in seen_names:
            raise ValueError(f"duplicate worker-tool package {name!r}")
        seen_names.add(name)
        local_file = package.get("local_file")
        expected_local_file = f"All/{name}-{version}.pkg"
        if local_file != expected_local_file:
            raise ValueError(f"invalid local package filename for {name!r}")
        checksum = package.get("checksum")
        if not isinstance(checksum, str):
            raise ValueError(f"invalid checksum for {name!r}")
        size = package.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError(f"invalid size for {name!r}")
        sha256 = verify_download(directory / expected_local_file, checksum, size)
        sealed_sha256 = package.get("sha256")
        if sealed_sha256 is not None and (
            not isinstance(sealed_sha256, str)
            or not HEX64.fullmatch(sealed_sha256)
            or not hmac.compare_digest(sealed_sha256, sha256)
        ):
            raise ValueError(f"sealed SHA-256 mismatch for {name!r}")
        package["sha256"] = sha256
        expected_files.append(expected_local_file)
    if install_order != expected_files:
        raise ValueError("worker-tool install order does not match its package list")
    actual_files = sorted(
        path.relative_to(directory).as_posix()
        for path in (directory / "All").glob("*.pkg")
        if path.is_file()
    )
    if actual_files != sorted(expected_files):
        raise ValueError("download directory does not contain the exact worker-tool closure")


def _write_manifest(manifest: dict[str, object], output: TextIO) -> None:
    json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
    output.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve", help="resolve the exact worker-tool closure")
    resolve.add_argument("--catalog", default="-", help="newline-JSON packagesite, or - for stdin")
    resolve.add_argument("--output", default="-", help="manifest path, or - for stdout")
    verify = commands.add_parser("verify", help="verify already-downloaded package files")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--directory", required=True)
    verify.add_argument(
        "--output",
        help="sealed manifest path (defaults to replacing --manifest after all checks pass)",
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "resolve":
            catalog: TextIO
            if args.catalog == "-":
                catalog = sys.stdin
            else:
                catalog = Path(args.catalog).open("r", encoding="utf-8")
            try:
                manifest = resolve_worker_tools(catalog)
            finally:
                if catalog is not sys.stdin:
                    catalog.close()
            if args.output == "-":
                _write_manifest(manifest, sys.stdout)
            else:
                with Path(args.output).open("w", encoding="utf-8", newline="\n") as output:
                    _write_manifest(manifest, output)
        else:
            manifest_path = Path(args.manifest)
            manifest = _load_json(manifest_path.read_text(encoding="utf-8"), "manifest")
            verify_manifest_downloads(manifest, Path(args.directory))
            output_path = Path(args.output) if args.output else manifest_path
            temporary = output_path.with_name(f".{output_path.name}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as output:
                    _write_manifest(manifest, output)
                temporary.replace(output_path)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
