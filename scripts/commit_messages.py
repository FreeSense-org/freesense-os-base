#!/usr/bin/env python3
"""Commit-message rules shared by the pull-request check and the release notes.

Every commit of the three product repositories (freesense, freesense-system-ports,
freesense-packages) becomes a line in the "What is new" list of a Development
build, so its first line must read well on its own:

    Area: what changed

        WebUI: the page width setting is per user and lives in Account
        Telegraf: generate a config the installed Telegraf accepts

Two optional trailers shape the note:

    Release-Note: Better wording for the note, one line   (replaces the subject)
    Release-Note: skip                                     (internal, not listed)
    Release-Type: security|fix|feature|ui|package|documentation|build|other

The publisher (publish_download.py) reads the trailers with parse_message(); the
check (check_commit_messages in the reusable workflow) reports problems() for
every commit of a pull request. Merge, revert and dependency-bot commits are
exempt. Commits already on a branch are never rewritten; only new ones are
checked.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

CHANGE_TYPES = ("security", "fix", "feature", "ui", "package", "documentation", "build", "other")
MAX_SUBJECT = 120
MIN_SUMMARY = 12
MAX_NOTE = 180
SUBJECT = re.compile(r"^(?P<area>[A-Z][A-Za-z0-9+./-]*(?: [A-Za-z0-9+./-]+){0,3}): (?P<summary>\S.*)$")
TRAILER = re.compile(r"^(?P<key>Release-Note|Release-Type):[ \t]*(?P<value>.*?)[ \t]*$", re.MULTILINE)
EXEMPT = ("Merge ", "Revert ", "Bump ", "Reapply ")
# Work that changes nothing a user can see; listed only with an explicit Release-Type.
INTERNAL_AREAS = frozenset({"build", "ci", "test", "tests", "chore"})
DRAFT = re.compile(r"^(wip|fixup!|squash!|tmp|todo)\b", re.IGNORECASE)


def parse_message(message: str) -> dict:
    """{subject, area, internal, note, type, skip} of one commit message.

    note is the Release-Note trailer (None when absent or `skip`), type the
    Release-Type trailer (None when absent or unknown)."""
    lines = (message or "").splitlines()
    subject = lines[0].strip() if lines else ""
    trailers = {match["key"]: match["value"] for match in TRAILER.finditer("\n".join(lines[1:]))}
    note = trailers.get("Release-Note")
    skip = note is not None and note.lower() == "skip"
    release_type = trailers.get("Release-Type", "").lower()
    match = SUBJECT.match(subject)
    return {
        "subject": subject,
        "area": match["area"] if match else None,
        "internal": bool(match) and match["area"].lower() in INTERNAL_AREAS,
        "note": None if skip or not note else note[:MAX_NOTE],
        "type": release_type if release_type in CHANGE_TYPES else None,
        "skip": skip,
    }


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
        found.append("start with the area, then a colon: 'Area: what changed', "
                     "for example 'WebUI: the page width setting is per user'")
    elif len(match["summary"]) < MIN_SUMMARY:
        found.append(f"say what changed after the area (at least {MIN_SUMMARY} characters)")
    if len(subject) > MAX_SUBJECT:
        found.append(f"keep the subject under {MAX_SUBJECT} characters (it is {len(subject)}); put detail in the body")
    if subject.endswith("."):
        found.append("no full stop at the end of the subject")
    body = "\n".join(lines[1:])
    for trailer in TRAILER.finditer(body):
        key, value = trailer["key"], trailer["value"]
        if key == "Release-Note" and value.lower() != "skip" and not (MIN_SUMMARY <= len(value) <= MAX_NOTE):
            found.append(f"Release-Note must be 'skip' or {MIN_SUMMARY}-{MAX_NOTE} characters")
        if key == "Release-Type" and value.lower() not in CHANGE_TYPES:
            found.append("Release-Type must be one of " + ", ".join(CHANGE_TYPES))
    return found


def commits_in(range_spec: str) -> list[tuple[str, str]]:
    """(sha, message) of every commit in a git range, oldest first."""
    raw = subprocess.run(
        ["git", "log", "--reverse", "--no-merges", "--format=%H%x00%B%x01", range_spec],
        check=True, capture_output=True, text=True).stdout
    commits = []
    for record in raw.split("\x01"):
        record = record.strip("\n")
        if record:
            sha, _, message = record.partition("\x00")
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
            subject = message.splitlines()[0] if message else ""
            print(f"{sha[:10]} {subject!r}\n    - {problem}")
    if failures:
        print(f"\n{failures} problem(s). Fix the commit messages (git commit --amend, or an "
              "interactive rebase for older commits) and push again. Rules: "
              "https://github.com/FreeSense-org/freesense-os-base/blob/main/docs/commit-messages.md",
              file=sys.stderr)
        return 1
    print("commit messages are fine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
