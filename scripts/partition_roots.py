#!/usr/bin/env python3
"""Deterministically assign component roots, isolating measured heavy roots."""
import argparse
import json
from pathlib import Path
import re

ORIGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*/[A-Za-z0-9][A-Za-z0-9+_.-]*(?:@[A-Za-z0-9][A-Za-z0-9+_.-]*)?$")


def partition(roots: list[str], heavy: list[str], count: int) -> list[list[str]]:
    ordered = sorted(set(roots))
    heavy = sorted(set(heavy) & set(ordered))
    if count != 4 or any(not ORIGIN.fullmatch(root) for root in ordered + heavy):
        raise ValueError("invalid four-shard root plan")
    if len(heavy) >= count:
        raise ValueError("too many measured heavy roots for isolated shards")
    shards = [[root] for root in heavy] + [[] for _ in range(count - len(heavy))]
    for index, root in enumerate(item for item in ordered if item not in heavy):
        shards[len(heavy) + index % (count - len(heavy))].append(root)
    return shards


def load(config: Path, component: str, roots: list[str]) -> list[list[str]]:
    value = json.loads(config.read_text(encoding="utf-8"))
    if (value.get("schema_version") != "freesense.multiarch-shards/v1"
            or set(value.get("measured_heavy_roots", {})) != {"system", "packages"}):
        raise ValueError("invalid multiarch shard policy")
    return partition(roots, value["measured_heavy_roots"][component], value.get("count"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--component", choices=("system", "packages"), required=True)
    parser.add_argument("--shard", type=int, choices=range(4), required=True)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    roots = [line.strip() for line in args.roots.read_text().splitlines() if line.strip()]
    selected = load(args.config, args.component, roots)[args.shard]
    args.output.write_text("".join(root + "\n" for root in selected), encoding="utf-8")
