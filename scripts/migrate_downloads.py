#!/usr/bin/env python3
"""Split the legacy combined download index into canonical channel documents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from publish_download import DOWNLOAD_SCHEMA, fetch_json, validate_download


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://pkg.freesense.org/v1")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    legacy = fetch_json(f"{args.base_url}/releases.json")
    if (not isinstance(legacy, dict)
            or legacy.get("schema_version") != "freesense.downloads/v1"
            or not isinstance(legacy.get("channels"), dict)):
        raise SystemExit("legacy download index is invalid")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    available = 0
    created = 0
    for channel in ("stable", "devel"):
        release = legacy["channels"].get(channel)
        if release is None:
            continue
        available += 1
        if not isinstance(release, dict):
            raise SystemExit(f"legacy {channel} download entry is invalid")
        release = {**release, "schema_version": DOWNLOAD_SCHEMA}
        validate_download(release, channel, args.base_url)
        existing = fetch_json(f"{args.base_url}/releases/{channel}.json", missing=None)
        if existing is not None:
            validate_download(existing, channel, args.base_url)
            if existing != release:
                raise SystemExit(
                    f"refusing to overwrite the existing {channel} download document"
                )
            continue
        Path(args.output_dir, f"{channel}.json").write_text(
            json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        created += 1
    if available == 0:
        raise SystemExit("legacy download index contains no releases")
    print(f"prepared {created} missing channel document(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
