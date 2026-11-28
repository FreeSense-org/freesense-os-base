#!/usr/bin/env python3
"""Commit-message rules of FreeSense, for the checks and the change list of a build.

The first line of every commit reads on its own:

    Area: what changed

Trailers: `Release-Note: <one line>` replaces the subject in the change list, `Release-Note: skip` leaves the
commit out. Merge, revert and dependency-bot commits are exempt. Carried over from freesense-os-base `main`.

usage: commit_messages.py --range origin/main..HEAD
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
import re
import subprocess

MAX_SUBJECT = 120
MIN_SUMMARY = 12
MAX_NOTE = 180
SUBJECT = re.compile(r"^(?P<area>[A-Z][A-Za-z0-9+./-]*(?: [A-Za-z0-9+./-]+){0,3}): (?P<summary>\S.*)$")
TRAILER = re.compile(r"^(?P<key>Release-Note):[ \t]*(?P<value>.*?)[ \t]*$", re.MULTILINE)
EXEMPT = ("Merge ", "Revert ", "Bump ", "Reapply ")
INTERNAL_AREAS = frozenset({"build", "ci", "test", "tests", "chore"})
DRAFT = re.compile(r"^(wip|fixup!|squash!|tmp|todo)\b", re.IGNORECASE)


def note_line(message: str) -> str | None:
    """The line this commit adds to a change list, or None."""
    lines = (message or "").splitlines()
    subject = lines[0].strip() if lines else ""
    trailers = {match["key"]: match["value"] for match in TRAILER.finditer("\n".join(lines[1:]))}
    note = trailers.get("Release-Note")
    if not subject or subject.startswith(EXEMPT) or (note is not None and note.lower() == "skip"):
        return None
    match = SUBJECT.match(subject)
    if note:
        return note[:MAX_NOTE]
    return None if match and match["area"].lower() in INTERNAL_AREAS else subject


def problems(message: str) -> list[str]:
    """What is wrong with a commit message; empty when it is fine."""
    lines = (message or "").splitlines()
    subject = lines[0].strip() if lines else ""
    if not subject:
        return ["the commit has no subject line"]
    if subject.startswith(EXEMPT):
        return []
    found = []
    if DRAFT.match(subject):
        found.append("a draft commit (wip, fixup!, squash!, tmp) must be cleaned up before it merges")
    match = SUBJECT.match(subject)
    if match is None:
        found.append("start with the area, then a colon: 'Area: what changed'")
    elif len(match["summary"]) < MIN_SUMMARY:
        found.append(f"say what changed after the area (at least {MIN_SUMMARY} characters)")
    if len(subject) > MAX_SUBJECT:
        found.append(f"keep the subject under {MAX_SUBJECT} characters (it is {len(subject)})")
    if subject.endswith("."):
        found.append("no full stop at the end of the subject")
    for trailer in TRAILER.finditer("\n".join(lines[1:])):
        value = trailer["value"]
        if value.lower() != "skip" and not MIN_SUMMARY <= len(value) <= MAX_NOTE:
            found.append(f"Release-Note must be 'skip' or {MIN_SUMMARY}-{MAX_NOTE} characters")
    return found


def commits_in(range_spec: str, directory: str = ".", limit: int | None = None) -> list[tuple[str, str]]:
    """(sha, message) of the commits in a git range, newest first."""
    command = ["git", "-C", directory, "log", "--no-merges", "--format=%H%x00%B%x01"]
    if limit:
        command.append(f"--max-count={limit}")
    raw = subprocess.run([*command, range_spec, "--"], check=True, capture_output=True).stdout.decode("utf-8", "replace")
    commits = []
    for record in raw.split("\x01"):
        sha, _, message = record.strip("\n").partition("\x00")
        if sha:
            commits.append((sha, message))
    return commits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--range", required=True, help="git range, for example origin/main..HEAD")
    args = parser.parse_args()
    failures = 0
    for sha, message in commits_in(args.range):
        for problem in problems(message):
            failures += 1
            print(f"{sha[:10]} {message.splitlines()[0] if message else ''!r}\n    - {problem}")
    print(f"{failures} problem(s)" if failures else "commit messages are fine")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
