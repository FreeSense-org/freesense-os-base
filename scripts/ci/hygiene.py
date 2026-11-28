#!/usr/bin/env python3
"""Reject committed key material and dependencies on vendor infrastructure. Prints the place, never the value.

usage: hygiene.py DIR...
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import re
import sys

RULES = (
    ("private key", re.compile(r"^\s*-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s*$"), False),
    ("storage credential", re.compile(r"R2_SECRET_ACCESS_KEY\s*[=:]\s*[^\s$<{]"), False),
    ("access token", re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}|\bAKIA[0-9A-Z]{16}\b"), False),
    # Documentation and the NOTICE files may name the vendor; code and build files may not fetch from it.
    ("vendor infrastructure", re.compile(r"(?:https?://[^\s]*netgate\.com|(?:files|packages(?:-beta)?|gitlab|codesigner)\.netgate\.com"
                                         r"|\.nyi\.netgate\.com)"), True),
)
SKIP = {".git", "__pycache__", "node_modules"}


def scan(root: Path):
    for parent, dirs, names in root.walk():
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(names):
            path = parent / name
            data = path.read_bytes()
            if b"\0" in data:
                continue
            prose = path.suffix == ".md" or name.startswith("NOTICE")
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                for rule, pattern, code_only in RULES:
                    if not (code_only and prose) and pattern.search(line):
                        yield path, number, rule


def main(roots: list[str]) -> int:
    found = 0
    for root in roots:
        for path, number, rule in scan(Path(root)):
            print(f"{path.as_posix()}:{number}: {rule}")
            found += 1
    print(f"hygiene: {found} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["."]))
