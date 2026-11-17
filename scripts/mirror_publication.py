#!/usr/bin/env python3
# Copyright (c) 2026 The FreeSense Project
# SPDX-License-Identifier: Apache-2.0
"""Give one signed mirror publication attempt its own immutable repository identity."""
import argparse
import json
from pathlib import Path
import re

from multiarch_pin import digest


def publication_plan(observed: dict, publication_id: str) -> dict:
    if (observed.get("schema_version") != "freesense.mirror-plan/v1"
            or observed.get("fingerprint") != digest({k: v for k, v in observed.items() if k != "fingerprint"})):
        raise ValueError("the observed mirror plan has an invalid identity")
    if not re.fullmatch(r"[1-9][0-9]*\.[1-9][0-9]*", publication_id):
        raise ValueError("publication identity must be a workflow run and attempt")
    if "publication_id" in observed or "observation_fingerprint" in observed:
        raise ValueError("the input must be an original observation plan")
    # pkg's signed catalogue metadata changes between publication attempts,
    # even when the selected upstream package bytes are identical. Keep the
    # observed content identity for provenance, but never overwrite its older
    # publication's immutable prefix with newly generated catalogue metadata.
    result = {**observed, "observation_fingerprint": observed["fingerprint"],
              "publication_id": publication_id}
    result["fingerprint"] = digest({k: v for k, v in result.items() if k != "fingerprint"})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observed", required=True, type=Path)
    parser.add_argument("--publication-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = publication_plan(json.loads(args.observed.read_text(encoding="utf-8")), args.publication_id)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
