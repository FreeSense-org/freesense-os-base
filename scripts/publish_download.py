#!/usr/bin/env python3
"""Build one atomic channel document from boot-tested ISO and cloud markers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_platform import image_profile, load_policy, target


SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ISO_FILE = re.compile(r"^FreeSense-[A-Za-z0-9.-]+-amd64\.iso$")
DOWNLOAD_SCHEMA = "freesense.download/v4"
V3_DOWNLOAD_SCHEMA = "freesense.download/v3"
V2_DOWNLOAD_SCHEMA = "freesense.download/v2"
LEGACY_DOWNLOAD_SCHEMA = "freesense.download/v1"
RELEASE_NOTES_SCHEMA = "freesense.release-notes/v2"
DOWNLOAD_BASE_URL = "https://downloads.freesense.org/v1"
MAX_PACKAGE_CHANGES = 200
MAX_CATALOG_BYTES = 64 * 1024 * 1024
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_,.@~-]{0,127}$")
CHANGE_TYPES = {
    "security", "fix", "feature", "ui", "package", "documentation", "build", "other",
}
CHANGE_REPOSITORIES = (
    ("source", "FreeSense-org/freesense", "System"),
    ("system_ports", "FreeSense-org/freesense-system-ports", "System packages"),
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


def fetch_bytes(url: str, maximum: int = MAX_CATALOG_BYTES) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "FreeSense-build/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > maximum:
                raise SystemExit("System package catalog exceeds the size limit")
            body = response.read(maximum + 1)
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise SystemExit(f"unable to fetch System package catalog: {error}") from error
    if len(body) > maximum:
        raise SystemExit("System package catalog exceeds the size limit")
    return body


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
            if change["type"] == "build":
                continue
            identity = (change["title"], scope)
            if identity in seen:
                continue
            seen.add(identity)
            changes.append({**change, "scope": scope})
    return changes[:50]


def system_package_inventory(base_url: str, fingerprint: str, package_arch: str = "amd64") -> dict[str, dict[str, str]]:
    if not SHA256.fullmatch(fingerprint):
        raise SystemExit("invalid System fingerprint for package inventory")
    url = f"{base_url}/artifacts/system/{fingerprint}/{package_arch}/packagesite.pkg"
    archive = fetch_bytes(url)
    with tempfile.TemporaryDirectory(prefix="freesense-packagesite-") as directory:
        path = Path(directory, "packagesite.pkg")
        path.write_bytes(archive)
        result = subprocess.run(
            ["tar", "--zstd", "-xOf", str(path), "packagesite.yaml"],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
    if result.returncode != 0:
        raise SystemExit("unable to extract the signed System package catalog")
    inventory: dict[str, dict[str, str]] = {}
    try:
        for line in result.stdout.splitlines():
            if not line:
                continue
            record = json.loads(line)
            name = record.get("name", "")
            version = record.get("version", "")
            origin = record.get("origin", "")
            if (not PACKAGE_NAME.fullmatch(name) or not isinstance(version, str)
                    or not version or len(version) > 120 or not isinstance(origin, str)
                    or len(origin) > 180 or name in inventory):
                raise ValueError("invalid package record")
            inventory[name] = {"version": version, "origin": origin}
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit("System package catalog contains invalid metadata") from error
    if not inventory:
        raise SystemExit("System package catalog is empty")
    return inventory


def package_changes(existing: dict | None, system: str, base_url: str, package_arch: str = "amd64") -> dict:
    empty = {
        "available": existing is not None,
        "updated": [], "added": [], "removed": [],
        "counts": {"updated": 0, "added": 0, "removed": 0},
        "truncated": False,
    }
    if existing is None:
        return empty
    previous_system = existing.get("system", "")
    if not SHA256.fullmatch(previous_system):
        raise SystemExit("existing release has an invalid System identity")
    if previous_system == system:
        return empty
    before = system_package_inventory(base_url, previous_system, package_arch)
    after = system_package_inventory(base_url, system, package_arch)
    updated = [
        {"name": name, "from": before[name]["version"],
         "to": after[name]["version"], "origin": after[name]["origin"]}
        for name in sorted(before.keys() & after.keys())
        if before[name]["version"] != after[name]["version"]
    ]
    added = [
        {"name": name, "version": after[name]["version"], "origin": after[name]["origin"]}
        for name in sorted(after.keys() - before.keys())
    ]
    removed = [
        {"name": name, "version": before[name]["version"], "origin": before[name]["origin"]}
        for name in sorted(before.keys() - after.keys())
    ]
    counts = {"updated": len(updated), "added": len(added), "removed": len(removed)}
    remaining = MAX_PACKAGE_CHANGES
    visible = {}
    for kind, values in (("updated", updated), ("added", added), ("removed", removed)):
        visible[kind] = values[:remaining]
        remaining -= len(visible[kind])
    return {
        "available": True,
        **visible,
        "counts": counts,
        "truncated": sum(counts.values()) > MAX_PACKAGE_CHANGES,
    }


def build_release_notes(existing: dict | None, provenance: dict[str, str],
                        system: str, base_url: str,
                        package_arch: str = "amd64") -> dict:
    if (existing is not None
            and existing.get("bundle_fingerprint") == provenance.get("fingerprint")
            and valid_release_notes(existing.get("release_notes"))):
        return existing["release_notes"]
    previous = existing.get("provenance", {}) if isinstance(existing, dict) else {}
    from_freebsd = previous.get("freebsd") if SHA.fullmatch(previous.get("freebsd", "")) else None
    from_ports = previous.get("ports") if SHA.fullmatch(previous.get("ports", "")) else None
    return {
        "schema_version": RELEASE_NOTES_SCHEMA,
        "baseline_release_id": existing.get("release_id") if isinstance(existing, dict) else None,
        "freesense": build_changes(existing, provenance),
        "platform": {
            "freebsd": {
                "changed": from_freebsd is not None and from_freebsd != provenance["freebsd"],
                "ports_changed": from_ports is not None and from_ports != provenance["ports"],
                "from_commit": from_freebsd,
                "to_commit": provenance["freebsd"],
                "from_ports_commit": from_ports,
                "to_ports_commit": provenance["ports"],
            },
            "packages": package_changes(existing, system, base_url, package_arch),
        },
    }


def valid_changes(changes) -> bool:
    return (isinstance(changes, list) and len(changes) <= 50
            and all(isinstance(change, dict)
                    and change.get("type") in CHANGE_TYPES
                    and isinstance(change.get("title"), str)
                    and 0 < len(change["title"]) <= 180
                    and isinstance(change.get("scope"), str)
                    and 0 < len(change["scope"]) <= 80
                    for change in changes))


def valid_release_notes(notes) -> bool:
    if not isinstance(notes, dict) or notes.get("schema_version") != RELEASE_NOTES_SCHEMA:
        return False
    baseline = notes.get("baseline_release_id")
    if not (baseline is None or isinstance(baseline, str) and 0 < len(baseline) <= 80):
        return False
    platform = notes.get("platform")
    if not valid_changes(notes.get("freesense")) or not isinstance(platform, dict):
        return False
    freebsd = platform.get("freebsd")
    packages = platform.get("packages")
    if (not isinstance(freebsd, dict)
            or not isinstance(freebsd.get("changed"), bool)
            or not isinstance(freebsd.get("ports_changed"), bool)
            or any(not SHA.fullmatch(freebsd.get(name, ""))
                   for name in ("to_commit", "to_ports_commit"))
            or any(value is not None and not SHA.fullmatch(value)
                   for value in (freebsd.get("from_commit"), freebsd.get("from_ports_commit")))):
        return False
    if (freebsd["changed"] != (freebsd.get("from_commit") is not None
                               and freebsd["from_commit"] != freebsd["to_commit"])
            or freebsd["ports_changed"] != (freebsd.get("from_ports_commit") is not None
                                             and freebsd["from_ports_commit"] != freebsd["to_ports_commit"])):
        return False
    if (not isinstance(packages, dict) or not isinstance(packages.get("available"), bool)
            or not isinstance(packages.get("truncated"), bool)
            or not isinstance(packages.get("counts"), dict)):
        return False
    total_visible = 0
    for kind in ("updated", "added", "removed"):
        values = packages.get(kind)
        count = packages["counts"].get(kind)
        if (not isinstance(values, list) or type(count) is not int
                or count < len(values) or count > 100000):
            return False
        total_visible += len(values)
        for item in values:
            if (not isinstance(item, dict) or not PACKAGE_NAME.fullmatch(item.get("name", ""))
                    or not isinstance(item.get("origin"), str) or len(item["origin"]) > 180):
                return False
            versions = ("from", "to") if kind == "updated" else ("version",)
            if any(not isinstance(item.get(name), str) or not item[name]
                   or len(item[name]) > 120 for name in versions):
                return False
    total_count = sum(packages["counts"].values())
    return (total_visible <= MAX_PACKAGE_CHANGES
            and packages["truncated"] == (total_count > total_visible))


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
    schema = release.get("schema_version")
    if schema == DOWNLOAD_SCHEMA:
        validate_v4_download(release, channel, base_url, download_base_url)
        return
    if (schema not in {V2_DOWNLOAD_SCHEMA, V3_DOWNLOAD_SCHEMA}
            or release.get("channel") != channel
            or not VERSION.fullmatch(release.get("version", ""))
            or not SHA256.fullmatch(release.get("bundle_fingerprint", ""))
            or not SHA256.fullmatch(release.get("system", ""))
            or not isinstance(release.get("generation"), int) or release["generation"] <= 0
            or not isinstance(artifacts, list) or len(artifacts) not in {1, 3, 5}
            or not isinstance(release.get("published_at"), str)
            or ("changes" in release and not valid_changes(release["changes"]))
            or ("release_notes" in release
                and not valid_release_notes(release["release_notes"]))):
        raise SystemExit(f"existing {channel} download document is invalid")
    if schema == V3_DOWNLOAD_SCHEMA:
        architecture = release.get("architecture")
        package_arch = release.get("package_arch")
        if ((architecture, package_arch) not in {("amd64", "amd64"), ("arm64", "aarch64")}
                or not isinstance(release.get("platform"), str)
                or not isinstance(release.get("firmware"), list)
                or not isinstance(release.get("capabilities"), dict)):
            raise SystemExit(f"existing {channel} download architecture is invalid")
    installer_format = "img" if release.get("architecture") == "arm64" else "iso"
    installer_only = (schema == V3_DOWNLOAD_SCHEMA and len(artifacts) == 1
                      and release.get("capabilities", {}).get("cloud_init") is False)
    if len(artifacts) == 1 and not installer_only:
        raise SystemExit(f"existing {channel} download document has invalid artifacts")
    expected = {("installer", None, installer_format)}
    if not installer_only:
        expected.update({("cloud", "ufs", "qcow2"), ("cloud", "ufs", "raw")})
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


def validate_v4_download(release: dict, channel: str, base_url: str,
                         download_base_url: str) -> None:
    artifacts = release.get("artifacts")
    if (release.get("channel") != channel
            or not VERSION.fullmatch(release.get("version", ""))
            or not SHA256.fullmatch(release.get("bundle_fingerprint", ""))
            or not SHA256.fullmatch(release.get("system", ""))
            or (release.get("architecture"), release.get("package_arch")) not in {
                ("amd64", "amd64"), ("arm64", "aarch64")}
            or not isinstance(release.get("generation"), int) or release["generation"] <= 0
            or not isinstance(artifacts, list) or not artifacts
            or not isinstance(release.get("published_at"), str)
            or release.get("release_id") != release_identity(release, channel)):
        raise SystemExit(f"existing {channel} v4 download document is invalid")
    identities = set()
    for artifact in artifacts:
        required = {
            "kind", "platform", "target_models", "filesystem", "format", "compression",
            "partition_scheme", "firmware", "capabilities", "boot_inputs",
            "artifact_fingerprint", "sha256", "size", "file", "url", "marker_url",
            "hardware_verification",
        }
        identity = artifact.get("artifact_fingerprint") if isinstance(artifact, dict) else ""
        if (not isinstance(artifact, dict) or not required.issubset(artifact)
                or artifact.get("kind") not in {"installer", "cloud", "appliance"}
                or not isinstance(artifact.get("platform"), str)
                or not isinstance(artifact.get("target_models"), list)
                or artifact.get("filesystem") not in {None, "ufs", "zfs"}
                or artifact.get("format") not in {"iso", "img", "qcow2", "raw"}
                or artifact.get("compression") not in {"none", "xz"}
                or artifact.get("partition_scheme") not in {"gpt", "mbr"}
                or not isinstance(artifact.get("firmware"), list)
                or not isinstance(artifact.get("capabilities"), dict)
                or not isinstance(artifact.get("boot_inputs"), dict)
                or not SHA256.fullmatch(identity or "")
                or not SHA256.fullmatch(artifact.get("sha256", ""))
                or not isinstance(artifact.get("size"), int) or artifact["size"] <= 0
                or artifact.get("hardware_verification") not in {"unverified", "verified"}
                or artifact.get("url") != public_artifact_url(
                    release, channel, artifact.get("file", ""), download_base_url)
                or not isinstance(artifact.get("marker_url"), str)):
            raise SystemExit(f"existing {channel} v4 artifact is invalid")
        identities.add(identity)
        if artifact["kind"] == "appliance" and (
                artifact["filesystem"] != "ufs" or artifact["format"] != "img"
                or artifact["compression"] != "xz" or artifact["partition_scheme"] != "mbr"
                or not artifact["target_models"]):
            raise SystemExit(f"existing {channel} appliance artifact is invalid")
    if artifacts[0]["kind"] != "installer":
        raise SystemExit(f"existing {channel} v4 artifact order is invalid")
    provenance = release.get("provenance")
    if (not isinstance(provenance, dict) or any(
            not SHA.fullmatch(provenance.get(name, ""))
            for name in ("source", "ports", "os_definition", "freebsd"))):
        raise SystemExit(f"existing {channel} v4 provenance is invalid")


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
    parser.add_argument("--cloud-ufs-fingerprint", default="")
    parser.add_argument("--cloud-zfs-fingerprint", default="")
    parser.add_argument("--appliance-fingerprint", action="append", default=[])
    parser.add_argument("--hardware-verified-profile", action="append", default=[])
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
    parser.add_argument("--target", default="amd64")
    parser.add_argument("--image-profile")
    args = parser.parse_args()

    requested_version = version_tuple(args.version)
    policy = load_policy(Path(__file__).resolve().parents[1] / "config/build-policy.json")
    selected_target = target(policy, args.target)
    selected_profile = image_profile(policy, args.image_profile, args.target)
    cloud_enabled = selected_profile["capabilities"].get("cloud_init") is True
    if not selected_target["publish_enabled"]:
        raise SystemExit(f"publication for target {args.target} is disabled by policy")
    release_policy = policy["release"]
    expected_train = release_policy[
        "stable_train" if args.channel == "stable" else "development_train"
    ]
    if (not SHA256.fullmatch(args.fingerprint)
            or not SHA256.fullmatch(args.bundle_fingerprint)
            or not SHA256.fullmatch(args.system)
            or not SHA256.fullmatch(args.packages_fingerprint)):
        raise SystemExit("invalid artifact identity")
    cloud_fingerprints = (args.cloud_ufs_fingerprint, args.cloud_zfs_fingerprint)
    if ((cloud_enabled and any(not SHA256.fullmatch(value) for value in cloud_fingerprints))
            or (not cloud_enabled and any(cloud_fingerprints))):
        raise SystemExit("cloud artifact identities do not match the image profile")
    if any(value and not SHA256.fullmatch(value) for value in args.appliance_fingerprint):
        raise SystemExit("invalid appliance artifact identity")
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
    marker_schema = "freesense.iso/v2" if selected_profile["installer"] == "iso" else "freesense.installer/v1"
    current_marker = (marker.get("schema_version") == marker_schema
                      and marker_inputs.get("packages") == args.packages_fingerprint
                      ) if isinstance(marker, dict) else False
    if (not isinstance(marker, dict) or not current_marker
            or marker.get("fingerprint") != args.fingerprint
            or marker.get("system") != args.system
            or not isinstance(marker.get("generation"), int) or marker["generation"] <= 0
            or not SHA256.fullmatch(marker.get("bundle_fingerprint", ""))
            or marker_inputs.get("channel") != args.channel
            or not SHA256.fullmatch(marker.get("sha256", ""))
            or not isinstance(marker.get("size"), int) or marker["size"] <= 0
            or not isinstance(marker.get("file"), str)):
        raise SystemExit("boot-tested installer marker does not match the release")

    cloud_results = {}
    cloud_identities = (("ufs", args.cloud_ufs_fingerprint),
                        ("zfs", args.cloud_zfs_fingerprint)) if cloud_enabled else ()
    for filesystem, cloud_fingerprint in cloud_identities:
        cloud_artifact_url = f"{args.base_url}/artifacts/cloud/{cloud_fingerprint}"
        cloud_marker_url = cloud_artifact_url + "/complete.json"
        cloud_marker = fetch_json(cloud_marker_url)
        cloud_files = cloud_marker.get("files", []) if isinstance(cloud_marker, dict) else []
        expected_size = selected_profile["variants"][filesystem]["virtual_size_gib"] * 1024**3
        if (not isinstance(cloud_marker, dict)
                or cloud_marker.get("schema_version") != "freesense.cloud-image/v1"
                or cloud_marker.get("fingerprint") != cloud_fingerprint
                or cloud_marker.get("filesystem") != filesystem
                or not SHA256.fullmatch(cloud_marker.get("bundle_fingerprint", ""))
                or not isinstance(cloud_marker.get("generation"), int)
                or cloud_marker["generation"] <= 0
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

    appliance_results = []
    for appliance_fingerprint in filter(None, args.appliance_fingerprint):
        appliance_url = f"{args.base_url}/artifacts/appliance/{appliance_fingerprint}"
        appliance_marker_url = appliance_url + "/complete.json"
        appliance_marker = fetch_json(appliance_marker_url)
        if (not isinstance(appliance_marker, dict)
                or appliance_marker.get("schema_version") != "freesense.appliance/v1"
                or appliance_marker.get("fingerprint") != appliance_fingerprint
                or not SHA256.fullmatch(appliance_marker.get("bundle_fingerprint", ""))
                or not isinstance(appliance_marker.get("generation"), int)
                or appliance_marker["generation"] <= 0
                or appliance_marker.get("channel") != args.channel
                or appliance_marker.get("architecture") != selected_target["architecture"]
                or appliance_marker.get("package_arch") != selected_target["package_arch"]
                or appliance_marker.get("inputs", {}).get("system") != args.system
                or appliance_marker.get("inputs", {}).get("packages") != args.packages_fingerprint
                or appliance_marker.get("filesystem") != "ufs"
                or appliance_marker.get("format") != "img"
                or appliance_marker.get("compression") != "xz"
                or appliance_marker.get("partition_scheme") != "mbr"
                or appliance_marker.get("hardware_verification") not in {"unverified", "verified"}
                or not SHA256.fullmatch(appliance_marker.get("sha256", ""))
                or not isinstance(appliance_marker.get("size"), int)
                or appliance_marker["size"] <= 0):
            raise SystemExit("structurally verified appliance marker does not match the release bundle")
        profile = image_profile(policy, appliance_marker.get("platform"), args.target)
        if profile.get("kind") != "appliance" or profile["boot_inputs"] != appliance_marker.get("boot_inputs"):
            raise SystemExit("appliance marker boot provenance does not match policy")
        if profile["boot_inputs"].get("redistribution_review") not in (None, "complete"):
            raise SystemExit(f"{profile['name']} redistribution review is incomplete")
        appliance_results.append((profile, appliance_fingerprint,
                                  appliance_marker_url, appliance_marker))

    release_url = f"{args.base_url}/releases/{args.channel}.{selected_target['architecture']}.json"
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
    release_notes = build_release_notes(
        existing, provenance, args.system, args.base_url,
        selected_target["package_arch"],
    )
    release = {
        "schema_version": DOWNLOAD_SCHEMA,
        "architecture": selected_target["architecture"],
        "package_arch": selected_target["package_arch"],
        "platform": selected_profile["name"],
        "firmware": selected_profile["firmware"],
        "capabilities": selected_profile["capabilities"],
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
        # Keep the filtered flat list for older appliances and the website.
        "changes": release_notes["freesense"],
        "release_notes": release_notes,
    }
    release["artifacts"].append({
        "kind": "installer", "format": selected_profile["installer"], "filesystem": None,
        "compression": "none" if selected_profile["installer"] == "iso" else "xz", "file": marker["file"],
        "url": public_artifact_url(release, args.channel, marker["file"], args.download_base_url),
        "marker_url": marker_url, "sha256": marker["sha256"], "size": marker["size"],
        "build_fingerprint": args.fingerprint, "artifact_fingerprint": args.fingerprint,
        "platform": selected_profile["name"], "target_models": [],
        "partition_scheme": selected_profile["partition_scheme"],
        "firmware": selected_profile["firmware"], "capabilities": selected_profile["capabilities"],
        "boot_inputs": {}, "hardware_verification": "verified",
    })
    for filesystem in cloud_results:
        cloud_fingerprint, cloud_marker_url, cloud_files = cloud_results[filesystem]
        for item in sorted(cloud_files, key=lambda value: value["format"]):
            release["artifacts"].append({
                "kind": "cloud", "format": item["format"], "filesystem": filesystem,
                "compression": "xz", "file": item["file"],
                "url": public_artifact_url(release, args.channel, item["file"], args.download_base_url),
                "marker_url": cloud_marker_url, "sha256": item["sha256"],
                "size": item["size"], "virtual_size": item["virtual_size"],
                "build_fingerprint": cloud_fingerprint, "artifact_fingerprint": cloud_fingerprint,
                "platform": selected_profile["name"], "target_models": [],
                "partition_scheme": selected_profile["partition_scheme"],
                "firmware": selected_profile["firmware"], "capabilities": selected_profile["capabilities"],
                "boot_inputs": {}, "hardware_verification": "verified",
            })
    requested_verified = set(args.hardware_verified_profile)
    known_profiles = {profile["name"] for profile, *_ in appliance_results}
    if not requested_verified.issubset(known_profiles):
        raise SystemExit("hardware verification promotion names an unknown appliance profile")
    existing_status = {
        artifact.get("platform"): artifact.get("hardware_verification")
        for artifact in (existing or {}).get("artifacts", [])
        if isinstance(artifact, dict) and artifact.get("kind") == "appliance"
    }
    for profile, appliance_fingerprint, appliance_marker_url, appliance_marker in appliance_results:
        status = appliance_marker["hardware_verification"]
        if existing_status.get(profile["name"]) == "verified" or profile["name"] in requested_verified:
            status = "verified"
        release["artifacts"].append({
            "kind": "appliance", "platform": profile["name"],
            "target_models": profile["target_models"], "filesystem": "ufs",
            "format": "img", "compression": "xz",
            "partition_scheme": profile["partition_scheme"],
            "firmware": profile["firmware"], "capabilities": profile["capabilities"],
            "boot_inputs": profile["boot_inputs"],
            "artifact_fingerprint": appliance_fingerprint,
            "build_fingerprint": appliance_fingerprint,
            "file": appliance_marker["file"],
            "url": public_artifact_url(release, args.channel, appliance_marker["file"], args.download_base_url),
            "marker_url": appliance_marker_url, "sha256": appliance_marker["sha256"],
            "size": appliance_marker["size"], "hardware_verification": status,
        })
    if (existing is not None and existing["version"] == args.version
            and existing["generation"] == args.generation
            and existing.get("bundle_fingerprint") == args.bundle_fingerprint):
        if existing.get("schema_version") == DOWNLOAD_SCHEMA:
            immutable = lambda item: {key: value for key, value in item.items()
                                      if key != "hardware_verification"}
            artifact_key = lambda item: (
                item["artifact_fingerprint"], item["format"], item["file"])
            old = {artifact_key(item): immutable(item)
                   for item in existing["artifacts"]}
            new = {artifact_key(item): immutable(item)
                   for item in release["artifacts"]}
            if old != new:
                raise SystemExit("an immutable generation permits only hardware-verification promotion")
            for item in release["artifacts"]:
                previous = next(old_item for old_item in existing["artifacts"]
                                if artifact_key(old_item) == artifact_key(item))
                if (previous["hardware_verification"] == "verified"
                        and item["hardware_verification"] != "verified"):
                    raise SystemExit("hardware verification cannot be reversed")
        release["published_at"] = existing["published_at"]
    validate_download(release, args.channel, args.base_url, args.download_base_url)
    args.output.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
