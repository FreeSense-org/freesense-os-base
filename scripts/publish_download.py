#!/usr/bin/env python3
"""Build one canonical channel download document from a boot-tested ISO marker."""

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
ISO_FILE = re.compile(r"^FreeSense-[A-Za-z0-9.-]+-amd64\.iso$")
DOWNLOAD_SCHEMA = "freesense.download/v1"
DOWNLOAD_BASE_URL = "https://downloads.freesense.org/v1"


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


def release_identity(release, channel: str) -> str:
    return (release["version"] if channel == "stable"
            else f"{release['version']}-g{release['generation']}")


def public_iso_url(release, channel: str, download_base_url: str) -> str:
    return (f"{download_base_url}/releases/{channel}/"
            f"{release_identity(release, channel)}/{release['iso']}")


def validate_download(release, channel: str, base_url: str,
                      download_base_url: str = DOWNLOAD_BASE_URL,
                      allow_legacy_url: bool = False) -> None:
    if (not isinstance(release, dict) or release.get("schema_version") != DOWNLOAD_SCHEMA
            or release.get("channel") != channel
            or not VERSION.fullmatch(release.get("version", ""))
            or not SHA256.fullmatch(release.get("fingerprint", ""))
            or not SHA256.fullmatch(release.get("system", ""))
            or not isinstance(release.get("generation"), int) or release["generation"] <= 0
            or not ISO_FILE.fullmatch(release.get("iso", ""))
            or not SHA256.fullmatch(release.get("sha256", ""))
            or not isinstance(release.get("size"), int) or release["size"] <= 0
            or not isinstance(release.get("published_at"), str)):
        raise SystemExit(f"existing {channel} download document is invalid")
    artifact_url = f"{base_url}/artifacts/iso/{release['fingerprint']}"
    legacy_url = artifact_url + "/" + release["iso"]
    expected_url = public_iso_url(release, channel, download_base_url)
    if (release.get("release_id") != release_identity(release, channel)
            or release.get("marker_url") != artifact_url + "/complete.json"
            or (release.get("url") != expected_url
                and not (allow_legacy_url and release.get("url") == legacy_url))):
        raise SystemExit(f"existing {channel} download document has non-canonical URLs")
    provenance = release.get("provenance")
    if (not isinstance(provenance, dict)
            or any(not SHA.fullmatch(provenance.get(name, ""))
                   for name in ("source", "ports", "os_definition", "freebsd"))):
        raise SystemExit(f"existing {channel} download document has invalid provenance")
    if channel == "stable" and (not release["version"].startswith("1.0.")
            or release.get("support_tier") != "supported"):
        raise SystemExit("stable download document violates the 1.0.x policy")
    if channel == "devel" and (not release["version"].startswith("1.1.")
            or release.get("support_tier") != "development"):
        raise SystemExit("development download document violates the 1.1.x policy")


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
    parser.add_argument("--download-base-url", default=DOWNLOAD_BASE_URL)
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

    release_url = f"{args.base_url}/releases/{args.channel}.json"
    existing = fetch_json(release_url, missing=None)
    if existing is not None:
        validate_download(existing, args.channel, args.base_url,
                          args.download_base_url, allow_legacy_url=True)
        current_version = version_tuple(existing["version"])
        if requested_version < current_version:
            raise SystemExit(f"{args.channel} download cannot move backwards")
        if (args.channel == "stable" and requested_version == current_version
                and existing.get("fingerprint") != args.fingerprint):
            raise SystemExit("an immutable stable download cannot be rewritten")
        if (args.channel == "devel" and requested_version == current_version
                and args.generation < existing["generation"]):
            raise SystemExit("development download generation cannot move backwards")
        if (args.channel == "devel" and requested_version == current_version
                and args.generation == existing["generation"]
                and existing.get("fingerprint") != args.fingerprint):
            raise SystemExit("an immutable development generation cannot be rewritten")

    release_id = args.version if args.channel == "stable" else f"{args.version}-g{args.generation}"
    display_name = (f"FreeSense {args.version} Stable" if args.channel == "stable"
                    else f"FreeSense {args.version} Development — Generation {args.generation}")
    release = {
        "schema_version": DOWNLOAD_SCHEMA,
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
        "url": "",
        "size": marker["size"],
        "sha256": marker["sha256"],
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "provenance": {
            "source": args.source, "ports": args.ports,
            "os_definition": args.os_definition, "freebsd": args.freebsd,
        },
    }
    release["url"] = public_iso_url(release, args.channel, args.download_base_url)
    if (existing is not None and existing["version"] == args.version
            and existing["generation"] == args.generation
            and existing["fingerprint"] == args.fingerprint):
        release["published_at"] = existing["published_at"]
    validate_download(release, args.channel, args.base_url, args.download_base_url)
    args.output.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
