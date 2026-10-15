#!/usr/bin/env python3
"""Release-policy gate of stable.yml (docs/release-policy.md).

Both stages refuse a withdrawn release number (R2) and a lock whose source
commits are not on the train's RELENG maintenance branch (R1). The seal stage
also refuses to seal sooner than stable_soak_hours after the live Stable
release unless the lock carries a soak_override (R4), and refuses to seal a
pair whose candidate build is not complete (R3).
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_lock
from stable_refresh import ORG, SOURCES, maintenance_branch

ROOT = Path(__file__).resolve().parents[1]


def fetch_json(url: str, token: str = "") -> dict | None:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "FreeSense-build/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise SystemExit(f"cannot read {url}: HTTP {error.code}") from error
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise SystemExit(f"cannot read {url}: {error}") from error


def on_branch(repository: str, sha: str, branch: str, token: str) -> bool:
    """True when sha is the branch head or one of its ancestors."""
    compare = fetch_json(f"https://api.github.com/repos/{ORG}/{repository}/compare/{sha}...{branch}", token)
    return compare is not None and compare.get("status") in ("ahead", "identical")


def check_sources(lock: dict, train: str, token: str) -> list[str]:
    branch = maintenance_branch(train)
    return [f"{repository}@{lock[key][:12]} is not on {branch} (R1)"
            for key, repository in SOURCES if not on_branch(repository, lock[key], branch, token)]


def live_stable(public_base_url: str) -> dict | None:
    envelope = fetch_json(f"{public_base_url}/repos.amd64.manifest.json")
    if envelope is None:
        return None
    payload = json.loads(base64.b64decode(envelope["payload"], validate=True))
    return payload.get("channels", {}).get("stable")


def check_soak(lock: dict, live: dict | None, soak_hours: int, now: datetime) -> list[str]:
    if live is None or live.get("version") == lock["release"] or lock.get("soak_override"):
        return []
    published = datetime.fromisoformat(live["system"]["published_at"].replace("Z", "+00:00"))
    waited = (now - published).total_seconds() / 3600
    if waited >= soak_hours:
        return []
    return [f"Stable {live.get('version')} went live {waited:.1f} h ago; seals are {soak_hours} h apart. "
            "A security or broken-update fix sets \"soak_override\": \"<reason>\" in its lock (R4)"]


def check_candidate(public_base_url: str, train: str, system: str, packages: str) -> list[str]:
    errors = []
    for label, url in (("System", f"{public_base_url}/artifacts/system/{system}/complete.json"),
                       ("Optional Packages", f"{public_base_url}/artifacts/packages/{train}/{packages}/complete.json")):
        if fetch_json(url) is None:
            errors.append(f"the {label} candidate {url} is not complete; run stage=candidate first (R3)")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--stage", choices=("candidate", "seal"), required=True)
    parser.add_argument("--system", action="append", default=[], help="System fingerprint (seal; per architecture)")
    parser.add_argument("--packages", action="append", default=[], help="Packages fingerprint (seal; per architecture)")
    args = parser.parse_args()
    policy = json.loads((ROOT / "config/build-policy.json").read_text(encoding="utf-8"))
    lock = release_lock.load(ROOT, policy, args.release)
    train = lock["package_train"]
    errors = []
    if args.release in release_lock.withdrawn(ROOT):
        errors.append(f"{args.release} is withdrawn; ship the next patch number instead (R2)")
    errors += check_sources(lock, train, os.environ.get("GITHUB_TOKEN", ""))
    if args.stage == "seal":
        base = policy["public_base_url"]
        if len(args.system) != len(args.packages) or not args.system:
            raise SystemExit("the seal gate needs one --system and --packages per architecture")
        for system, packages in zip(args.system, args.packages):
            errors += check_candidate(base, train, system, packages)
        errors += check_soak(lock, live_stable(base), int(policy["stable_soak_hours"]), datetime.now(timezone.utc))
    for error in errors:
        print(f"release policy: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"Stable {args.release} passes the release-policy gate for stage {args.stage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
