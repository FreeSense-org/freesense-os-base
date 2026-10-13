#!/usr/bin/env python3
"""Propose the next patch lock of the Stable train.

The Stable train is frozen on maintenance branches (RELENG_<major>_<minor>) of
freesense, freesense-system-ports and freesense-packages. A refresh is the next
patch lock, config/releases/<train>.<N+1>.json, built from the head of those
branches and the FreeBSD pin that is active now. It is only a proposal: merging
the lock publishes nothing, and a person dispatches stable.yml to release it.

A proposal exists only when something would change against the newest lock of
the train, so a quiet fortnight proposes nothing.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lock
from build_platform import load_policy, pin_target
from multiarch_pin import worker

ROOT = Path(__file__).resolve().parents[1]
ORG = "FreeSense-org"
SOURCES = (
    ("source_sha", "freesense"),
    ("system_ports_sha", "freesense-system-ports"),
    ("packages_sha", "freesense-packages"),
)
# What a refresh is judged on: if none of these moved, there is nothing to ship.
INPUTS = (
    "source_sha", "system_ports_sha", "packages_sha", "freebsd_source_sha", "freebsd_ports_sha",
    "freebsd_osversion", "jail_object", "worker_image_sha256", "worker_tools_sha256", "arm64",
)


def maintenance_branch(train: str) -> str:
    return "RELENG_" + train.replace(".", "_")


def head_commit(repository: str, branch: str) -> str | None:
    """The tip of a maintenance branch, or None when it does not exist."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "FreeSense-build/1"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{ORG}/{repository}/commits/{branch}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            sha = json.load(response).get("sha", "")
    except urllib.error.HTTPError as error:
        if error.code in (404, 422):
            return None
        raise SystemExit(f"cannot read {repository}@{branch}: HTTP {error.code}") from error
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise SystemExit(f"cannot read {repository}@{branch}: {error}") from error
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def latest_lock(root: Path, train: str) -> tuple[str, dict]:
    """The highest patch lock of the train."""
    found = []
    for path in (root / "config/releases").glob(f"{train}.*.json"):
        match = re.fullmatch(rf"{re.escape(train)}\.([0-9]+)", path.stem)
        if match:
            found.append((int(match.group(1)), path.stem))
    if not found:
        raise SystemExit(f"no release lock exists for the {train} train")
    _, version = max(found)
    return version, json.loads((root / "config/releases" / f"{version}.json").read_text(encoding="utf-8"))


