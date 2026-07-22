#!/usr/bin/env python3
"""Build the public website download index from a boot-tested ISO marker."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import urllib.error
import urllib.request


SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def version_tuple(value: str) -> tuple[int, int, int]:
    if not VERSION.fullmatch(value):
        raise SystemExit("invalid release version")
    return tuple(int(part) for part in value.split("."))


def fetch_json(url: str, missing=None):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "FreeSense-build/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return missing
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=("stable", "devel"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--source", required=True)
    parser.add_argument("--ports", required=True)
    parser.add_argument("--os-definition", required=True)
    parser.add_argument("--freebsd", required=True)
    parser.add_argument("--base-url", default="https://pkg.freesense.org/v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    requested_version = version_tuple(args.version)
    if not SHA256.fullmatch(args.fingerprint) or not SHA256.fullmatch(args.system):
        raise SystemExit("invalid artifact identity")
    if args.generation <= 0 or any(not SHA.fullmatch(value) for value in
        (args.source, args.ports, args.os_definition, args.freebsd)):
        raise SystemExit("invalid release provenance")
    if args.channel == "stable" and not args.version.startswith("1.0."):
        raise SystemExit("stable downloads must be exact 1.0.x releases")
    if args.channel == "devel" and not args.version.startswith("1.1."):
        raise SystemExit("development downloads must be 1.1.x releases")

    artifact_url = f"{args.base_url}/artifacts/iso/{args.fingerprint}"
    marker_url = artifact_url + "/complete.json"
    marker = fetch_json(marker_url)
    if (not isinstance(marker, dict) or marker.get("schema_version") != "freesense.iso/v1"
            or marker.get("fingerprint") != args.fingerprint
            or marker.get("system") != args.system
            or marker.get("generation") != args.generation
            or marker.get("inputs", {}).get("channel") != args.channel
            or not SHA256.fullmatch(marker.get("sha256", ""))
            or not isinstance(marker.get("size"), int) or marker["size"] <= 0
            or not isinstance(marker.get("file"), str)):
        raise SystemExit("boot-tested ISO marker does not match the release")

    index_url = f"{args.base_url}/releases.json"
    index = fetch_json(index_url, missing={
        "schema_version": "freesense.downloads/v1", "generated": None,
        "channels": {"stable": None, "devel": None},
    })
    if (not isinstance(index, dict) or index.get("schema_version") != "freesense.downloads/v1"
            or not isinstance(index.get("channels"), dict)):
        raise SystemExit("existing download index is invalid")
    existing = index["channels"].get(args.channel)
    if isinstance(existing, dict) and VERSION.fullmatch(existing.get("version", "")):
        current_version = version_tuple(existing["version"])
        if args.channel == "stable" and requested_version < current_version:
            raise SystemExit("stable download index cannot move backwards")
        if (args.channel == "stable" and requested_version == current_version
                and existing.get("fingerprint") != args.fingerprint):
            raise SystemExit("an immutable stable download cannot be rewritten")

    release_id = args.version if args.channel == "stable" else f"{args.version}-g{args.generation}"
    display_name = (f"FreeSense {args.version} Stable" if args.channel == "stable"
                    else f"FreeSense {args.version} Development — Generation {args.generation}")
    index["channels"].setdefault("stable", None)
    index["channels"].setdefault("devel", None)
    index["channels"][args.channel] = {
        "version": args.version,
        "release_id": release_id,
        "display_name": display_name,
        "support_tier": "supported" if args.channel == "stable" else "development",
        "channel": args.channel,
        "generation": args.generation,
        "fingerprint": args.fingerprint,
        "system": args.system,
        "iso": marker["file"],
        "marker_url": marker_url,
        "url": artifact_url + "/" + marker["file"],
        "size": marker["size"],
        "sha256": marker["sha256"],
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "provenance": {
            "source": args.source, "ports": args.ports,
            "os_definition": args.os_definition, "freebsd": args.freebsd,
        },
    }
    index["generated"] = index["channels"][args.channel]["published_at"]
    index.pop("temporary", None)
    args.output.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
