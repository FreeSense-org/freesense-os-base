#!/usr/bin/env python3
"""The rules every FreeSense 2.0 workflow follows: build only, GitHub-hosted runners, nothing published.

usage: workflow_lint.py [--dispatcher FILE ...] PATH...

The one exception to "no secrets, no environment" is the `sign` job of v2-build.yml: environment signing-2.0 and the
secret of the 2.0 test repository key, on the 2.0 branch only.

PATH is a workflow file or a directory that is searched for .github/workflows/*.yml. --dispatcher names a file that
may hold `actions: write` (the stub on main that only starts the 2.0 build). Text rules, no YAML library needed.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
from pathlib import Path
import re
import sys

FORBIDDEN = (
    (r"\bsecrets\s*[.:\[]", "no secrets"),
    (r"\bid-token\b", "no OIDC token"),
    (r"^\s*environment\s*:", "no deployment environment"),
    (r"self-hosted|build-runner", "GitHub-hosted runners only"),
    (r"\bpull_request_target\b", "pull_request_target runs fork code with write access"),
    (r"\bworkflow_run\b", "workflow_run chains privileged runs"),
    (r"\b(rclone|wrangler|fsbuild)\b", "publication tool"),
    (r"\baws\s+s3|\bR2_[A-Z_]+|r2\.cloudflarestorage|cloudflare/", "publication storage"),
    (r"\bpages\s+deploy\b|\bnpm\s+publish\b|\bdocker\s+push\b", "publication command"),
    (r"\bgh\s+release\b|\bgh\s+pr\s+(create|merge)\b|\bgit\s+(push|tag)\b", "no release, tag or push from a workflow"),
)
# Writes a 2.0 workflow may ask for. security-events uploads scan results to this repository.
WRITABLE = {"security-events"}
# The one exception: the job that signs the 2.0 test package repository (scripts/ci/signing.md).
SIGNING_WORKFLOW = "v2-build.yml"
SIGNING_JOB = "sign"
SIGNING_ENVIRONMENT = re.compile(r"^    environment: signing-2\.0\s*$")
SIGNING_SECRET = re.compile(r"^          [A-Z0-9_]+: \$\{\{ secrets\.FREESENSE_2_PKG_SIGNING_KEY_B64 \}\}\s*$")
SIGNING_CONDITION = "    if: github.ref == 'refs/heads/2.0' && github.event_name == 'workflow_dispatch'"
SIGNING_ACTIONS = ("actions/checkout@", "actions/download-artifact@", "actions/upload-artifact@")
IMAGES = re.compile(r"\.(iso|img|raw|qcow2|pkg)\b", re.IGNORECASE)
OWN = re.compile(r"^(\./|FreeSense-org/freesense-os-base/\.github/workflows/[\w.-]+@2\.0$)")
UNTRUSTED = re.compile(r"\$\{\{[^}]*\b(inputs\.|github\.event\.|github\.head_ref|github\.ref_name)")


def code_lines(text: str) -> list[str]:
    """The lines without comments."""
    return [re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines()]


def blocks(lines: list[str], key: str):
    """(line number, indent, header, body) of every `key:` mapping or block scalar."""
    pattern = re.compile(rf"^(\s*(?:- )?){key}\s*:(.*)$")
    for number, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        indent = len(match[1])
        body = []
        for following in lines[number + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        yield number, indent, match[2].strip(), body


def job_range(lines: list[str], name: str) -> range:
    """The line indexes of one job of `jobs:`."""
    for number, line in enumerate(lines):
        if line.rstrip() == f"  {name}:" and "jobs:" in (other.rstrip() for other in lines[:number]):
            end = next((after for after in range(number + 1, len(lines)) if re.match(r"^ {0,2}\S", lines[after])), len(lines))
            return range(number, end)
    return range(0)


def signing_exception(lines: list[str]) -> tuple[set[int], list[str]]:
    """The lines of the signing job that may name the environment and the secret, and what is wrong with that job."""
    job = job_range(lines, SIGNING_JOB)
    environment = [number for number in job if SIGNING_ENVIRONMENT.match(lines[number])]
    secret = [number for number in job if SIGNING_SECRET.match(lines[number])]
    if not environment and not secret:
        return set(), []
    problems = []
    if len(environment) != 1 or len(secret) != 1:
        problems.append("the signing job names its environment once and its secret once")
    if SIGNING_CONDITION not in [lines[number].rstrip() for number in job]:
        problems.append("the signing job runs only for a dispatch of the 2.0 branch")
    if any(re.match(r"^\s+[a-z-]+\s*:\s*write", lines[number]) for number in job):
        problems.append("the signing job has no write permission")
    if "      contents: read" not in [lines[number].rstrip() for number in job]:
        problems.append("the signing job sets its own permissions")
    for number in job:
        action = re.match(r"^\s*(?:- )?uses\s*:\s*(\S+)", lines[number])
        if action and not action[1].startswith(SIGNING_ACTIONS):
            problems.append(f"the signing job uses only checkout and the artifact actions, not {action[1]}")
    if any(re.match(r"^\s*pull_request", line) for line in lines):
        problems.append("a workflow with the signing job has no pull request trigger")
    return set(environment[:1] + secret[:1]), [f"{job.start + 1}: {problem}" for problem in problems]


def lint(text: str, dispatcher: bool = False, signing: bool = False) -> list[str]:
    lines = code_lines(text)
    allowed_lines, found = signing_exception(lines) if signing else (set(), [])
    for number, line in enumerate(lines, 1):
        for pattern, why in FORBIDDEN if number - 1 not in allowed_lines else ():
            if re.search(pattern, line, re.IGNORECASE):
                found.append(f"{number}: {why}")
        action = re.match(r"^\s*(?:- )?uses\s*:\s*[\"']?([^\"'\s]+)", line)
        if action and not OWN.match(action[1]) and not re.search(r"@[0-9a-f]{40}$", action[1]):
            found.append(f"{number}: action is not pinned to a commit: {action[1]}")
        runner = re.match(r"^\s*runs-on\s*:\s*(.*)$", line)
        if runner and not re.fullmatch(r"ubuntu-[0-9a-z.]+", runner[1].strip()):
            found.append(f"{number}: runs-on must name a GitHub-hosted ubuntu label")
    if not any(re.match(r"^permissions\s*:", line) for line in lines):
        found.append("1: set the workflow's permissions")
    allowed = WRITABLE | ({"actions"} if dispatcher else set())
    for number, _, header, body in blocks(lines, "permissions"):
        if "write-all" in header or "read-all" in header:
            found.append(f"{number + 1}: name each permission")
        for entry in [header, *body]:
            for scope in re.findall(r"([a-z-]+)\s*:\s*write\b", entry):
                if scope not in allowed:
                    found.append(f"{number + 1}: write permission {scope} is not allowed")
    for number, _, header, body in blocks(lines, "run"):
        for entry in [header, *body]:
            if UNTRUSTED.search(entry):
                found.append(f"{number + 1}: pass inputs and event values to scripts through env, not text substitution")
                break
    found.extend(image_uploads(lines))
    return found


def image_uploads(lines: list[str]) -> list[str]:
    """An installable image is an artifact only when a step asks for it with `upload_iso`: others can download it."""
    found = []
    starts = [number for number, line in enumerate(lines) if re.match(r"^\s*- ", line)]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        step = "\n".join(lines[start:end])
        if "actions/upload-artifact@" not in step and "actions/cache" not in step:
            continue
        paths = "\n".join(line for _, _, header, body in blocks(lines[start:end], "path") for line in [header, *body])
        if IMAGES.search(paths) and not re.search(r"^\s*(?:- )?if\s*:.*upload_iso", step, re.IGNORECASE | re.MULTILINE):
            found.append(f"{start + 1}: an image in an artifact or cache needs the upload_iso condition")
    return found


def workflows(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for pattern in ("*.yml", "*.yaml") for p in path.rglob(pattern)
                  if p.parent.name == "workflows" and p.parent.parent.name == ".github")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dispatcher", action="append", default=[], type=Path)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    files = [file for path in args.paths for file in workflows(path)]
    if not files:
        print("no workflow found", file=sys.stderr)
        return 1
    dispatchers = {path.resolve() for path in args.dispatcher}
    failures = 0
    for file in files:
        for problem in lint(file.read_text(encoding="utf-8"), file.resolve() in dispatchers, file.name == SIGNING_WORKFLOW):
            print(f"{file.as_posix()}:{problem}")
            failures += 1
    print(f"{len(files)} workflows, {failures} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
