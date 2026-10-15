#!/usr/bin/env python3
"""Check the Stable release locks against the release policy (docs/release-policy.md).

Without --base the checked-in locks are validated as a set: every lock of the
current Stable train (and later) is a loadable sealed lock, patch numbers of a
train have no gaps, and withdrawn.json only names locks that exist (R2).

With --base <git ref> the change since that ref is checked as well: a merged
lock is never edited, renamed or deleted (R1), and withdrawn.json only gains
entries (R2).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lock

ROOT = Path(__file__).resolve().parents[1]
LOCKS = "config/releases"
WITHDRAWN = f"{LOCKS}/withdrawn.json"


def train_key(train: str) -> tuple[int, int]:
    major, minor = train.split(".")
    return int(major), int(minor)


def check_set(root: Path, policy: dict) -> list[str]:
    errors = []
    stable_train = policy["release"]["stable_train"]
    trains: dict[str, list[int]] = {}
    for path in sorted((root / LOCKS).glob("*.json")):
        if path.name == "withdrawn.json":
            continue
        match = release_lock.RELEASE.fullmatch(path.stem)
        if match is None:
            errors.append(f"{path.name}: lock files are named <major>.<minor>.<patch>.json")
            continue
        train = f"{match.group(1)}.{match.group(2)}"
        trains.setdefault(train, []).append(int(match.group(3)))
        try:
            lock = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            errors.append(f"{path.name}: not JSON ({error})")
            continue
        try:
            if train_key(train) >= train_key(stable_train):
                release_lock.validate(lock, path.stem)
            elif (lock.get("schema_version") != release_lock.SCHEMA or lock.get("release") != path.stem
                  or lock.get("product_version") != f"{path.stem}-RELEASE"):
                raise SystemExit("historical lock does not name its own release")
        except SystemExit as error:
            errors.append(f"{path.name}: {error}")
    for train, patches in trains.items():
        expected = list(range(len(patches)))
        if sorted(patches) != expected:
            errors.append(f"train {train}: patch numbers must run 0..N without gaps, found {sorted(patches)}")
    try:
        withdrawn = release_lock.withdrawn(root)
    except SystemExit as error:
        return errors + [str(error)]
    for version, entry in withdrawn.items():
        if not (root / LOCKS / f"{version}.json").exists():
            errors.append(f"withdrawn.json: {version} has no lock")
        if entry["superseded_by"] and not (root / LOCKS / f"{entry['superseded_by']}.json").exists():
            errors.append(f"withdrawn.json: {version} is superseded by {entry['superseded_by']}, which has no lock")
    return errors


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def check_change(root: Path, base: str) -> list[str]:
    errors = []
    for line in git(root, "diff", "--name-status", "--no-renames", base, "--", LOCKS).splitlines():
        status, path = line.split("\t", 1)
        if path == WITHDRAWN or status == "A":
            continue
        verb = {"M": "edits", "D": "deletes"}.get(status, "changes")
        errors.append(f"{path}: this change {verb} a merged release lock; locks are append-only "
                      "(R1). Ship a new patch number and withdraw the old one instead (R2).")
    try:
        before = json.loads(git(root, "show", f"{base}:{WITHDRAWN}"))
    except subprocess.CalledProcessError:
        return errors
    after = json.loads((root / WITHDRAWN).read_text(encoding="utf-8")) if (root / WITHDRAWN).exists() else {}
    kept = after.get("withdrawn", [])
    for entry in before.get("withdrawn", []):
        if entry not in kept:
            errors.append(f"withdrawn.json: the entry for {entry.get('release')} was changed or removed; "
                          "entries are append-only (R2)")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="git ref the change is compared against")
    args = parser.parse_args()
    policy = json.loads((ROOT / "config/build-policy.json").read_text(encoding="utf-8"))
    errors = check_set(ROOT, policy)
    if args.base:
        errors += check_change(ROOT, args.base)
    for error in errors:
        print(f"release policy: {error}", file=sys.stderr)
    if not errors:
        print("release locks follow the release policy")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
