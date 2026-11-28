"""Lay the single 2.0 tree out as one staging checkout per repository, ready for review before any push.

usage: python tools/export_repos.py [--clones DIR] [--out DIR] [--repo NAME ...] [--from-github | --offline]

For every repository: a fresh local clone of the checkout in --clones, branch `2.0` from its origin/main, and one
commit with the committed content (HEAD) of this tree. Product repositories get their tree replaced; the website
repositories keep their content, lose main's workflows and gain the 2.0 modules in v2/. Nothing is pushed: the
push address of every staging clone is disabled. --from-github bases the branch on the current
main of GitHub (read only, into the staging clone) instead of the checkout's possibly older origin/main. A staging checkout made by this script is replaced on the next run.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "tools/export"
BRANCH = "2.0"
IDENTITY = {"GIT_AUTHOR_NAME": "FreeSense", "GIT_AUTHOR_EMAIL": "dev@freesense.org",
            "GIT_COMMITTER_NAME": "FreeSense", "GIT_COMMITTER_EMAIL": "dev@freesense.org"}
MARKER = "freesense-2.0-export"
# History of the rename; nothing in the published tree needs it.
EXCLUDED = {"tools/rename13.py"}
# The planning notes are local work logs (lab details, open defects) and are not published.
EXCLUDED_FOLDERS = ("planning/",)
REPLACE_MESSAGE = "Build: start the FreeSense 2.0 tree\n\nRelease-Note: skip\n"
OVERLAY_MESSAGE = "Build: add the FreeSense 2.0 checks and modules\n\nRelease-Note: skip\n"

spec = spec_from_file_location("assemble_workspace", ROOT / "freesense-os-base/scripts/assemble-workspace.py")
layout = module_from_spec(spec)
spec.loader.exec_module(layout)


def git(directory: Path, *arguments: str, data: bytes | None = None, env: dict | None = None) -> str:
    result = subprocess.run(["git", "-C", str(directory), *arguments], input=data, capture_output=True,
                            env={**os.environ, **(env or {})})
    if result.returncode:
        raise ValueError(f"git {' '.join(arguments[:2])} in {directory.name}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout.decode("utf-8", "replace").strip()


def listing(directory: Path, revision: str) -> dict[str, tuple[str, str]]:
    """path -> (mode, blob) of a commit."""
    raw = subprocess.run(["git", "-C", str(directory), "ls-tree", "-r", "-z", "--full-tree", revision], check=True,
                         capture_output=True).stdout.decode("utf-8")
    entries = {}
    for record in filter(None, raw.split("\0")):
        meta, path = record.split("\t", 1)
        mode, _, blob = meta.split()
        entries[path] = (mode, blob)
    return entries


def blob_bytes(blob: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", blob], check=True, capture_output=True).stdout


def tree_files() -> dict[str, dict[str, tuple[str, bytes]]]:
    """repository -> path -> (mode, content), from HEAD of the tree and the templates under tools/export."""
    source = listing(ROOT, "HEAD")
    repositories: dict = {name: {} for name in layout.LAYOUT}

    def put(repository: str, path: str, mode: str, content: bytes) -> None:
        if path in repositories[repository]:
            raise ValueError(f"two sources for {repository}:{path}")
        repositories[repository][path] = (mode, content)

    unplaced = []
    for path, (mode, blob) in sorted(source.items()):
        if path in EXCLUDED or path.startswith(EXCLUDED_FOLDERS):
            continue
        if mode not in ("100644", "100755"):
            raise ValueError(f"unsupported entry {mode} {path}")
        target = layout.to_repository(path)
        if target is None:
            unplaced.append(path)
            continue
        put(*target, mode, blob_bytes(blob))
    if unplaced:
        raise ValueError("no repository takes: " + ", ".join(unplaced[:20]))

    def committed(path: str) -> bytes:
        if path not in source:
            raise ValueError(f"commit {path} first")
        return blob_bytes(source[path][1])

    for repository in layout.PRODUCTS:
        put(repository, ".gitattributes", "100644", committed(".gitattributes"))
        put(repository, ".gitignore", "100644", committed("tools/export/gitignore"))
        put(repository, "NOTICE.md", "100644", committed("NOTICE.md"))
        put(repository, "README.md", "100644", committed(f"tools/export/{repository}/README.md")) \
            if "README.md" not in repositories[repository] else None
        put(repository, "AGENTS.md", "100644", committed("tools/export/AGENTS.md"))
    return repositories


def writable(function, path, _):
    os.chmod(path, stat.S_IWRITE)
    function(path)


def export(repository: str, files: dict, clones: Path, out: Path, offline: bool, from_github: bool) -> dict:
    source, destination = clones / repository, out / repository
    if not (source / ".git").exists():
        raise ValueError(f"no checkout of {repository} in {clones}")
    if destination.exists():
        if not (destination / ".git" / MARKER).is_file():
            raise ValueError(f"{destination} was not made by this script; remove it yourself")
        shutil.rmtree(destination, onexc=writable)
    out.mkdir(parents=True, exist_ok=True)
    url = git(source, "remote", "get-url", "origin")
    git(out, "clone", "-q", "--no-hardlinks", "--no-checkout", str(source), repository)
    (destination / ".git" / MARKER).write_text("made by tools/export_repos.py\n", encoding="utf-8")
    for key, value in (("core.autocrlf", "false"), ("core.longpaths", "true"), ("user.name", "FreeSense"),
                       ("user.email", "dev@freesense.org")):
        git(destination, "config", key, value)
    # The base is the active checkout's view of GitHub's main, not its local branch.
    git(destination, "fetch", "-q", url if from_github else str(source),
        "refs/heads/main" if from_github else "refs/remotes/origin/main")
    base = git(destination, "rev-parse", "FETCH_HEAD")
    git(destination, "remote", "set-url", "origin", url)
    git(destination, "remote", "set-url", "--push", "origin", "disabled-until-the-owner-approves")
    behind = "not checked"
    if not offline:
        remote = subprocess.run(["git", "ls-remote", url, "refs/heads/main"], capture_output=True, text=True, timeout=120)
        if remote.returncode == 0 and remote.stdout:
            behind = "current" if remote.stdout.split()[0] == base else "STALE: export again with --from-github"

    existing = listing(destination, base)
    wanted = dict(files)
    if repository in layout.SITES:
        message = OVERLAY_MESSAGE
        kept = {path: entry for path, entry in existing.items() if not path.startswith(".github/")}
        clash = sorted(set(kept) & set(wanted))
        if clash:
            raise ValueError(f"{repository}: the tree would overwrite {', '.join(clash[:10])}")
    else:
        message = REPLACE_MESSAGE
        # Upstream attribution stays exactly as on main.
        kept = {path: entry for path, entry in existing.items() if path == "NOTICE"}
    index = [f"{mode} {blob}\t{path}" for path, (mode, blob) in kept.items()]
    for path, (mode, content) in sorted(wanted.items()):
        blob = git(destination, "hash-object", "-w", "--no-filters", "--stdin", data=content)
        index.append(f"{mode} {blob}\t{path}")
    git(destination, "read-tree", "--empty")
    git(destination, "update-index", "--add", "-z", "--index-info", data=("\0".join(index) + "\0").encode("utf-8"))
    tree = git(destination, "write-tree")
    commit = git(destination, "commit-tree", tree, "-p", base, "-F", "-", data=message.encode("utf-8"), env=IDENTITY)
    git(destination, "update-ref", f"refs/heads/{BRANCH}", commit)
    git(destination, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    git(destination, "reset", "-q", "--hard")
    removed = len(set(existing) - set(kept) - set(wanted))
    return {"base": base[:12], "commit": commit[:12], "files": len(index), "added": len(set(wanted) - set(existing)),
            "removed": removed, "main": behind, "url": url}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clones", type=Path, default=ROOT.parent / "active")
    parser.add_argument("--out", type=Path, default=ROOT.parents[1] / "local-output" / "2.0-staging")
    parser.add_argument("--repo", action="append", choices=sorted(layout.LAYOUT))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--offline", action="store_true", help="do not ask GitHub whether the base is current")
    group.add_argument("--from-github", action="store_true", help="base on main as GitHub has it now")
    args = parser.parse_args()
    dirty = git(ROOT, "status", "--porcelain")
    if dirty:
        print(f"note: {len(dirty.splitlines())} uncommitted change(s) in the tree are not exported", file=sys.stderr)
    print(f"tree {git(ROOT, 'rev-parse', '--short=12', 'HEAD')}")
    files = tree_files()
    for repository in args.repo or layout.LAYOUT:
        result = export(repository, files[repository], args.clones.resolve(), args.out.resolve(), args.offline,
                        args.from_github)
        print(f"{repository}: " + " ".join(f"{key}={value}" for key, value in result.items() if key != "url"))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(f"FAIL: {error}")
