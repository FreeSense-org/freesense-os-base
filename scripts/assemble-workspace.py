#!/usr/bin/env python3
"""Lay the FreeSense 2.0 repository checkouts out as the one workspace the checks and the build driver expect.

usage: assemble-workspace.py --repos DIR --dest DIR [--optional NAME ...]

DIR of --repos holds one checkout per repository, named like the repository. --dest must not exist or be empty.
Repositories named with --optional (and the website repositories) are left out when their checkout is missing.
Only files that git tracks are copied. LAYOUT is also the export mapping of tools/export_repos.py.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

PRODUCTS = ("freesense", "freesense-os-base", "freesense-system-ports", "freesense-packages")
SITES = ("freesense-docs", "freesense.org", "freesense-security")
ROOT_DOCUMENTS = ("AGENTS.md", "README.md", "NOTICE.md", ".gitignore", ".gitattributes")
# repository -> ((workspace path, repository path), ...); "" is the root. A longer repository path wins.
LAYOUT = {
    "freesense": (("freesense", ""), ("freesense-native", "native")),
    "freesense-os-base": (("freesense-os-base", ""), ("tools", "workspace/tools"), ("planning", "workspace/planning"),
                          *((name, f"workspace/{name}") for name in ROOT_DOCUMENTS)),
    "freesense-system-ports": (("freesense-system-ports", ""),),
    "freesense-packages": (("freesense-packages", ""),),
    # The website repositories keep their own content; the 2.0 modules live in v2/ and only those are checked here.
    "freesense-docs": (("freesense-docs/.github", ".github"),),
    "freesense.org": (("freesense.org", "v2"), ("freesense.org/.github", ".github")),
    "freesense-security": (("freesense-security", "v2"), ("freesense-security/.github", ".github")),
}


def inside(path: str, prefix: str) -> str | None:
    """The rest of `path` below `prefix` ("" when equal), or None."""
    if not prefix:
        return path
    if path == prefix:
        return ""
    return path[len(prefix) + 1:] if path.startswith(prefix + "/") else None


def to_workspace(repository: str, path: str) -> str | None:
    """Where a repository file goes in the workspace; None for files that are not part of it."""
    for workspace, source in sorted(LAYOUT[repository], key=lambda pair: -len(pair[1])):
        rest = inside(path, source)
        if rest is None or (repository in SITES and not source):
            continue
        return str(PurePosixPath(workspace, rest))
    return None


def to_repository(path: str) -> tuple[str, str] | None:
    """The (repository, path) that owns a workspace file; None when no repository takes it."""
    pairs = [(workspace, source, repository) for repository, entries in LAYOUT.items() for workspace, source in entries]
    for workspace, source, repository in sorted(pairs, key=lambda item: -len(item[0])):
        rest = inside(path, workspace)
        if rest is not None:
            return repository, str(PurePosixPath(source, rest))
    return None


def tracked(checkout: Path) -> list[str]:
    result = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z"], check=True, capture_output=True)
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def assemble(repos: Path, destination: Path, optional: set[str]) -> dict[str, int]:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"{destination} is not empty")
    counts = {}
    for repository in LAYOUT:
        checkout = repos / repository
        if not (checkout / ".git").exists():
            if repository in optional or repository in SITES:
                continue
            raise ValueError(f"no checkout of {repository} in {repos}")
        counts[repository] = 0
        for name in tracked(checkout):
            target = to_workspace(repository, name)
            if target is None:
                continue
            path = destination / target
            if path.exists():
                raise ValueError(f"two repositories provide {target}")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checkout / name, path)
            counts[repository] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repos", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--optional", action="append", default=[], choices=sorted(LAYOUT))
    args = parser.parse_args()
    for repository, count in assemble(args.repos.resolve(), args.dest.resolve(), set(args.optional)).items():
        print(f"{repository}: {count} files")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"FAIL: {error}")
