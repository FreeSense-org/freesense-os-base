#!/usr/bin/env python3
"""Sealed Stable release locks shared by the Stable planners."""

from __future__ import annotations

import json
from pathlib import Path
import re

SCHEMA = "freesense.release-lock/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELEASE = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")
NOTE_TYPES = {"security", "fix", "feature", "ui", "package", "documentation", "build", "other"}


WITHDRAWN_SCHEMA = "freesense.withdrawn-releases/v1"


def load(root: Path, policy: dict, release: str) -> dict:
    """Return the checked, sealed lock of one exact release of the Stable train."""
    stable_train = policy.get("release", {}).get("stable_train")
    match = RELEASE.fullmatch(release)
    if match is None or f"{match.group(1)}.{match.group(2)}" != stable_train:
        raise SystemExit(f"--release must be an exact {stable_train}.x version")
    lock = json.loads((root / "config/releases" / f"{release}.json").read_text(encoding="utf-8"))
    validate(lock, release)
    if lock.get("package_train") != stable_train:
        raise SystemExit("release lock package train does not match Stable policy")
    return lock


def validate(lock: dict, release: str) -> dict:
    """Check one lock document on its own, whatever train it belongs to."""
    match = RELEASE.fullmatch(release)
    if match is None:
        raise SystemExit(f"invalid release number {release!r}")
    if lock.get("schema_version") != SCHEMA or not lock.get("sealed"):
        raise SystemExit("release lock is not sealed")
    if lock.get("release") != release or lock.get("product_version") != f"{release}-RELEASE":
        raise SystemExit("release lock version does not match the requested release")
    if lock.get("package_train") != f"{match.group(1)}.{match.group(2)}":
        raise SystemExit("release lock package train does not match its version")
    for key in ("source_sha", "system_ports_sha", "packages_sha", "freebsd_source_sha", "freebsd_ports_sha"):
        if not SHA.fullmatch(lock.get(key, "")):
            raise SystemExit(f"invalid release input {key}")
    for key in ("worker_image_sha256", "worker_tools_sha256"):
        if not SHA256.fullmatch(lock.get(key, "")):
            raise SystemExit(f"invalid release input {key}")
    if not re.fullmatch(r"inputs/sha256/[0-9a-f]{64}", lock.get("jail_object", "")):
        raise SystemExit("invalid release input jail_object")
    if not isinstance(lock.get("freebsd_osversion"), int):
        raise SystemExit("release lock has no exact OSVERSION")
    # arm64 is optional. Its System is built on the same worker (the dedicated
    # host runs it under qemu-user), so the lock only adds its own jail seed.
    arm64 = lock.get("arm64")
    if arm64 is not None and (
            not isinstance(arm64, dict) or set(arm64) != {"jail_object"}
            or not re.fullmatch(r"inputs/sha256/[0-9a-f]{64}", arm64.get("jail_object", ""))):
        raise SystemExit("invalid release input arm64")
    # Optional hand-written release notes for changes no product repository
    # records (build or publication work in os-base). The publisher lists them
    # first in the release's change list, under the scope "Release".
    notes = lock.get("notes", [])
    if (not isinstance(notes, list) or len(notes) > 10
            or any(not isinstance(note, dict) or set(note) != {"type", "title"}
                   or note["type"] not in NOTE_TYPES
                   or not isinstance(note["title"], str)
                   or not 0 < len(note["title"]) <= 180
                   for note in notes)):
        raise SystemExit("invalid release notes: up to 10 {type, title} entries")
    # A seal sooner than stable_soak_hours after the previous one needs a
    # written reason (release policy R4).
    override = lock.get("soak_override")
    if override is not None and (not isinstance(override, str) or not 0 < len(override) <= 180):
        raise SystemExit("invalid soak_override: a reason of up to 180 characters")
    return lock


def withdrawn(root: Path) -> dict[str, dict]:
    """Releases that were cancelled or pulled, by version (release policy R2)."""
    path = root / "config/releases/withdrawn.json"
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    entries = document.get("withdrawn") if isinstance(document, dict) else None
    if document.get("schema_version") != WITHDRAWN_SCHEMA or not isinstance(entries, list):
        raise SystemExit("invalid withdrawn.json")
    found = {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"release", "date", "reason", "superseded_by"}
                or not RELEASE.fullmatch(str(entry["release"]))
                or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(entry["date"]))
                or not isinstance(entry["reason"], str) or not 0 < len(entry["reason"]) <= 300
                or not (entry["superseded_by"] is None or RELEASE.fullmatch(str(entry["superseded_by"])))
                or entry["release"] in found):
            raise SystemExit(f"invalid withdrawn.json entry: {entry!r}")
        found[entry["release"]] = entry
    return found


def targets(lock: dict) -> list[str]:
    """The architectures a sealed release ships, amd64 first."""
    return ["amd64", "arm64"] if "arm64" in lock else ["amd64"]


def sources(lock: dict) -> dict:
    """The coordinator-resolved source snapshot a sealed release builds from."""
    return {
        "source": lock["source_sha"],
        "system_ports": lock["system_ports_sha"],
        "packages": lock["packages_sha"],
    }


def policy_for(policy: dict, lock: dict) -> dict:
    """Policy as the sealed release sees it: its own train and version."""
    return {
        **policy,
        "package_train": lock["package_train"],
        "release": {**policy["release"], "development_version": lock["release"]},
    }


def check_pin(lock: dict, pin: dict, *, osversion: int, jail_object: str,
              worker_image_sha256: str, worker_tools_sha256: str, target: str = "amd64") -> None:
    """A sealed release builds on the active FreeBSD pin, so the two must agree.

    The worker farm validates every plan against config/freebsd-16.json; a
    release sealed against a different pin could never be built, and one built
    anyway would not be the release the lock describes.
    """
    if target not in targets(lock):
        raise SystemExit(f"release lock does not ship {target}")
    sealed_jail = lock["jail_object"] if target == "amd64" else lock["arm64"]["jail_object"]
    expected = {
        "FreeBSD source": (lock["freebsd_source_sha"], pin["freebsd_source"]["commit"]),
        "FreeBSD ports": (lock["freebsd_ports_sha"], pin["freebsd_ports"]["commit"]),
        "OSVERSION": (lock["freebsd_osversion"], osversion),
        "jail seed": (sealed_jail, jail_object),
        "worker image": (lock["worker_image_sha256"], worker_image_sha256),
        "worker tools": (lock["worker_tools_sha256"], worker_tools_sha256),
    }
    differing = [name for name, (sealed, active) in expected.items() if sealed != active]
    if differing:
        raise SystemExit(
            "release lock differs from the active FreeBSD pin: " + ", ".join(differing)
            + " (re-seal the release against the current pin)"
        )
