#!/usr/bin/env python3
"""Render one checked FreeBSD worker without writing secrets to logs."""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path


FIELDS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "R2_ENDPOINT", "R2_BUCKET", "FREESENSE_REPO_SIGNING_KEY",
    "STAGE", "FINGERPRINT", "PLATFORM_ID", "SYSTEM_ID", "SOURCE_SHA",
    "SYSTEM_SHA", "PACKAGES_SHA", "OS_BASE_SHA", "FREEBSD_SHA", "PORTS_SHA",
    "IMAGE_SHA256", "WORKER_TOOLS_SHA256", "JAIL_OBJECT", "PACKAGE_TRAIN", "GENERATION", "PUBLIC_BASE_URL",
    "CHANNEL", "CHANNEL_PAYLOAD_SHA256", "CHANNEL_PAYLOAD_B64", "CHANNEL_SIGNATURE_B64",
)
ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--common", type=Path, required=True)
    parser.add_argument("--stages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    missing = [name for name in FIELDS if name not in os.environ]
    if missing:
        raise SystemExit("missing worker inputs: " + ", ".join(missing))
    stage = os.environ["STAGE"]
    if stage not in {"system", "packages", "iso"}:
        raise SystemExit(f"invalid stage: {stage}")
    parts = [
        ROOT / "scripts/runner/install-worker-tools.sh",
        args.common,
        args.stages / f"{stage}.sh",
    ]
    lines = ["#!/bin/sh", "set -eu"]
    for name in FIELDS:
        encoded = base64.b64encode(os.environ[name].encode()).decode("ascii")
        lines.append(f"{name}_B64='{encoded}'")
    lines.extend(path.read_text(encoding="utf-8") for path in parts)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    args.output.chmod(0o700)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
