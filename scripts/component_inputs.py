"""Fingerprint local System and Optional Package inputs independently for a dry-run comparison."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def recipe_digest(root: Path, files: list[Path]) -> str:
    """Use Development's length-framed path and content encoding for all recipes."""
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise ValueError(f"Recipe input must belong to its local component: {path}")
        relative = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big") + relative)
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def fingerprint(inputs: dict) -> str:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def inputs_under(root: Path, excluded: set[str]) -> list[Path]:
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ValueError(f"Expected a local input directory: {root}")
    files = []
    for base, dirs, names in root.walk(on_error=raise_error):
        if base == root:
            dirs[:] = [name for name in dirs if name not in excluded]
            names = [name for name in names if name not in excluded]
        for name in [*dirs, *names]:
            path = base / name
            if path.is_symlink() or path.is_junction():
                raise ValueError(f"Review linked build input: {path}")
        files.extend(base / name for name in names)
    if not files:
        raise ValueError(f"No admitted inputs under {root}")
    return files


def raise_error(error: OSError) -> None:
    raise error


def component_inputs(workspace: Path, pin: str, architecture: str) -> dict:
    if not re.fullmatch("[0-9a-f]{64}", pin) or architecture not in {"amd64", "aarch64"}:
        raise ValueError("Expected a FreeBSD pin digest and a supported target architecture")
    common = {"format": "freesense-local-inputs/v1", "freebsd_pin": pin, "architecture": architecture}
    system = workspace / "freesense"
    ports = workspace / "freesense-system-ports"
    optional = workspace / "freesense-packages"
    ignored = {".git", ".github", "README.md", "AGENTS.md", "tests"}
    result = {"system": fingerprint({
        **common, "component": "system",
        "payload": recipe_digest(system, [system / "LICENSE", *inputs_under(system / "src", set())]),
        "ports": recipe_digest(ports, inputs_under(ports, ignored)),
    }), "packages": None}
    if optional.exists():
        package_config = workspace / "freesense/tools/conf/pfPorts/make.conf"
        result["packages"] = fingerprint({
            **common, "component": "packages",
            "overlay": recipe_digest(optional, inputs_under(optional, ignored | {"tools", "policy", "docs"})),
            "make_conf": recipe_digest(workspace / "freesense", [package_config]),
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freebsd-pin", required=True, help="SHA-256 identity of the FreeBSD pin")
    parser.add_argument("--architecture", required=True, choices=("amd64", "aarch64"))
    args = parser.parse_args()
    print(json.dumps(component_inputs(Path(__file__).resolve().parents[2], args.freebsd_pin, args.architecture),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f"Cannot fingerprint local inputs: {error}")
