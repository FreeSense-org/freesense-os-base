"""Look through the staged 2.0 commits for what must not become public. Prints rule and place, never the value.

usage: python tools/publication_scan.py [--staging DIR] [--patterns FILE] [--verbose]

Scans every file that the top commit of each staging checkout adds or changes, and that commit's message and identity.
--patterns names a file outside the repositories with one `label<TAB>regular expression` per line: the names,
addresses and host names of the people and machines behind the project, which must not be listed in here.
Exit status 1 when a rule of the BLOCK kind matches; REVIEW findings are for a person to look at.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
BASE64_LINE = re.compile(r"^[+ ]?[A-Za-z0-9+/]{40,}={0,2}\\?n?[\"',]*\s*$")
KEY_HEADER = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----")
BLOCK = {
    "access token": re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}|\bAKIA[0-9A-Z]{16}\b"
                               r"|\bxox[abprs]-[A-Za-z0-9-]{20,}|\bsk-[A-Za-z0-9_-]{32,}"),
    "workstation path": re.compile(r"\b[A-Za-z]:[\\/]{1,2}(?:Users|Projects)[\\/]|/c/(?:Users|Projects)/|\bZ:/\.git"),
}
REVIEW = {
    "password hash": re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}|\$6\$[./A-Za-z0-9]{8,16}\$[./A-Za-z0-9]{60,}"),
    "SSH public key": re.compile(r"\b(?:ssh-(?:ed25519|rsa)|ecdsa-sha2-nistp\d+) AAAA[A-Za-z0-9+/]{40,}"),
    "link to local evidence": re.compile(r"\]\((?:\.\./)+(?:test-lab|local-output|private|backups)/"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
# Reserved for documentation and tests, the project's own, or protocol names that look like an address.
HARMLESS_DOMAIN = re.compile(r"(?:^|\.)(?:example(?:\.(?:com|net|org|invalid))?|test|invalid|localdomain|local|lan|home\.arpa"
                             r"|freesense\.org|openssh\.com|libssh\.org|domain|php|js|py|sh|conf|service|so|pid|sock)$", re.IGNORECASE)
FILE_NAMES = re.compile(r"(?:^|/)(?:id_(?:rsa|ed25519|ecdsa)(?:\.pub)?|authorized_keys|known_hosts|\.env(?:\..*)?|NUL)$"
                        r"|\.(?:pem|key|pfx|p12|ppk|lnk|raw|iso|img|qcow2|vmdk|pkg|log|pyc|swp|orig|rej|sqlite3?|db)$", re.IGNORECASE)
IDENTITY = "FreeSense <dev@freesense.org>"
LARGE = 1_000_000


def git(directory: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(directory), "-c", "core.quotepath=false", *arguments], check=True,
                          capture_output=True).stdout


def private_patterns(path: Path | None) -> dict[str, re.Pattern]:
    if path is None:
        return {}
    patterns = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            label, expression = line.split("\t", 1)
            patterns[label] = re.compile(expression, re.IGNORECASE)
    return patterns


def scan_text(text: str, private: dict) -> list[tuple[str, str, int]]:
    """(kind, rule, line) findings of one text."""
    found = []
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if KEY_HEADER.search(line) and any(BASE64_LINE.match(other) for other in lines[number:number + 4]):
            found.append(("BLOCK", "private key with a body", number))
        found += [("BLOCK", rule, number) for rule, pattern in BLOCK.items() if pattern.search(line)]
        # A private label that starts with "review " does not block.
        found += [("REVIEW" if rule.startswith("review ") else "BLOCK", rule, number)
                  for rule, pattern in private.items() if pattern.search(line)]
        found += [("REVIEW", rule, number) for rule, pattern in REVIEW.items() if pattern.search(line)]
        if any(not HARMLESS_DOMAIN.search(match[1]) for match in EMAIL.finditer(line)):
            found.append(("REVIEW", "e-mail address of another domain", number))
    return found


def scan_repository(directory: Path, private: dict) -> tuple[list, dict]:
    findings, facts = [], {"files": 0, "binary": [], "large": [], "domains": Counter()}
    message = git(directory, "log", "-1", "--format=%B").decode("utf-8")
    identities = git(directory, "log", "-1", "--format=%an <%ae>%n%cn <%ce>").decode("utf-8").split("\n")[:2]
    if identities != [IDENTITY, IDENTITY]:
        findings.append(("BLOCK", "commit identity", "(commit)", 0))
    findings += [(kind, rule, "(commit message)", line) for kind, rule, line in scan_text(message, private)]
    changed = git(directory, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=AMT", "HEAD~1", "HEAD")
    for name in filter(None, changed.decode("utf-8").split("\0")):
        data = git(directory, "cat-file", "blob", f"HEAD:{name}")
        facts["files"] += 1
        if FILE_NAMES.search(name):
            findings.append(("REVIEW", "file name", name, 0))
        findings += [(kind, rule, name, 0) for kind, rule, _ in scan_text(name, private) if kind == "BLOCK"]
        if len(data) > LARGE:
            facts["large"].append((name, len(data)))
        if b"\0" in data:
            facts["binary"].append((name, len(data)))
            # Metadata of images and fonts can carry names and paths.
            printable = b"\n".join(re.findall(rb"[ -~]{5,}", data)).decode("ascii")
            findings += [("BLOCK", rule, name, 0) for rule, pattern in {**BLOCK, **private}.items() if pattern.search(printable)]
            continue
        text = data.decode("utf-8", "replace")
        findings += [(kind, rule, name, line) for kind, rule, line in scan_text(text, private)]
        facts["domains"].update(m[1].lower() for m in EMAIL.finditer(text) if not HARMLESS_DOMAIN.search(m[1]))
    return findings, facts


def unlicensed_third_party(directory: Path) -> list[str]:
    """Directories of third-party code without a licence or notice file next to them or one level up."""
    files = git(directory, "ls-files", "-z").decode("utf-8").split("\0")
    licensed = {str(Path(f).parent.as_posix()) for f in files if re.search(r"(?:LICENSE|LICENCE|COPYING|NOTICE|OFL)", Path(f).name, re.I)}
    folders = set()
    for name in files:
        match = re.match(r"(.*?(?:third-party|vendor)(?:/[^/]+)?)/", name)
        if match:
            folders.add(match[1])
    return sorted(folder for folder in folders
                  if not any(known == folder or known.startswith(folder + "/") for known in licensed)
                  and str(Path(folder).parent.as_posix()) not in licensed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--staging", type=Path, default=ROOT.parents[1] / "local-output" / "2.0-staging")
    parser.add_argument("--patterns", type=Path)
    parser.add_argument("--verbose", action="store_true", help="list every finding instead of a summary")
    args = parser.parse_args()
    private = private_patterns(args.patterns)
    blocked = 0
    for directory in sorted(p for p in args.staging.iterdir() if (p / ".git").is_dir()):
        findings, facts = scan_repository(directory, private)
        print(f"== {directory.name}: {facts['files']} added or changed files, {len(facts['binary'])} binary")
        grouped = defaultdict(list)
        for kind, rule, name, line in findings:
            grouped[(kind, rule)].append(f"{name}:{line}" if line else name)
        for (kind, rule), places in sorted(grouped.items()):
            blocked += len(places) if kind == "BLOCK" else 0
            shown = places if args.verbose or kind == "BLOCK" else sorted({p.rsplit(":", 1)[0] for p in places})[:8]
            print(f"  {kind} {rule}: {len(places)}")
            for place in shown:
                print(f"    {place}")
        for name, size in facts["binary"] if args.verbose else []:
            print(f"  binary {size:>8} {name}")
        for name, size in facts["large"]:
            print(f"  large {size:>9} {name}")
        if facts["domains"] and args.verbose:
            print("  e-mail domains: " + ", ".join(f"{d} ({n})" for d, n in facts["domains"].most_common()))
        for folder in unlicensed_third_party(directory):
            print(f"  REVIEW third-party folder without a licence file: {folder}")
    print(f"{blocked} blocking finding(s); private patterns: {len(private)}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
