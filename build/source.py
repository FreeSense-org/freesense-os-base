"""The pinned sources: fetch them by commit or checksum, apply the patch series, write the build tree.

The series is applied to a temporary git index, so `check` needs no checkout and the `source` stage writes exactly
the tree that was checked.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(repository: Path, *args: str, index: Path | None = None, work: Path | None = None, stdin: str | None = None) -> str:
    # The index and work tree are set per command only; they never leak into other git calls.
    environment = {k: v for k, v in os.environ.items() if k not in {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"}}
    if index:
        environment["GIT_INDEX_FILE"] = str(index)
    if work:
        environment["GIT_WORK_TREE"] = str(work)
    command = ["git", "-c", "core.autocrlf=false", "--git-dir", str(repository), *args]
    result = subprocess.run(command, env=environment, input=stdin, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def fetch_commit(repository: Path, url: str, commit: str, mirror: Path | None) -> None:
    """Make the pinned commit available in a cached bare repository. A commit id names its content."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"not a full commit id: {commit}")
    if not (repository / "HEAD").is_file():
        repository.mkdir(parents=True, exist_ok=True)
        git(repository, "init", "-q", "--bare")
    try:
        git(repository, "cat-file", "-e", f"{commit}^{{commit}}")
        return
    except ValueError:
        pass
    origin = mirror / repository.name if mirror and (mirror / repository.name).is_dir() else url
    print(f"    fetching {repository.name} {commit[:12]} from {origin}", flush=True)
    git(repository, "fetch", "-q", "--depth", "1", "--no-tags", str(origin), commit)
    git(repository, "update-ref", "refs/pins/current", commit)


def fetch_file(destination: Path, urls: list[str], digest: str, mirror: Path | None) -> Path:
    """A checksummed download. A file with another checksum is an error, never silently replaced."""
    if not destination.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        if mirror and (mirror / destination.name).is_file():
            shutil.copyfile(mirror / destination.name, partial)
        else:
            failures = []
            for url in urls:
                try:
                    with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as stream:
                        shutil.copyfileobj(response, stream)
                    break
                except OSError as error:
                    failures.append(f"{url}: {error}")
            else:
                raise ValueError(f"could not download {destination.name}: {'; '.join(failures)}")
        got = sha256(partial)
        if got != digest:
            partial.unlink()
            raise ValueError(f"{destination.name} has sha256 {got}, the lock says {digest}")
        partial.replace(destination)
    elif sha256(destination) != digest:
        raise ValueError(f"{destination} does not match the sha256 in the lock; delete it to fetch it again")
    return destination


def patch_files(component: Path) -> list[Path]:
    patches = sorted((component / "patches").glob("*.patch"))
    if not patches:
        raise ValueError(f"no patches under {component}")
    return patches


def binary_patches(patches: list[Path]) -> list[str]:
    """Patches stay reviewable text. Images live in boot/images; programs are pinned inputs."""
    found = []
    for patch in patches:
        data = patch.read_bytes()
        if re.search(rb"^(GIT binary patch|Binary files )", data, re.M):
            found.append(f"{patch.name} carries a binary hunk")
    return found


def patched_index(repository: Path, commit: str, patches: list[Path], tree_files: dict, component: Path, index: Path) -> str:
    """Apply the series and the checked-in files to an index of the commit and return the tree id."""
    index.parent.mkdir(parents=True, exist_ok=True)
    index.unlink(missing_ok=True)
    git(repository, "read-tree", commit, index=index)
    failed = []
    for patch in patches:
        try:
            git(repository, "apply", "--cached", "--whitespace=nowarn", str(patch.resolve()), index=index)
        except ValueError as error:
            failed.append(f"{patch.name} does not apply: {error}")
    for path in tree_files["remove"]:
        try:
            git(repository, "cat-file", "-e", f"{commit}:{path}")
        except ValueError:
            failed.append(f"file to remove is not in the pinned source: {path}")
        git(repository, "update-index", "--force-remove", path, index=index, work=index.parent)
    for path, local in tree_files["add"].items():
        if not (component / local).is_file():
            failed.append(f"file to add is missing: {local}")
            continue
        blob = git(repository, "hash-object", "-w", "--no-filters", str((component / local).resolve())).strip()
        git(repository, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", index=index, work=index.parent)
    if failed:
        raise ValueError("\n".join(failed))
    return git(repository, "write-tree", index=index).strip()


def osversion(repository: Path, tree: str) -> int:
    header = git(repository, "cat-file", "blob", f"{tree}:sys/sys/param.h")
    found = re.search(r"^#define\s+__FreeBSD_version\s+(\d+)", header, re.M)
    if not found:
        raise ValueError("__FreeBSD_version was not found in sys/sys/param.h")
    return int(found[1])


def missing_contrib_sources(repository: Path, tree: str, contrib: Path, commit: str) -> list[str]:
    """Every library file the ipsecmb module compiles must exist at the pinned library commit."""
    listed = git(repository, "cat-file", "blob", f"{tree}:sys/modules/ipsecmb/sources.mk")
    wanted = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:asm|c)(?![A-Za-z0-9])", re.sub(r"^#.*$", "", listed, flags=re.M))
    have = set(git(contrib, "ls-tree", "-r", "--name-only", commit, "--", "lib").splitlines())
    return [f"ipsecmb needs lib/{name}, which the pinned library does not have" for name in wanted if f"lib/{name}" not in have]


def checkout(repository: Path, index: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    git(repository, "checkout-index", "-a", "-f", index=index, work=destination.resolve())


def write_tree_files(repository: Path, commit: str, destination: Path, exclude: list[str], index: Path) -> None:
    """Check out a pinned commit without the excluded folders."""
    index.unlink(missing_ok=True)
    git(repository, "read-tree", commit, index=index)
    for folder in exclude:
        names = git(repository, "ls-tree", "-r", "--name-only", commit, "--", folder)
        if names:
            git(repository, "update-index", "--force-remove", "--stdin", index=index, work=index.parent, stdin=names)
    checkout(repository, index, destination)
