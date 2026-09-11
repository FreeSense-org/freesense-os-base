#!/usr/bin/env python3
"""Dependency-aware, deterministic shard and cumulative-batch planning."""
import argparse
import json
from pathlib import Path
import re

ORIGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*/[A-Za-z0-9][A-Za-z0-9+_.-]*(?:@[A-Za-z0-9][A-Za-z0-9+_.-]*)?$")


def partition(roots: list[str], heavy: list[str], count: int,
              closures: dict[str, list[str]] | None = None,
              costs: dict[str, int] | None = None) -> list[list[str]]:
    ordered = sorted(set(roots))
    heavy = sorted(set(heavy) & set(ordered))
    if count < 1 or len(heavy) >= count or any(not ORIGIN.fullmatch(root) for root in ordered + heavy):
        raise ValueError("invalid shard root plan")
    closures, costs = closures or {}, costs or {}
    if any(root not in ordered or type(costs.get(root, 1)) is not int or costs.get(root, 1) < 0 for root in heavy):
        raise ValueError("invalid measured root policy")
    # Union roots that share dependencies. Keeping closures together prevents
    # duplicate source work across runners and makes checkpoint reuse useful.
    groups: list[set[str]] = [{root} for root in heavy]
    for root in ordered:
        if root in heavy:
            continue
        deps = set(closures.get(root, [root])) | {root}
        overlaps = [group for group in groups[len(heavy):] if any(deps & (set(closures.get(item, [item])) | {item}) for item in group)]
        merged = {root}
        for group in overlaps:
            merged |= group
            groups.remove(group)
        groups.append(merged)
    groups = groups[:len(heavy)] + sorted(groups[len(heavy):], key=lambda group: (-sum(costs.get(root, 1) for root in group), sorted(group)))
    shards = [[] for _ in range(count)]
    loads = [0] * count
    for group_index, group in enumerate(groups):
        index = group_index if group_index < len(heavy) else min(range(len(heavy), count), key=lambda i: (loads[i], i))
        shards[index].extend(sorted(group))
        loads[index] += sum(costs.get(root, 1) for root in group)
    return shards


def batches(roots: list[str], costs: dict[str, int], limit_seconds: int = 10800,
            default_cost_seconds: int = 1800) -> list[list[str]]:
    """Return cumulative batch root lists capped by measured work."""
    if limit_seconds <= 0 or default_cost_seconds <= 0:
        raise ValueError("invalid batch watchdog")
    result, current, elapsed = [], [], 0
    for root in roots:
        cost = costs.get(root, default_cost_seconds)
        if type(cost) is not int or cost < 0:
            raise ValueError(f"invalid measured cost: {root}")
        if cost > limit_seconds:
            if current:
                result.append(current.copy()); current, elapsed = [], 0
            result.append([root])
            continue
        if current and elapsed + cost > limit_seconds:
            result.append(current.copy())
            current, elapsed = [], 0
        current.append(root); elapsed += cost
    if current:
        result.append(current.copy())
    cumulative, seen = [], []
    for batch in result:
        seen.extend(batch); cumulative.append(seen.copy())
    return cumulative


def load(config: Path, component: str, roots: list[str]) -> list[list[str]]:
    value = json.loads(config.read_text(encoding="utf-8"))
    if (value.get("schema_version") != "freesense.multiarch-shards/v1"
            or set(value.get("measured_heavy_roots", {})) != {"system", "packages"}):
        raise ValueError("invalid multiarch shard policy")
    closures = value.get("dependency_closures", {}).get(component, {})
    costs = value.get("measured_cost_seconds", {}).get(component, {})
    if not isinstance(closures, dict) or not isinstance(costs, dict):
        raise ValueError("invalid dependency/cost shard policy")
    return partition(roots, value["measured_heavy_roots"][component], value.get("count"), closures, costs)


def load_batches(config: Path, component: str, roots: list[str]) -> list[list[list[str]]]:
    value = json.loads(config.read_text(encoding="utf-8"))
    shards = load(config, component, roots)
    costs = value.get("measured_cost_seconds", {}).get(component, {})
    limit = value.get("batch_target_seconds")
    default_cost = value.get("default_cost_seconds", 1800)
    return [batches(shard, costs, limit, default_cost) for shard in shards]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--component", choices=("system", "packages"), required=True)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batches-output", type=Path)
    args = parser.parse_args()
    roots = [line.strip() for line in args.roots.read_text().splitlines() if line.strip()]
    shards = load(args.config, args.component, roots)
    if args.shard < 0 or args.shard >= len(shards):
        raise SystemExit("invalid shard")
    selected = shards[args.shard]
    args.output.write_text("".join(root + "\n" for root in selected), encoding="utf-8")
    if args.batches_output:
        selected_batches = load_batches(args.config, args.component, roots)[args.shard]
        args.batches_output.write_text(json.dumps(selected_batches, separators=(",", ":")) + "\n")