def next_version(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def pin_values(pin: dict, policy: dict, now: datetime) -> dict:
    """The FreeBSD part of a lock, read from the active pin exactly as plan.py does."""
    valid_from = datetime.fromisoformat(pin["valid_from"].replace("Z", "+00:00"))
    valid_until = datetime.fromisoformat(pin["valid_until"].replace("Z", "+00:00"))
    if not valid_from <= now < valid_until:
        raise SystemExit("the FreeBSD pin is outside its active window")
    amd64 = worker(pin, "amd64", "dedicated")
    values = {
        "freebsd_source_sha": pin["freebsd_source"]["commit"],
        "freebsd_ports_sha": pin["freebsd_ports"]["commit"],
        "freebsd_osversion": pin["freebsd_source"]["osversion"],
        "jail_object": pin_target(pin, "amd64")["jail_seed"]["object"],
        "worker_image_sha256": amd64["worker_image"]["sha256"],
        "worker_tools_sha256": amd64["worker_tools"]["sha256"],
    }
    if policy["targets"].get("arm64", {}).get("build_enabled"):
        values["arm64"] = {"jail_object": pin_target(pin, "arm64")["jail_seed"]["object"]}
    return values


def propose(root: Path, policy: dict, pin: dict, heads: dict[str, str | None], now: datetime) -> dict:
    """What a refresh would do: {"due": False, "reason": ...} or the new lock."""
    train = policy["release"]["stable_train"]
    branch = maintenance_branch(train)
    missing = [repository for _, repository in SOURCES if not heads.get(repository)]
    if missing:
        return {"due": False, "reason": f"no {branch} branch in: {', '.join(missing)}"}
    version, previous = latest_lock(root, train)
    lock = {key: previous[key] for key in ("schema_version", "package_train", "sealed")}
    lock.update({"release": next_version(version), "product_version": next_version(version) + "-RELEASE"})
    lock.update({key: heads[repository] for key, repository in SOURCES})
    lock.update(pin_values(pin, policy, now))
    ordered = {key: lock[key] for key in (
        "schema_version", "release", "product_version", "package_train", "sealed", "source_sha",
        "system_ports_sha", "packages_sha", "freebsd_source_sha", "freebsd_ports_sha",
        "freebsd_osversion", "jail_object", "worker_image_sha256", "worker_tools_sha256") if key in lock}
    if "arm64" in lock:
        ordered["arm64"] = lock["arm64"]
    changed = [key for key in INPUTS if previous.get(key) != ordered.get(key)]
    if not changed:
        return {"due": False, "reason": f"nothing changed since {version}"}
    return {"due": True, "version": ordered["release"], "previous": version, "changed": changed,
            "lock": ordered, "previous_lock": previous}


def describe(proposal: dict, today: str) -> str:
    """The pull request body: what moves, and what to do with it."""
    new, old, version = proposal["lock"], proposal["previous_lock"], proposal["version"]
    lines = [
        f"## Stable {version} refresh proposal ({today})", "",
        f"A scheduled check found changes since **{proposal['previous']}**. Merging this changes "
        "only `config/releases/` — **it publishes nothing**. To release, merge it, then dispatch "
        f"`Publish FreeSense Stable train` with `release={version}`.", "",
        "### What moves",
    ]
    for key, repository in SOURCES:
        if new[key] != old.get(key):
            lines.append(f"- `{repository}`: [`{old.get(key, '')[:8]}…{new[key][:8]}`]"
                         f"(https://github.com/{ORG}/{repository}/compare/{old.get(key)}...{new[key]})")
    for key, label in (("freebsd_source_sha", "FreeBSD source"), ("freebsd_ports_sha", "FreeBSD ports")):
        if new[key] != old.get(key):
            lines.append(f"- {label}: `{old.get(key, '')[:12]}` → `{new[key][:12]}`")
    if new["freebsd_osversion"] != old.get("freebsd_osversion"):
        lines.append(f"- OSVERSION: {old.get('freebsd_osversion')} → {new['freebsd_osversion']}")
    if any(key in proposal["changed"] for key in ("jail_object", "worker_image_sha256", "worker_tools_sha256", "arm64")):
        lines.append("- Worker / jail inputs of the new FreeBSD pin")
    lines += [
        "", "### Before you release",
        "- The FreeBSD base is 16.0-CURRENT: a pin refresh takes everything upstream changed, not only "
        "security fixes. Read the compare links above.",
        f"- Upgrade-test {proposal['previous']} → {version} on a test VM.",
        "- The pin this lock matches must still be active when you dispatch (it rotates every 14 days); "
        "if it has rotated, this proposal is replaced by a fresh one.",
        "- A person decides: leaving this open does nothing, and it closes itself when stale.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the proposed lock into config/releases/")
    parser.add_argument("--body", type=Path, help="write the pull request body here")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    policy = load_policy(ROOT / "config/build-policy.json")
    pin = json.loads((ROOT / "config/freebsd-16.json").read_text(encoding="utf-8"))
    branch = maintenance_branch(policy["release"]["stable_train"])
    heads = {repository: head_commit(repository, branch) for _, repository in SOURCES}
    now = datetime.now(timezone.utc)
    result = propose(ROOT, policy, pin, heads, now)
    outputs = {"due": str(result["due"]).lower(), "reason": result.get("reason", "")}
    if result["due"]:
        release_lock_path = ROOT / "config/releases" / f"{result['version']}.json"
        outputs.update({"version": result["version"], "previous": result["previous"],
                        "changed": ",".join(result["changed"])})
        if args.write:
            release_lock_path.write_text(json.dumps(result["lock"], indent=2) + "\n", encoding="utf-8")
            # a lock this script wrote must be one stable_plan can load
            release_lock.load(ROOT, policy, result["version"])
        if args.body:
            args.body.write_text(describe(result, now.strftime("%Y-%m-%d")), encoding="utf-8")
    print(json.dumps(outputs, indent=2))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
