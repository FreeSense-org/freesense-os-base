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
from multiarch_pin import ARCHES, SHA256, validate  # noqa: E402

COMMIT = re.compile(r"[0-9a-f]{40}")


def missing(pin: dict) -> list[str]:
    """Architectures whose pin target has no mirror yet."""
    return [arch for arch in ARCHES if not pin["targets"][arch].get("mirror")]


def mirror_entry(arch: str, plan: dict, blob: dict) -> dict:
    if plan.get("schema_version") != "freesense.mirror-plan/v1" or plan.get("abi") != ARCHES[arch]:
        raise ValueError(f"{arch} mirror plan is not a {ARCHES[arch]} mirror plan")
    fingerprint = str(plan.get("fingerprint", ""))
    ports_commit = str(plan.get("ports_commit", ""))
    obj = str(blob.get("object") or blob.get("key") or "")
    if not SHA256.fullmatch(fingerprint) or not COMMIT.fullmatch(ports_commit):
        raise ValueError(f"{arch} mirror plan has no exact identity")
    if blob.get("schema_version") != "freesense.blob/v1" or obj != f"inputs/sha256/{blob.get('sha256')}":
        raise ValueError(f"{arch} mirror plan was not sealed as an immutable input")
    return {"fingerprint": fingerprint, "object": obj, "ports_commit": ports_commit}


def apply(pin: dict, mirrors: dict[str, dict], *, now: datetime | None = None) -> dict:
    updated = json.loads(json.dumps(pin))
    for arch, entry in mirrors.items():
        updated["targets"][arch]["mirror"] = entry
    validate(updated, now=now)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=Path("config/freebsd-16.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("missing", help="print the architectures that need a mirror, one per line")
    record = sub.add_parser("record", help="write mirrors from Mirror-run artifact directories")
    record.add_argument("--mirror", nargs=2, action="append", metavar=("ARCH", "DIR"), required=True,
                        help="architecture and the directory holding its mirror-plan(.blob).json")
    args = parser.parse_args()
    pin = json.loads(args.pin.read_text(encoding="utf-8"))
    if args.command == "missing":
        for arch in missing(pin):
            print(arch)
        return
    mirrors = {}
    for arch, directory in args.mirror:
        if arch not in ARCHES:
            raise SystemExit(f"unknown architecture: {arch}")
        root = Path(directory)
        mirrors[arch] = mirror_entry(arch, json.loads((root / "mirror-plan.json").read_text(encoding="utf-8")),
                                     json.loads((root / "mirror-plan.blob.json").read_text(encoding="utf-8")))
    args.pin.write_text(json.dumps(apply(pin, mirrors), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
