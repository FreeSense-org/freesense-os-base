#!/usr/bin/env python3
"""Record freshly cut per-architecture mirrors in the FreeBSD pin.

pin.yml writes a pin without mirrors, so a rollover would silently drop the
layer the delta build seeds from. pin-mirrors.yml observes and cuts a mirror
for every architecture that lacks one; this script checks what is missing and
writes each cut mirror's plan object, fingerprint and ports commit into the pin.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from multiarch_pin import ARCHES, SHA256, validate, with_source_archives  # noqa: E402

COMMIT = re.compile(r"[0-9a-f]{40}")


def overlay_origins(directories: list[Path]) -> list[str]:
    """Port origins (category/port) the FreeSense overlays change.

    Overlaid ports are built from source, so a mirror plan must have been cut
    knowing them; a plan cut before an overlay existed still lists the port as
    a prebuilt mirror package and the repositories fail to compose.
    """
    origins = set()
    for directory in directories:
        for makefile in directory.glob("*/*/Makefile"):
            category, port = makefile.parent.parent.name, makefile.parent.name
            if not category.startswith(".") and category not in {"Mk", "tools", "tests", "docs"}:
                origins.add(f"{category}/{port}")
    return sorted(origins)


def missing(pin: dict, overlays: list[str] | None = None) -> list[str]:
    """Architectures whose pin target needs a (new) mirror.

    An architecture needs one when it has none, or when its mirror was cut for
    a different set of overlaid ports than the overlays have now. A mirror cut
    before the set was recorded is left alone until something re-cuts it.
    """
    stale = []
    for arch in ARCHES:
        mirror = pin["targets"][arch].get("mirror")
        if not mirror:
            stale.append(arch)
        elif overlays is not None and "overlay_origins" in mirror and mirror["overlay_origins"] != overlays:
            stale.append(arch)
    return stale


def mirror_entry(arch: str, plan: dict, blob: dict, overlays: list[str] | None = None) -> dict:
    if plan.get("schema_version") != "freesense.mirror-plan/v1" or plan.get("abi") != ARCHES[arch]:
        raise ValueError(f"{arch} mirror plan is not a {ARCHES[arch]} mirror plan")
    fingerprint = str(plan.get("fingerprint", ""))
    ports_commit = str(plan.get("ports_commit", ""))
    obj = str(blob.get("object") or blob.get("key") or "")
    if not SHA256.fullmatch(fingerprint) or not COMMIT.fullmatch(ports_commit):
        raise ValueError(f"{arch} mirror plan has no exact identity")
    if blob.get("schema_version") != "freesense.blob/v1" or obj != f"inputs/sha256/{blob.get('sha256')}":
        raise ValueError(f"{arch} mirror plan was not sealed as an immutable input")
    entry = {"fingerprint": fingerprint, "object": obj, "ports_commit": ports_commit}
    if overlays is not None:
        entry["overlay_origins"] = overlays
    return entry


def ports_archive(entry: dict, archive: dict) -> dict[str, dict]:
    """The stored ports archive a Mirror run made at its mirror's commit, if any."""
    commit = archive.get("commit")
    if commit != entry["ports_commit"] or archive.get("repository") != "freebsd/freebsd-ports":
        raise ValueError("ports archive was stored for a different commit than the mirror")
    return {commit: {key: value for key, value in archive.items() if key != "commit"}}


def apply(pin: dict, mirrors: dict[str, dict], *, now: datetime | None = None,
          archives: dict[str, dict] | None = None) -> dict:
    updated = json.loads(json.dumps(pin))
    for arch, entry in mirrors.items():
        updated["targets"][arch]["mirror"] = entry
    # Re-cutting a mirror moves its ports commit; the old commit's archive goes.
    updated = with_source_archives(updated, archives or {})
    validate(updated, now=now)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=Path("config/freebsd-16.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    parser.add_argument("--overlay", type=Path, action="append", default=None,
                        help="a FreeSense ports overlay checkout (freesense-system-ports, freesense-packages)")
    sub.add_parser("missing", help="print the architectures that need a mirror, one per line")
    record = sub.add_parser("record", help="write mirrors from Mirror-run artifact directories")
    record.add_argument("--mirror", nargs=2, action="append", metavar=("ARCH", "DIR"), required=True,
                        help="architecture and the directory holding its mirror-plan(.blob).json")
    args = parser.parse_args()
    pin = json.loads(args.pin.read_text(encoding="utf-8"))
    overlays = overlay_origins(args.overlay) if args.overlay else None
    if args.command == "missing":
        for arch in missing(pin, overlays):
            print(arch)
        return
    mirrors, archives = {}, {}
    for arch, directory in args.mirror:
        if arch not in ARCHES:
            raise SystemExit(f"unknown architecture: {arch}")
        root = Path(directory)
        mirrors[arch] = mirror_entry(arch, json.loads((root / "mirror-plan.json").read_text(encoding="utf-8")),
                                     json.loads((root / "mirror-plan.blob.json").read_text(encoding="utf-8")),
                                     overlays)
        archive = root / "ports-archive.json"
        if archive.is_file():
            archives.update(ports_archive(mirrors[arch], json.loads(archive.read_text(encoding="utf-8"))))
        else:
            print(f"{arch} mirror has no stored ports archive; its builds fetch ports from GitHub")
    args.pin.write_text(json.dumps(apply(pin, mirrors, archives=archives), indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
