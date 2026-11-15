#!/usr/bin/env python3
"""Keep the rolling Development release-notes log up to date on every build.

The download document (devel.<arch>.json) carries the notes the WebUI shows, but
it is only rewritten when images are published, which follows the FreeBSD pin.
Devices update from the repositories every day, so their "What is new" went
stale. This log is rewritten on every Development publication, repositories
only or with images, and holds one entry per build:

    {"schema_version": "freesense.release-notes-log/v1", "channel": "devel",
     "updated_at": ..., "entries": [newest first, at most LOG_LIMIT]}

An entry records what changed since the entry before it (commit titles of the
three product repositories plus the FreeBSD and package diff). A build whose
inputs equal the newest entry adds nothing.

The first run has no log; it takes its baseline from the current download
document so that the first entry covers everything since that image release.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_download as pd  # noqa: E402

LOG_SCHEMA = "freesense.release-notes-log/v1"
LOG_LIMIT = 30
CHANGES_PER_ENTRY = 50
PACKAGES_PER_LIST = 50
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PROVENANCE_FIELDS = ("source", "system_ports", "packages", "freebsd", "ports")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_provenance(plan: dict, architecture: str) -> dict[str, str]:
    target = plan["targets"][architecture]
    system, packages = target["system"], target["packages"]
    values = {
        "source": system["source_sha"], "system_ports": system["system_sha"],
        "packages": packages["packages_sha"], "freebsd": system["freebsd_sha"],
        "ports": system["ports_sha"],
    }
    for field, value in values.items():
        if not SHA.fullmatch(value):
            raise SystemExit(f"plan has an invalid {field} commit")
    return values


def baseline_from_release(document: dict | None) -> dict | None:
    """The newest log entry's stand-in when there is no log yet."""
    if not isinstance(document, dict):
        return None
    provenance = document.get("provenance")
    system = document.get("system", "")
    if (not isinstance(provenance, dict) or not SHA256.fullmatch(system)
            or any(not SHA.fullmatch(provenance.get(field, "")) for field in PROVENANCE_FIELDS)):
        return None
    return {"generation": document.get("generation"), "system": system,
            "packages_fingerprint": document.get("packages_fingerprint", ""),
            "provenance": {field: provenance[field] for field in PROVENANCE_FIELDS}}


def same_inputs(entry: dict, provenance: dict, system: str, packages_fingerprint: str) -> bool:
    return (entry.get("system") == system
            and entry.get("packages_fingerprint") == packages_fingerprint
            and entry.get("provenance") == provenance)


def repository_changes(previous: dict, provenance: dict) -> list[dict]:
    changes, seen = [], set()
    for field, repository, scope in pd.CHANGE_REPOSITORIES:
        before, after = previous.get(field, ""), provenance.get(field, "")
        if not SHA.fullmatch(before) or not SHA.fullmatch(after) or before == after:
            continue
        for change in pd.github_compare(repository, before, after):
            if change["type"] == "build":
                continue
            identity = (change["title"], scope)
            if identity in seen:
                continue
            seen.add(identity)
            changes.append({**change, "scope": scope})
    return changes[:CHANGES_PER_ENTRY]


def platform_changes(previous: dict, provenance: dict, system: str,
                     base_url: str, package_arch: str) -> dict:
    before_freebsd = previous["provenance"].get("freebsd")
    before_ports = previous["provenance"].get("ports")
    packages = pd.package_changes({"system": previous["system"]}, system, base_url, package_arch)
    for key in ("updated", "added", "removed"):
        if len(packages[key]) > PACKAGES_PER_LIST:
            packages[key] = packages[key][:PACKAGES_PER_LIST]
            packages["truncated"] = True
    return {
        "freebsd": {
            "changed": before_freebsd != provenance["freebsd"],
            "ports_changed": before_ports != provenance["ports"],
            "from_commit": before_freebsd, "to_commit": provenance["freebsd"],
            "from_ports_commit": before_ports, "to_ports_commit": provenance["ports"],
        },
        "packages": packages,
    }


def new_entry(previous: dict | None, generation: int, provenance: dict, system: str,
              packages_fingerprint: str, base_url: str, package_arch: str, version: str) -> dict:
    entry = {
        "generation": generation, "version": version, "published_at": now_utc(),
        "system": system, "packages_fingerprint": packages_fingerprint,
        "provenance": provenance, "changes": [], "platform": None,
    }
    if previous is not None:
        entry["changes"] = repository_changes(previous["provenance"], provenance)
        entry["platform"] = platform_changes(previous, provenance, system, base_url, package_arch)
    return entry


def valid_log(document) -> bool:
    if not isinstance(document, dict) or document.get("schema_version") != LOG_SCHEMA:
        return False
    entries = document.get("entries")
    return (isinstance(entries, list) and len(entries) <= LOG_LIMIT
            and all(isinstance(entry, dict) and isinstance(entry.get("generation"), int)
                    and SHA256.fullmatch(entry.get("system", ""))
                    and isinstance(entry.get("provenance"), dict)
                    and pd.valid_changes(entry.get("changes")) for entry in entries))


def update_log(existing: dict | None, release: dict | None, generation: int,
               provenance: dict, system: str, packages_fingerprint: str,
               base_url: str, package_arch: str, version: str) -> dict | None:
    """The new log, or None when this build changes nothing."""
    entries = list(existing["entries"]) if existing is not None else []
    previous = entries[0] if entries else baseline_from_release(release)
    if entries and same_inputs(entries[0], provenance, system, packages_fingerprint):
        return None
    entry = new_entry(previous, generation, provenance, system, packages_fingerprint,
                      base_url, package_arch, version)
    return {"schema_version": LOG_SCHEMA, "channel": "devel", "updated_at": entry["published_at"],
            "entries": ([entry] + entries)[:LOG_LIMIT]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--pair-generation", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument("--base-url", default="https://pkg.freesense.org/v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    generation = json.loads(args.pair_generation.read_text(encoding="utf-8"))["generation"]
    if not isinstance(generation, int) or generation <= 0:
        raise SystemExit("invalid pair generation")
    target = plan["targets"][args.architecture]
    system = target["system"]["system"]
    packages_fingerprint = target["packages"]["packages"]
    if not SHA256.fullmatch(system) or not SHA256.fullmatch(packages_fingerprint):
        raise SystemExit("plan has an invalid System or Optional fingerprint")
    provenance = build_provenance(plan, args.architecture)
    package_arch = target["system"].get("package_arch", "amd64")
    version = target["system"].get("release_version", "")

    log_url = f"{args.base_url}/releases/devel.notes.json"
    existing = pd.fetch_json(log_url, missing=None)
    if existing is not None and not valid_log(existing):
        raise SystemExit("the published release-notes log is invalid; refusing to overwrite it")
    release = pd.fetch_json(f"{args.base_url}/releases/devel.{args.architecture}.json", missing=None)
    document = update_log(existing, release, generation, provenance, system,
                          packages_fingerprint, args.base_url, package_arch, version)
    if document is None:
        print("release-notes log unchanged")
        return 0
    if not valid_log(document):
        raise SystemExit("the new release-notes log is invalid")
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"release-notes log updated: {len(document['entries'])} entries, "
          f"{len(document['entries'][0]['changes'])} change(s) in the newest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
