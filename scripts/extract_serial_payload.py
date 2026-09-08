#!/usr/bin/env python3
"""Extract one bounded base64 JSON payload from an authenticated worker serial log."""
import argparse
import base64
import json
from pathlib import Path
import re


def extract(data: str, name: str) -> bytes:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name):
        raise ValueError("invalid serial payload name")
    pattern = re.compile(rf"^{name}_BEGIN\r?\n([A-Za-z0-9+/=\r\n]+)^{name}_END\r?$", re.MULTILINE)
    matches = pattern.findall(data)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {name} serial payload")
    encoded = "".join(matches[0].split())
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError("serial payload is oversized")
    decoded = base64.b64decode(encoded, validate=True)
    value = json.loads(decoded)
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_bytes(extract(args.log.read_text(errors="strict"), args.name))
