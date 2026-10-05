#!/usr/bin/env python3
"""Decide whether this Development run also releases images.

Devices get System and Packages every day. ISO, cloud and appliance images and
the website release follow the FreeBSD pin: they are due when the live release
of either architecture was built on a different pin than this run's pair (a
pin rollover or mirror re-cut), or does not exist yet. A run that fails to
release them leaves them due, so the next day retries.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_platform import load_policy  # noqa: E402

ARCHES = ("amd64", "arm64")
USER_AGENT = "FreeSense-build/1"


def fetch_json(url: str) -> dict | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def released_pin(base: str, arch: str, read=fetch_json) -> str | None:
    """The FreeBSD pin the live image release of one architecture was built on."""
    release = read(f"{base}/releases/devel.{arch}.json")
    if not release or not release.get("system"):
        return None
    marker = read(f"{base}/artifacts/system/{release['system']}/complete.json") or {}
    return marker.get("inputs", {}).get("freebsd_pin_id")


def decide(plan: dict, mode: str, base: str, read=fetch_json) -> tuple[bool, str]:
    if mode == "force":
        return True, "forced by dispatch"
    if mode == "skip":
        return False, "skipped by dispatch"
    if mode != "auto":
        raise ValueError(f"invalid images mode: {mode}")
    for arch in ARCHES:
        planned = plan["targets"][arch]["system"]["freebsd_pin_id"]
        live = released_pin(base, arch, read)
        if live is None:
            return True, f"no {arch} image release yet"
        if live != planned:
            return True, f"{arch} images were built on FreeBSD pin {live[:12]}, this pair is on {planned[:12]}"
    return False, "the live images are on this run's FreeBSD pin"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--mode", default="auto", choices=("auto", "force", "skip"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    due, reason = decide(json.loads(args.plan.read_text(encoding="utf-8")), args.mode,
                         load_policy()["public_base_url"])
    print(f"Images due: {str(due).lower()} ({reason})")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"due={str(due).lower()}\nreason={reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
