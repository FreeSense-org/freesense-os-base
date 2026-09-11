#!/usr/bin/env python3
"""Split, verify, and merge the frozen ports candidate window for chunked pin VMs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


SCHEMA = "freesense.pin-ports-cache/v1"
CHUNK_SIZE = 2


def load_common(path: Path) -> dict:
    common = json.loads(path.read_text())
    candidates = common.get("ports_candidates")
    if not isinstance(candidates, list) or not (1 <= len(candidates) <= 14):
        raise ValueError("ports_candidates must contain 1..14 revisions")
    commits = []
    for item in candidates:
        commit = item.get("commit")
        if not isinstance(commit, str) or len(commit) != 40:
            raise ValueError("invalid ports candidate")
        commits.append(commit)
    if len(set(commits)) != len(commits):
        raise ValueError("duplicate ports candidates")
    return common


def split_candidates(candidates: list[dict], size: int = CHUNK_SIZE) -> list[dict]:
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [{"id": index, "candidates": candidates[offset:offset + size]}
            for index, offset in enumerate(range(0, len(candidates), size))]


def verify_git_commits(git_dir: Path, commits: list[str]) -> None:
    for commit in commits:
        kind = subprocess.check_output(
            ["git", "--git-dir", str(git_dir), "cat-file", "-t", commit], text=True).strip()
        if kind != "commit":
            raise ValueError(f"{commit} is not a commit in {git_dir}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def merge_chunks(files: list[Path], expected: list[str]) -> list[dict]:
    by_commit = {}
    for path in files:
        payload = json.loads(path.read_text())
        if not isinstance(payload, list):
            raise ValueError(f"{path} is not a candidate array")
        for item in payload:
            commit = item.get("commit")
            if commit in by_commit:
                raise ValueError(f"duplicate candidate requirements for {commit}")
            by_commit[commit] = item
    if set(by_commit) != set(expected):
        raise ValueError("chunks did not evaluate the complete frozen candidate window")
    return [by_commit[commit] for commit in expected]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("--output-chunks", type=Path)
    parser.add_argument("--verify-git-dir", type=Path)
    parser.add_argument("--tar", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--merge", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    common = load_common(args.common)
    candidates = common["ports_candidates"]
    commits = [item["commit"] for item in candidates]
    if args.verify_git_dir:
        verify_git_commits(args.verify_git_dir, commits)
    if args.output_chunks:
        chunks = split_candidates(candidates, args.chunk_size)
        args.output_chunks.write_text(json.dumps(chunks) + "\n")
        print(json.dumps([chunk["id"] for chunk in chunks]), flush=True)
    if args.tar or args.report:
        if not args.tar or not args.report:
            raise SystemExit("--tar and --report must be used together")
        sha256 = file_sha256(args.tar)
        args.report.write_text(json.dumps({
            "schema_version": SCHEMA, "sha256": sha256, "size": args.tar.stat().st_size,
            "commits": commits,
        }, indent=2, sort_keys=True) + "\n")
    if args.merge:
        files = sorted(args.merge.glob("**/candidate-requirements-*.json"))
        if not files:
            raise SystemExit(f"no chunk requirement files under {args.merge}")
        merged = merge_chunks(files, commits)
        if not args.output:
            raise SystemExit("--output is required with --merge")
        args.output.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
