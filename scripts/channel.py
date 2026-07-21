#!/usr/bin/env python3
"""Verify and read the compact signed FreeSense channel document."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://pkg.freesense.org/v1/repos.manifest.json")
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--channel", choices=("devel", "stable"), required=True)
    parser.add_argument("--component", choices=("system", "packages"), required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    with urllib.request.urlopen(args.url, timeout=20) as response:
        envelope = json.load(response)
    if envelope.get("schema_version") != "freesense.repositories/v3":
        raise SystemExit("unsupported channel envelope")
    try:
        payload_bytes = base64.b64decode(envelope["payload"], validate=True)
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (KeyError, ValueError) as error:
        raise SystemExit("invalid channel envelope encoding") from error

    with tempfile.TemporaryDirectory() as directory:
        payload_file = Path(directory, "payload.json")
        signature_file = Path(directory, "signature.bin")
        payload_file.write_bytes(payload_bytes)
        signature_file.write_bytes(signature)
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(args.public_key),
             "-signature", str(signature_file), str(payload_file)],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    payload = json.loads(payload_bytes)
    if payload.get("schema_version") != "freesense.channels/v1":
        raise SystemExit("unsupported signed channel payload")
    try:
        channel = payload["channels"][args.channel]
        component = channel[args.component]
    except (KeyError, TypeError) as error:
        raise SystemExit(f"{args.channel} has no {args.component} component") from error
    values = {
        "fingerprint": component["fingerprint"],
        "url": component["url"],
        "generation": component["generation"],
        "published_at": component["published_at"],
        "verified": str(bool(component.get("verified"))).lower(),
        "package_train": channel["package_train"],
        "abi": channel["abi"],
        "altabi": channel["altabi"],
    }
    print(json.dumps(values, indent=2, sort_keys=True))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
