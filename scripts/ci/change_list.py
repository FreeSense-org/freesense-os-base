#!/usr/bin/env python3
"""Write what changed between two builds, from the commit subjects of the repositories.

usage: change_list.py --repos DIR --current sources.json [--previous sources.json] [--record NAME=DIR ...] --output FILE

sources.json is {"build_id": ..., "repositories": {name: commit}}. --record writes --current from the checkouts'
HEAD commits before the list is made (--build-id names the build). Without --previous the list is the head commits.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
import json
from pathlib import Path
import re
import subprocess

import commit_messages

LIMIT = 100


def record(checkouts: dict[str, Path], build_id: str) -> dict:
    return {"build_id": build_id, "repositories": {
        name: subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True,
                             text=True).stdout.strip() for name, path in checkouts.items()}}


def known(directory: Path, commit: str) -> bool:
    return subprocess.run(["git", "-C", str(directory), "cat-file", "-e", f"{commit}^{{commit}}"],
                          capture_output=True).returncode == 0


def change_list(repos: Path, current: dict, previous: dict | None) -> str:
    lines = [f"# Changes in {current['build_id']}",
             f"Compared with {previous['build_id']}." if previous else "No earlier build to compare with."]
    for name, commit in current["repositories"].items():
        before = (previous or {}).get("repositories", {}).get(name)
        directory = repos / name
        if before == commit:
            continue
        lines += ["", f"## {name} {commit[:12]}"]
        if before and re.fullmatch(r"[0-9a-f]{40}", before) and known(directory, before):
            commits = commit_messages.commits_in(f"{before}..{commit}", str(directory), LIMIT)
        else:
            if before:
                lines.append(f"- The earlier commit {before[:12]} is not in this history; showing the newest commit.")
            commits = commit_messages.commits_in(commit, str(directory), 1)
        notes = [note for _, message in commits if (note := commit_messages.note_line(message))]
        lines += [f"- {note}" for note in notes] or ["- Internal changes only."]
        if len(commits) == LIMIT:
            lines.append(f"- Only the newest {LIMIT} commits are listed.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repos", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--record", action="append", default=[], metavar="NAME")
    parser.add_argument("--build-id", default="local")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.record:
        sources = record({name: args.repos / name for name in args.record}, args.build_id)
        args.current.write_text(json.dumps(sources, indent=1) + "\n", encoding="utf-8")
    current = json.loads(args.current.read_text(encoding="utf-8"))
    previous = json.loads(args.previous.read_text(encoding="utf-8")) if args.previous and args.previous.is_file() else None
    args.output.write_text(change_list(args.repos, current, previous), encoding="utf-8", newline="\n")
    print(args.output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
