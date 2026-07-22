#!/usr/bin/env python3
"""Select the ports commit used by the newest FreeBSD package build."""

from __future__ import annotations

from datetime import datetime
import json
import re
import sys
from typing import Iterable


SHA1 = re.compile(r"[0-9a-f]{40}")


def select_ports_commit(lines: Iterable[str]) -> str:
    newest: datetime | None = None
    commits: set[str] = set()

    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            package = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid packagesite record at line {number}") from error
        if not isinstance(package, dict):
            raise ValueError(f"invalid package record at line {number}")
        annotations = package.get("annotations", {})
        if not isinstance(annotations, dict):
            raise ValueError(f"invalid package annotations at line {number}")
        timestamp = annotations.get("build_timestamp")
        if not isinstance(timestamp, str):
            raise ValueError(f"invalid package build timestamp at line {number}")
        try:
            built_at = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError as error:
            raise ValueError(f"invalid package build timestamp at line {number}") from error
        commit = annotations.get("ports_top_git_hash", "")
        if not isinstance(commit, str) or not SHA1.fullmatch(commit):
            raise ValueError(f"invalid package ports commit at line {number}")
        if newest is not None and built_at < newest:
            continue
        if newest is None or built_at > newest:
            newest = built_at
            commits.clear()
        commits.add(commit)

    if newest is None:
        raise ValueError("packagesite has no package build timestamps")
    if len(commits) != 1:
        raise ValueError("newest package generation has ambiguous ports commits")
    return next(iter(commits))


def main() -> int:
    try:
        print(select_ports_commit(sys.stdin))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
