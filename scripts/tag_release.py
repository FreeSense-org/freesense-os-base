#!/usr/bin/env python3
"""Tag a sealed Stable release in the four repositories (release policy R5).

v<release> is an annotated tag on the lock's commit in freesense,
freesense-system-ports and freesense-packages, and on the os-base commit that
added config/releases/<release>.json. Tags never move: an existing tag on the
same commit is left alone, one on another commit is an error.

GITHUB_TOKEN needs contents:write on the four repositories. Run it by hand
(GITHUB_TOKEN=$(gh auth token) python3 scripts/tag_release.py --release 1.1.5)
to tag a release whose stable.yml tag job failed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lock
from stable_refresh import ORG, SOURCES

ROOT = Path(__file__).resolve().parents[1]
TAGGER = {"name": "FreeSense release", "email": "dev@freesense.org"}


def api(method: str, path: str, token: str, body: dict | None = None):
    request = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "FreeSense-build/1",
                 "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise SystemExit(f"{method} {path}: HTTP {error.code} {error.read().decode(errors='replace')[:300]}") from error


def lock_commit(release: str, token: str) -> str:
    """The os-base commit that added the release's lock (the oldest touching it)."""
    commits = api("GET", f"/repos/{ORG}/freesense-os-base/commits?sha=main&per_page=100"
                         f"&path=config/releases/{release}.json", token) or []
    if not commits:
        raise SystemExit(f"no os-base commit adds config/releases/{release}.json")
    return commits[-1]["sha"]


def tagged_commit(repository: str, tag: str, token: str) -> str | None:
    ref = api("GET", f"/repos/{ORG}/{repository}/git/ref/tags/{tag}", token)
    if ref is None:
        return None
    target = ref["object"]
    if target["type"] == "tag":
        target = api("GET", f"/repos/{ORG}/{repository}/git/tags/{target['sha']}", token)["object"]
    return target["sha"]


def tag(repository: str, sha: str, release: str, token: str, dry_run: bool) -> str:
    name = f"v{release}"
    existing = tagged_commit(repository, name, token)
    if existing == sha:
        return f"{repository}: {name} already on {sha[:12]}"
    if existing is not None:
        raise SystemExit(f"{repository}: {name} is on {existing[:12]}, not {sha[:12]}; tags never move (R5)")
    if dry_run:
        return f"{repository}: would tag {name} on {sha[:12]}"
    tagger = {**TAGGER, "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    created = api("POST", f"/repos/{ORG}/{repository}/git/tags", token, {
        "tag": name, "message": f"FreeSense {release} Stable", "object": sha, "type": "commit", "tagger": tagger})
    api("POST", f"/repos/{ORG}/{repository}/git/refs", token, {"ref": f"refs/tags/{name}", "sha": created["sha"]})
    return f"{repository}: tagged {name} on {sha[:12]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", action="append", required=True, help="sealed release, repeatable")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("GITHUB_TOKEN with contents:write on the four repositories is required")
    for release in args.release:
        lock = release_lock.validate(
            json.loads((ROOT / "config/releases" / f"{release}.json").read_text(encoding="utf-8")), release)
        targets = [(repository, lock[key]) for key, repository in SOURCES]
        targets.append(("freesense-os-base", lock_commit(release, token)))
        for repository, sha in targets:
            print(tag(repository, sha, release, token, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
