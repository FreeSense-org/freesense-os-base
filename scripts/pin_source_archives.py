#!/usr/bin/env python3
"""Store pinned FreeBSD src and ports commits as immutable R2 inputs.

Every build VM used to fetch both trees from GitHub at the same commits for the
whole 14-day pin window. Each commit is now fetched once, stored as a bare
one-commit repository under inputs/sha256/, and recorded in the pin under
source_archives, so builds clone from a local file:// remote instead.

  pin     archive every commit of a candidate pin (pin.yml), carrying over
          those the previous pin already stored
  commit  archive one commit and write its entry (mirror.yml, for the ports
          commit a mirror is cut at; pin_mirrors.py records it)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

from multiarch_pin import SOURCE_ARCHIVE_FORMAT, SOURCE_REPOSITORIES, archivable_commits, with_source_archives

UPSTREAM = "https://github.com/{repository}.git"


def fetch_bare(repository: str, commit: str, destination: Path, upstream: str = UPSTREAM) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    subprocess.run(["git", "init", "-q", "--bare", str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "fetch", "-q", "--depth", "1",
                    upstream.format(repository=repository), f"{commit}:refs/heads/main"], check=True)
    head = subprocess.run(["git", "-C", str(destination), "rev-parse", "refs/heads/main"],
                          check=True, capture_output=True, text=True).stdout.strip()
    if head != commit:
        raise SystemExit(f"{repository} fetched {head}, not the pinned {commit}")
    subprocess.run(["git", "-C", str(destination), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    for transient in ("FETCH_HEAD", "hooks", "logs", "description"):
        path = destination / transient
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def write_tar(source: Path, output: Path) -> None:
    """Tar the repository with sorted entries and no host ownership or times."""
    def normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in [source, *sorted(source.rglob("*"))]:
            archive.add(path, arcname=str(Path(source.name) / path.relative_to(source)),
                        recursive=False, filter=normalise)


def archive_entry(repository: str, blob: dict) -> dict:
    return {"repository": repository, "format": SOURCE_ARCHIVE_FORMAT, "ref": "refs/heads/main",
            "object": blob["object"], "sha256": blob["sha256"], "size": blob["size"]}


def store(repository: str, commit: str, fsbuild: str, work: Path, upstream: str = UPSTREAM) -> dict:
    if repository not in SOURCE_REPOSITORIES:
        raise SystemExit(f"{repository} is not an archived upstream")
    work.mkdir(parents=True, exist_ok=True)
    name = repository.split("/")[1] + ".git"
    repo = work / name
    fetch_bare(repository, commit, repo, upstream)
    tar = work / f"{name}.tar"
    write_tar(repo, tar)
    shutil.rmtree(repo)
    report = work / f"{name}.blob.json"
    subprocess.run([fsbuild, "blob", "put", "--file", str(tar), "--output", str(report)], check=True)
    blob = json.loads(report.read_text(encoding="utf-8"))
    tar.unlink()
    print(f"Stored {repository} {commit} as {blob['object']} ({blob['size']} bytes)")
    return archive_entry(repository, blob)


def archive(pin: dict, previous: dict, fsbuild: str, work: Path, upstream: str = UPSTREAM) -> dict:
    known = previous.get("source_archives") or {}
    additions = {}
    for commit, repository in sorted(archivable_commits(pin).items()):
        carried = known.get(commit)
        if carried and carried.get("repository") == repository:
            print(f"Reusing the stored {repository} archive for {commit}")
            additions[commit] = carried
        else:
            additions[commit] = store(repository, commit, fsbuild, work, upstream)
    return with_source_archives({**pin, "source_archives": {}}, additions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fsbuild", required=True)
    parser.add_argument("--work", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    pin_mode = sub.add_parser("pin")
    pin_mode.add_argument("--pin", type=Path, required=True, help="candidate pin, rewritten in place")
    pin_mode.add_argument("--previous", type=Path, required=True)
    commit_mode = sub.add_parser("commit")
    commit_mode.add_argument("--repository", required=True)
    commit_mode.add_argument("--commit", required=True)
    commit_mode.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "commit":
        entry = store(args.repository, args.commit, args.fsbuild, args.work)
        args.output.write_text(json.dumps({"commit": args.commit, **entry}, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8", newline="\n")
        return 0
    pin = json.loads(args.pin.read_text(encoding="utf-8"))
    previous = json.loads(args.previous.read_text(encoding="utf-8"))
    updated = archive(pin, previous, args.fsbuild, args.work)
    args.pin.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
