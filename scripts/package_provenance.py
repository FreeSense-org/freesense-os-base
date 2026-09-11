#!/usr/bin/env python3
"""Generate and compare package-level FreeSense build provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from freesense_reuse import provenance, reusable


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"unsafe provenance tree: {path}")
    for item in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        if item.is_symlink():
            raise ValueError(f"symlink in provenance tree: {item}")
        relative = item.relative_to(path).as_posix().encode()
        digest.update(b"d\0" if item.is_dir() else b"f\0")
        digest.update(relative); digest.update(b"\0")
        if item.is_file():
            digest.update(item.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.is_file() else b"").hexdigest()


def build(records: list[dict], *, ports: Path, overlays: list[Path], make_config: Path,
          architecture_policy: Path, abi: str, osversion: int) -> dict:
    mk_hash = tree_digest(ports / "Mk")
    config_hash, policy_hash = file_digest(make_config), file_digest(architecture_policy)
    base = {}
    for record in records:
        name, origin = record.get("name"), record.get("origin")
        if not isinstance(name, str) or not isinstance(origin, str) or name in base or "/" not in origin:
            raise ValueError("invalid provenance inventory")
        candidates = [root / origin for root in overlays] + [ports / origin]
        source = next((path for path in candidates if path.is_dir()), None)
        if source is None:
            raise ValueError(f"package origin is absent: {origin}")
        item = {**record, "abi": abi, "osversion": osversion,
                "port_directory_sha256": tree_digest(source),
                "patches_sha256": tree_digest(source / "files"), "mk_sha256": mk_hash,
                "make_configuration_sha256": config_hash,
                "architecture_policy_sha256": policy_hash}
        item["patched"] = (source / "files").is_dir() and any((source / "files").iterdir())
        item["kernel_sensitive"] = name.lower().endswith("-kmod") or origin.startswith("kld/")
        base[name] = item
    effective: dict[str, str] = {}
    visiting: set[str] = set()

    def effective_digest(name: str) -> str:
        if name in effective:
            return effective[name]
        if name in visiting:
            raise ValueError(f"cyclic package dependency provenance: {name}")
        visiting.add(name)
        item = base[name]
        deps = item.get("dependencies", {})
        if not isinstance(deps, dict):
            raise ValueError("invalid dependency provenance")
        item["dependencies"] = {
            dep: {**value, "provenance_digest": effective_digest(dep) if dep in base else "external"}
            for dep, value in sorted(deps.items())
        }
        effective[name] = provenance(item)["digest"]
        visiting.remove(name)
        return effective[name]

    output = []
    for name in sorted(base):
        item = base[name]
        effective_digest(name)
        output.append({"name": name, "provenance": provenance(item)})
    return {"schema_version": "freesense.repository-package-provenance/v1", "abi": abi,
            "osversion": osversion, "packages": output}


def select(previous: dict, current: dict, *, pin_unchanged: bool) -> dict:
    if previous.get("schema_version") != "freesense.repository-package-provenance/v1" or current.get("schema_version") != previous.get("schema_version"):
        raise ValueError("invalid repository provenance")
    old = {item["name"]: item["provenance"] for item in previous.get("packages", [])}
    accepted, rejected = [], {}
    for item in current.get("packages", []):
        name, now = item["name"], item["provenance"]
        ok, reason = reusable(old.get(name, {}), now, pin_unchanged=pin_unchanged)
        if ok: accepted.append(name)
        else: rejected[name] = reason
    return {"schema_version": "freesense.previous-repository-selection/v1",
            "accepted": sorted(accepted), "rejected": dict(sorted(rejected.items()))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--ports", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, action="append", default=[])
    parser.add_argument("--make-config", type=Path, required=True)
    parser.add_argument("--architecture-policy", type=Path, required=True)
    parser.add_argument("--abi", required=True); parser.add_argument("--osversion", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-provenance", type=Path)
    parser.add_argument("--selection-output", type=Path)
    args = parser.parse_args()
    result = build(json.loads(args.inventory.read_text()), ports=args.ports, overlays=args.overlay,
                   make_config=args.make_config, architecture_policy=args.architecture_policy,
                   abi=args.abi, osversion=args.osversion)
    args.output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    if args.previous_provenance:
        if not args.selection_output: raise SystemExit("--selection-output is required with previous provenance")
        chosen = select(json.loads(args.previous_provenance.read_text()), result, pin_unchanged=True)
        args.selection_output.write_text(json.dumps(chosen, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__": main()
