#!/usr/bin/env python3
"""Render the make.conf region that renames FreeSense's layer of the repository.

A package FreeSense builds must never be mistaken for the upstream package of the
same name, in either direction: pkg's solver would otherwise be free to satisfy a
dependency from the mirror with a build that has different options or patches,
and an upgrade could move a user off a FreeSense build onto the stock one.

Renaming is safe because the delta is closed under reverse dependencies: every
package that links or runs against a renamed one is itself in the delta and is
rebuilt against the new name, so the graph stays consistent inside our layer.

The region is generated at build time from the sealed mirror plan rather than
committed, so the names can never disagree with the membership decision that
produced them. make.conf is read before each port's own Makefile, which is why a
port that assigns PKGNAMESUFFIX instead of appending to it needs an overlay --
`conflicts()` names those so the plan fails in CI rather than four hours into a
build.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

BEGIN = "# BEGIN FreeSense delta suffix (generated from the sealed mirror plan)"
END = "# END FreeSense delta suffix"
SUFFIX = re.compile(r"^-[a-z0-9]+$")
ORIGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*/[A-Za-z0-9][A-Za-z0-9+_.-]*$")
REGION = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.DOTALL)


def region(collisions: list[dict], suffix: str) -> str:
    """Return one conditional block per port whose name also exists upstream."""
    if not SUFFIX.fullmatch(suffix):
        raise ValueError("delta suffix must be a lowercase alphanumeric dash suffix")
    origins = sorted({entry["origin"] for entry in collisions})
    for origin in origins:
        if not ORIGIN.fullmatch(origin):
            raise ValueError(f"unsafe port origin in the sealed plan: {origin}")
    lines = [BEGIN]
    for origin in origins:
        # The :N filter empties the value when .CURDIR matches, which is the
        # idiom make.conf already uses for the frr ETCDIR override. The pattern
        # has no trailing wildcard, so net/haproxy cannot match net/haproxy-devel.
        lines.append(f".if ${{.CURDIR:N*/{origin}}}==\"\"")
        lines.append(f"PKGNAMESUFFIX+=\t{suffix}")
        lines.append(".endif")
    lines.append(END)
    return "\n".join(lines) + "\n"


def apply(make_conf: str, rendered: str) -> str:
    """Append or replace the region, so re-rendering is idempotent."""
    if REGION.search(make_conf):
        return REGION.sub(rendered, make_conf)
    separator = "" if make_conf.endswith("\n") else "\n"
    return make_conf + separator + "\n" + rendered


def conflicts(collisions: list[dict], evaluated: dict[str, str], suffix: str) -> list[str]:
    """Names where the suffix did not take, so the port must assign it itself.

    `evaluated` maps a package name to the PKGBASE the ports tree produces with
    the region in place. A port whose own Makefile assigns PKGNAMESUFFIX rather
    than appending wins, because make.conf is read first.
    """
    return sorted(entry["name"] for entry in collisions
                  if evaluated.get(entry["name"]) != entry["name"] + suffix)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror-plan", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path("config/mirror-policy.json"))
    parser.add_argument("--make-conf", type=Path, required=True,
                        help="make.conf to rewrite in place with the rendered region")
    args = parser.parse_args()
    plan = json.loads(args.mirror_plan.read_text(encoding="utf-8"))
    if plan.get("schema_version") != "freesense.mirror-plan/v1":
        raise SystemExit("invalid mirror plan")
    suffix = json.loads(args.policy.read_text(encoding="utf-8")).get("delta_suffix")
    rendered = region(plan["collisions"], suffix)
    args.make_conf.write_text(apply(args.make_conf.read_text(encoding="utf-8"), rendered),
                              encoding="utf-8")


if __name__ == "__main__":
    main()
