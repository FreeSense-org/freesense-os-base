"""Copy files into a throwaway Docker volume so containers read them at native speed.

A Windows bind mount costs a round trip per file operation: one PHP test that restarts its autoloader for every request
took five minutes on the mount and two seconds from the container's own disk. The volume keeps the bind mount's contract:
the same paths, mounted read-only, every file executable.
"""
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.

from contextlib import contextmanager
import io
from pathlib import Path
import secrets
import subprocess
import tarfile

IMAGE = "php:8.6.0-cli"
# Set while a run holds a workspace snapshot; the browser suites mount it instead of the tree (tests/web/ui_helpers.py).
ENVIRONMENT = "FREESENSE_CHECK_VOLUME"
SKIP = {".git", "__pycache__", "node_modules", ".astro", ".cache", "dist"}
PREFIX = "freesense-check-"


def archive(directory: Path, names: list[str] | None = None) -> bytes:
    def prepare(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if Path(info.name).name in SKIP:
            return None
        if info.issym() or info.islnk():
            raise ValueError(f"review link before admission: {info.name}")
        info.mode = 0o777
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as bundle:
        for name in sorted(names if names is not None else (p.name for p in directory.iterdir())):
            bundle.add(directory / name, arcname=name, filter=prepare)
    return stream.getvalue()


@contextmanager
def volume(directory: Path, names: list[str] | None = None, docker: str = "docker"):
    """Yield the name of a volume holding `names` (default: everything) of `directory`; it is removed afterwards."""
    name = PREFIX + secrets.token_hex(8)
    subprocess.run([docker, "volume", "create", name], check=True, capture_output=True)
    try:
        subprocess.run([docker, "run", "--rm", "-i", "--pull=never", "--network", "none", "-v", f"{name}:/check", IMAGE,
                        "tar", "-xf", "-", "-C", "/check"], input=archive(directory, names), check=True, capture_output=True)
        yield name
    finally:
        subprocess.run([docker, "volume", "rm", "-f", name], capture_output=True)
