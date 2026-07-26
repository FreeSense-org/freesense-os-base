#!/usr/bin/env python3
"""Build one atomic channel document from boot-tested ISO and cloud markers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request


SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ISO_FILE = re.compile(r"^FreeSense-[A-Za-z0-9.-]+-amd64\.iso$")
DOWNLOAD_SCHEMA = "freesense.download/v2"
LEGACY_DOWNLOAD_SCHEMA = "freesense.download/v1"
DOWNLOAD_BASE_URL = "https://downloads.freesense.org/v1"
CHANGE_TYPES = {
    "security", "fix", "feature", "ui", "package", "documentation", "build", "other",
}
CHANGE_REPOSITORIES = (
    ("source", "FreeSense-org/freesense", "System"),
    ("system_ports", "FreeSense-org/freesense-system-ports", "System packages"),
    ("packages", "FreeSense-org/freesense-packages", "Optional packages"),
    ("os_definition", "FreeSense-org/freesense-os-base", "Build and installer"),
)


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


def classify_change(title: str) -> str:
    lowered = title.lower()
    if any(value in lowered for value in ("security", "cve-", "vulnerability")):
        return "security"
    if any(value in lowered for value in (
        "fix", "repair", "recover", "prevent", "correct", "regression", "broken",
    )):
        return "fix"
    if any(value in lowered for value in ("documentation", "docs:", "readme", "guide")):
        return "documentation"
    if any(value in lowered for value in ("webui", "website", "interface", " ui ")):
        return "ui"
    if any(value in lowered for value in ("package", "ports", "poudriere", "catalog")):
        return "package"
    if any(value in lowered for value in (
        "build", "workflow", "release", "runner", "broker", "publish", "pin freebsd", "ci:",
    )):
        return "build"
    if any(value in lowered for value in (
        "add", "enable", "support", "introduce", "implement", "allow", "increase",
    )):
        return "feature"
    return "other"


def github_compare(repository: str, before: str, after: str) -> list[dict[str, str]]:
    if before == after:
        return []
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "FreeSense-build/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/compare/{before}...{after}?per_page=100",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            comparison = json.load(response)
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise SystemExit(f"unable to build release notes for {repository}: {error}") from error
    commits = comparison.get("commits") if isinstance(comparison, dict) else None
    if not isinstance(commits, list):
        raise SystemExit(f"GitHub returned invalid release notes for {repository}")
    if comparison.get("total_commits", len(commits)) > len(commits):
        raise SystemExit(f"release notes for {repository} exceed the 100-commit safety limit")
    changes = []
    for commit in commits:
        message = commit.get("commit", {}).get("message", "") if isinstance(commit, dict) else ""
        title = message.splitlines()[0].strip()
        if not title:
            continue
        changes.append({"type": classify_change(title), "title": title[:180]})
    return changes


def build_changes(existing: dict | None, provenance: dict[str, str]) -> list[dict[str, str]]:
    if existing is None:
        return []
    if (existing.get("fingerprint") == provenance.get("fingerprint")
            and isinstance(existing.get("changes"), list)):
        return existing["changes"]
    previous = existing.get("provenance", {})
    if not isinstance(previous, dict):
        return []
    changes = []
    seen = set()
    for field, repository, scope in CHANGE_REPOSITORIES:
        before = previous.get(field, "")
        after = provenance.get(field, "")
        if not SHA.fullmatch(before) or not SHA.fullmatch(after) or before == after:
            continue
        for change in github_compare(repository, before, after):
            identity = (change["title"], scope)
            if identity in seen:
                continue
            seen.add(identity)
            changes.append({**change, "scope": scope})
    if (SHA.fullmatch(previous.get("freebsd", ""))
            and previous["freebsd"] != provenance["freebsd"]):
        changes.append({
            "type": "build",
            "title": "Advance the pinned FreeBSD source snapshot",
            "scope": "FreeBSD platform",
        })
    if (SHA.fullmatch(previous.get("ports", ""))
            and previous["ports"] != provenance["ports"]):
        changes.append({
            "type": "package",
            "title": "Advance the pinned FreeBSD ports snapshot",
            "scope": "FreeBSD packages",
        })
    return changes[:50]


def valid_changes(changes) -> bool:
    return (isinstance(changes, list) and len(changes) <= 50
            and all(isinstance(change, dict)
                    and change.get("type") in CHANGE_TYPES
                    and isinstance(change.get("title"), str)
                    and 0 < len(change["title"]) <= 180
                    and isinstance(change.get("scope"), str)
                    and 0 < len(change["scope"]) <= 80
                    for change in changes))


def release_identity(release, channel: str) -> str:
    return (release["version"] if channel == "stable"
            else f"{release['version']}-g{release['generation']}")


def public_iso_url(release, channel: str, download_base_url: str) -> str:
    return (f"{download_base_url}/releases/{channel}/"
            f"{release_identity(release, channel)}/{release['iso']}")


def public_artifact_url(release: dict, channel: str, file: str,
                        download_base_url: str) -> str:
    return (f"{download_base_url}/releases/{channel}/"
            f"{release_identity(release, channel)}/{file}")


def validate_download(release, channel: str, base_url: str,
                      download_base_url: str = DOWNLOAD_BASE_URL,
                      allow_legacy_url: bool = False) -> None:
    if not isinstance(release, dict):
        raise SystemExit(f"existing {channel} download document is invalid")
    if release.get("schema_version") == LEGACY_DOWNLOAD_SCHEMA:
        validate_legacy_download(
            release, channel, base_url, download_base_url, allow_legacy_url
        )
        return
    artifacts = release.get("artifacts")
    if (release.get("schema_version") != DOWNLOAD_SCHEMA
            or release.get("channel") != channel
            or not VERSION.fullmatch(release.get("version", ""))
            or not SHA256.fullmatch(release.get("bundle_fingerprint", ""))
            or not SHA256.fullmatch(release.get("system", ""))
            or not isinstance(release.get("generation"), int) or release["generation"] <= 0
            or not isinstance(artifacts, list) or len(artifacts) not in {3, 5}
            or not isinstance(release.get("published_at"), str)
            or ("changes" in release and not valid_changes(release["changes"]))):
        raise SystemExit(f"existing {channel} download document is invalid")
    expected = {
        ("installer", None, "iso"),
        ("cloud", "ufs", "qcow2"), ("cloud", "ufs", "raw"),
    }
    if len(artifacts) == 5:
        expected.update({("cloud", "zfs", "qcow2"), ("cloud", "zfs", "raw")})
    actual = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise SystemExit(f"existing {channel} download document has invalid artifacts")
        actual.add((artifact.get("kind"), artifact.get("filesystem"), artifact.get("format")))
        if (artifact.get("filesystem") not in {None, "ufs", "zfs"}
                or artifact.get("compression") not in {"none", "xz"}
                or not SHA256.fullmatch(artifact.get("sha256", ""))
                or not SHA256.fullmatch(artifact.get("build_fingerprint", ""))
                or not isinstance(artifact.get("size"), int) or artifact["size"] <= 0
                or not isinstance(artifact.get("file"), str)
                or artifact.get("url") != public_artifact_url(
                    release, channel, artifact["file"], download_base_url
                )
                or not isinstance(artifact.get("marker_url"), str)):
            raise SystemExit(f"existing {channel} download document has invalid artifacts")
        if artifact["format"] in {"qcow2", "raw"} and (
            artifact.get("virtual_size") != {
                "ufs": 16 * 1024 * 1024 * 1024,
                "zfs": 32 * 1024 * 1024 * 1024,
            }.get(artifact.get("filesystem"))
            or artifact.get("compression") != "xz"
        ):
            raise SystemExit(f"existing {channel} cloud artifact is invalid")
    if actual != expected or release.get("release_id") != release_identity(release, channel):
        raise SystemExit(f"existing {channel} download document has non-canonical URLs")
    provenance = release.get("provenance")
    if (not isinstance(provenance, dict)
            or any(not SHA.fullmatch(provenance.get(name, ""))
                   for name in ("source", "ports", "os_definition", "freebsd"))):
        raise SystemExit(f"existing {channel} download document has invalid provenance")
    if release.get("support_tier") not in {"supported", "development"}:
        raise SystemExit(f"existing {channel} download document has invalid lifecycle")


def validate_legacy_download(release, channel: str, base_url: str,
                             download_base_url: str, allow_legacy_url: bool) -> None:
    if (release.get("channel") != channel
            or not VERSION.fullmatch(release.get("version", ""))
            or not SHA256.fullmatch(release.get("fingerprint", ""))
            or not SHA256.fullmatch(release.get("system", ""))
            or not isinstance(release.get("generation"), int) or release["generation"] <= 0
            or not ISO_FILE.fullmatch(release.get("iso", ""))
            or not SHA256.fullmatch(release.get("sha256", ""))
            or not isinstance(release.get("size"), int) or release["size"] <= 0):
        raise SystemExit(f"existing {channel} legacy download document is invalid")
    artifact_url = f"{base_url}/artifacts/iso/{release['fingerprint']}"
    expected_url = public_iso_url(release, channel, download_base_url)
    if (release.get("release_id") != release_identity(release, channel)
            or release.get("marker_url") != artifact_url + "/complete.json"
            or (release.get("url") != expected_url
                and not (allow_legacy_url and release.get("url") == artifact_url + "/" + release["iso"]))):
        raise SystemExit(f"existing {channel} legacy download document has non-canonical URLs")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=("stable", "devel"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--bundle-fingerprint", required=True)
    parser.add_argument("--cloud-ufs-fingerprint", required=True)
    parser.add_argument("--cloud-zfs-fingerprint", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--source", required=True)
    parser.add_argument("--system-ports", required=True)
    parser.add_argument("--packages", required=True)
    parser.add_argument("--packages-fingerprint", required=True)
    parser.add_argument("--ports", required=True)
    parser.add_argument("--os-definition", required=True)
    parser.add_argument("--freebsd", required=True)
    parser.add_argument("--base-url", default="https://pkg.freesense.org/v1")
    parser.add_argument("--download-base-url", default=DOWNLOAD_BASE_URL)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    requested_version = version_tuple(args.version)
    policy = json.loads((Path(__file__).resolve().parents[1] / "config/build-policy.json").read_text())
    release_policy = policy["release"]
    expected_train = release_policy[
        "stable_train" if args.channel == "stable" else "development_train"
    ]
    if (not SHA256.fullmatch(args.fingerprint)
            or not SHA256.fullmatch(args.bundle_fingerprint)
            or not SHA256.fullmatch(args.cloud_ufs_fingerprint)
            or not SHA256.fullmatch(args.cloud_zfs_fingerprint)
            or not SHA256.fullmatch(args.system)
            or not SHA256.fullmatch(args.packages_fingerprint)):
        raise SystemExit("invalid artifact identity")
    if args.generation <= 0 or any(not SHA.fullmatch(value) for value in
        (args.source, args.system_ports, args.packages, args.ports,
         args.os_definition, args.freebsd)):
        raise SystemExit("invalid release provenance")
    if ".".join(args.version.split(".")[:2]) != expected_train:
        raise SystemExit(f"{args.channel} download does not match configured train {expected_train}")

    artifact_url = f"{args.base_url}/artifacts/iso/{args.fingerprint}"
    marker_url = artifact_url + "/complete.json"
    marker = fetch_json(marker_url)
    marker_inputs = marker.get("inputs", {}) if isinstance(marker, dict) else {}
    current_marker = (marker.get("schema_version") == "freesense.iso/v2"
                      and marker_inputs.get("packages") == args.packages_fingerprint
                      and marker.get("bundle_fingerprint") == args.bundle_fingerprint
                      ) if isinstance(marker, dict) else False
    if (not isinstance(marker, dict) or not current_marker
            or marker.get("fingerprint") != args.fingerprint
            or marker.get("system") != args.system
            or marker.get("generation") != args.generation
            or marker_inputs.get("channel") != args.channel
            or not SHA256.fullmatch(marker.get("sha256", ""))
            or not isinstance(marker.get("size"), int) or marker["size"] <= 0
            or not isinstance(marker.get("file"), str)):
        raise SystemExit("boot-tested ISO marker does not match the release")

    cloud_results = {}
    for filesystem, cloud_fingerprint in (
        ("ufs", args.cloud_ufs_fingerprint), ("zfs", args.cloud_zfs_fingerprint)
    ):
        cloud_artifact_url = f"{args.base_url}/artifacts/cloud/{cloud_fingerprint}"
        cloud_marker_url = cloud_artifact_url + "/complete.json"
        cloud_marker = fetch_json(cloud_marker_url)
        cloud_files = cloud_marker.get("files", []) if isinstance(cloud_marker, dict) else []
        expected_size = policy["cloud"]["variants"][filesystem]["virtual_size_gib"] * 1024**3
        if (not isinstance(cloud_marker, dict)
                or cloud_marker.get("schema_version") != "freesense.cloud-image/v1"
                or cloud_marker.get("fingerprint") != cloud_fingerprint
                or cloud_marker.get("filesystem") != filesystem
                or cloud_marker.get("bundle_fingerprint") != args.bundle_fingerprint
                or cloud_marker.get("generation") != args.generation
                or cloud_marker.get("system") not in {None, args.system}
                or cloud_marker.get("inputs", {}).get("system") != args.system
                or cloud_marker.get("inputs", {}).get("packages") != args.packages_fingerprint
                or cloud_marker.get("channel") != args.channel
                or cloud_marker.get("disk", {}).get("virtual_size") != expected_size
                or not isinstance(cloud_files, list) or len(cloud_files) != 2
                or {item.get("format") for item in cloud_files} != {"qcow2", "raw"}
                or any(item.get("virtual_size") != expected_size for item in cloud_files)):
            raise SystemExit(f"boot-tested {filesystem} cloud marker does not match the release bundle")
        cloud_results[filesystem] = (
            cloud_fingerprint, cloud_marker_url, cloud_files
        )

    release_url = f"{args.base_url}/releases/{args.channel}.json"
    existing = fetch_json(release_url, missing=None)
    if existing is not None:
        validate_download(existing, args.channel, args.base_url,
                          args.download_base_url, allow_legacy_url=True)
        current_version = version_tuple(existing["version"])
        if requested_version < current_version:
            raise SystemExit(f"{args.channel} download cannot move backwards")
        existing_identity = existing.get("bundle_fingerprint", existing.get("fingerprint"))
        if (args.channel == "stable" and requested_version == current_version
                and existing_identity != args.bundle_fingerprint):
            raise SystemExit("an immutable stable download cannot be rewritten")
        if (args.channel == "devel" and requested_version == current_version
                and args.generation < existing["generation"]):
            raise SystemExit("development download generation cannot move backwards")
        if (args.channel == "devel" and requested_version == current_version
                and args.generation == existing["generation"]
                and existing_identity != args.bundle_fingerprint):
            raise SystemExit("an immutable development generation cannot be rewritten")

    release_id = args.version if args.channel == "stable" else f"{args.version}-g{args.generation}"
    display_name = (f"FreeSense {args.version} Stable" if args.channel == "stable"
                    else f"FreeSense {args.version} Development — Generation {args.generation}")
    provenance = {
        "source": args.source,
        "system_ports": args.system_ports,
        "packages": args.packages,
        "ports": args.ports,
        "os_definition": args.os_definition,
        "freebsd": args.freebsd,
        "fingerprint": args.bundle_fingerprint,
    }
    release = {
        "schema_version": DOWNLOAD_SCHEMA,
        "version": args.version,
        "release_id": release_id,
        "display_name": display_name,
        "support_tier": "supported" if args.channel == "stable" else "development",
        "channel": args.channel,
        "generation": args.generation,
        "bundle_fingerprint": args.bundle_fingerprint,
        "system": args.system,
        "artifacts": [],
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "provenance": {key: value for key, value in provenance.items() if key != "fingerprint"},
        "changes": build_changes(existing, provenance),
    }
    release["artifacts"].append({
        "kind": "installer", "format": "iso", "filesystem": None,
        "compression": "none", "file": marker["file"],
        "url": public_artifact_url(release, args.channel, marker["file"], args.download_base_url),
        "marker_url": marker_url, "sha256": marker["sha256"], "size": marker["size"],
        "build_fingerprint": args.fingerprint,
    })
    for filesystem in ("ufs", "zfs"):
        cloud_fingerprint, cloud_marker_url, cloud_files = cloud_results[filesystem]
        for item in sorted(cloud_files, key=lambda value: value["format"]):
            release["artifacts"].append({
                "kind": "cloud", "format": item["format"], "filesystem": filesystem,
                "compression": "xz", "file": item["file"],
                "url": public_artifact_url(release, args.channel, item["file"], args.download_base_url),
                "marker_url": cloud_marker_url, "sha256": item["sha256"],
                "size": item["size"], "virtual_size": item["virtual_size"],
                "build_fingerprint": cloud_fingerprint,
            })
    if (existing is not None and existing["version"] == args.version
            and existing["generation"] == args.generation
            and existing.get("bundle_fingerprint") == args.bundle_fingerprint):
        release["published_at"] = existing["published_at"]
    validate_download(release, args.channel, args.base_url, args.download_base_url)
    args.output.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
