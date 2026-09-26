#!/usr/bin/env python3
"""Classify the FreeSense delta and close it under runtime dependencies.

Only packages FreeSense customizes -- plus everything that links or runs against
them -- are built from source. The frozen upstream mirror supplies the rest, so a
version difference between the pinned ports tree and the published catalogue
stops being a reason to rebuild anything.

The reverse closure is the load-bearing safety property of the layered design.
pkg will install an upstream binary against a FreeSense-modified library and
report nothing, so every dependent of a customized package must come from the
FreeSense layer too. Closure runs over the recorded dependencies, which
package_requirements collects from LIB_DEPENDS and RUN_DEPENDS only; a build-only
edge does not change an already-compiled binary and must not widen the delta.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from binary_seed import FLAGS, options as normalize_options
from multiarch_pin import ARCHES
from package_requirements import audit_make_conf
from resolve_worker_tools import _safe_name, _safe_origin, _safe_version

# Poudriere per-port option knobs are <category>_<port directory>_SET[_FORCE].
# A category never contains an underscore, so the first underscore separates the
# two and a port directory keeps the rest of them (net/nss_ldap, mail/pear-Mail).
OPTIONS_KNOB = re.compile(r"^([A-Za-z0-9][A-Za-z0-9.+-]*)_(.+)_(?:UN)?SET(?:_FORCE)?$")

# A port belongs to FreeSense when FreeSense changed it, not when it happens to
# carry patches. `overlay` is the precise signal: package_requirements derives it
# from a git diff of the pinned ports checkout after the overlays are copied in,
# so any file FreeSense writes into a port directory sets it.
#
# `custom_patches` is deliberately NOT here. It is bool(EXTRA_PATCHES), which is
# true for ports whose own upstream Makefile uses the variable -- archivers/arj
# applies Debian's patch set, dns/bind920 and net/isc-dhcp44-* have upstream
# conditionals, lang/lua54 and graphics/gd only declare OPTIONS-conditional patch
# variables. FreeBSD applies exactly those patches when it builds the package, so
# they are not a divergence. None of the ports it flags appear in the overlay.
SOURCE_ONLY_FLAGS = ("overlay", "non_options_knobs", "kernel_sensitive", "base_package")


def overrides(make_conf: str) -> set[str]:
    """Return the port origins whose OPTIONS FreeSense deliberately diverges on."""
    result = set()
    for knob in audit_make_conf(make_conf):
        match = OPTIONS_KNOB.fullmatch(knob)
        if match:
            result.add(_safe_origin(f"{match[1]}/{match[2]}", "option override origin"))
    return result


def published(records: list[dict]) -> dict[str, dict]:
    """Map every unambiguously published upstream package to its catalogue record.

    An ambiguous name is treated as unpublished: we cannot say which build the
    mirror would serve, so FreeSense builds it instead.
    """
    catalogue, ambiguous = {}, set()
    for record in records:
        name = record.get("name")
        if name in catalogue:
            ambiguous.add(name)
        catalogue[name] = record
    for name in ambiguous:
        del catalogue[name]
    return catalogue


def eligibility(record: dict) -> str:
    """Return why FreeSense must build this package itself, or "" if it need not.

    Fails closed: a record that never declared an eligibility flag is a bug in
    the collector, not a package we may quietly take from upstream.
    """
    for flag in FLAGS:
        if type(record.get(flag)) is not bool:
            raise ValueError(f"missing explicit eligibility audit: {flag}")
    name, origin = record["name"].lower(), record["origin"].lower()
    if (name.startswith(("freesense", "freebsd-"))
            or origin.split("/")[1].startswith("freesense")
            or name.endswith("-kmod") or name in {"world", "kernel"}):
        return "freesense-own"
    for flag in SOURCE_ONLY_FLAGS:
        if record[flag]:
            return flag
    return ""


def requirement_records(requirements: dict) -> dict[str, dict]:
    abi = requirements.get("abi")
    if (requirements.get("schema_version") != "freesense.package-requirements/v1"
            or abi not in ARCHES.values()
            or set(requirements.get("roots", {})) != {"system", "packages"}
            or any(not isinstance(requirements["roots"][key], list) or not requirements["roots"][key]
                   for key in ("system", "packages"))):
        raise ValueError("requirements must cover System and Optional Packages for one known ABI")
    records = {}
    for record in requirements.get("packages", []):
        name = _safe_name(record.get("name"), "requirement name")
        _safe_origin(record.get("origin"), "requirement origin")
        _safe_version(record.get("version"), "requirement version")
        if name in records or record.get("abi") != abi:
            raise ValueError("duplicate or cross-architecture requirement")
        if not isinstance(record.get("deps"), dict):
            raise ValueError("requirements must record an explicit dependency map")
        normalize_options(record.get("options"))
        records[name] = record
    if not records:
        raise ValueError("empty requirements")
    for record in records.values():
        if any(dependency not in records for dependency in record["deps"]):
            raise ValueError("requirements have an incomplete dependency closure")
    for names in requirements["roots"].values():
        if any(name not in records for name in names):
            raise ValueError("root missing from evaluated requirements")
    return records


def classify(records: dict[str, dict], *, options: set[str], upstream: dict[str, dict]) -> dict[str, str]:
    """Return every package FreeSense must build itself, with the reason why."""
    causes = {}
    for name, record in sorted(records.items()):
        reason = eligibility(record)
        if not reason and record["origin"] in options:
            reason = "options-override"
        if not reason and name not in upstream:
            reason = "absent-upstream"
        # Declared overrides state intent; this measures it. OPTIONS sets change
        # between releases, so the comparison only means anything when our tree
        # and the catalogue agree on the version -- otherwise a churned package
        # would look divergent purely because its option list moved on.
        if (not reason and upstream[name].get("version") == record["version"]
                and normalize_options(upstream[name].get("options", {})) != normalize_options(record["options"])):
            reason = "options-divergence"
        if reason:
            causes[name] = reason
    return causes


def dependents(records: dict[str, dict], customized: set[str]) -> set[str]:
    """Close the customized set under the packages that link or run against it."""
    reverse: dict[str, set[str]] = {}
    for name, record in records.items():
        for dependency in record["deps"]:
            reverse.setdefault(dependency, set()).add(name)
    forced: set[str] = set()
    stack = list(customized)
    while stack:
        for parent in reverse.get(stack.pop(), ()):
            if parent not in customized and parent not in forced:
                forced.add(parent)
                stack.append(parent)
    return forced


def reachable(records: dict[str, dict], roots: list[str]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(records[current]["deps"])
    return seen


def churn(records: dict[str, dict], upstream: dict[str, dict], build: set[str]) -> dict[str, dict]:
    """Report uncustomized packages the mirror publishes at a different version.

    These take the mirror's build. They matter because a churned package that is
    also a dependency of a delta port is the one case Poudriere still rebuilds.
    """
    return {
        name: {"ports": record["version"], "mirror": upstream[name]["version"]}
        for name, record in sorted(records.items())
        if name not in build and name in upstream and upstream[name].get("version") != record["version"]
    }


def plan(requirements: dict, catalogue: list[dict], make_conf: str) -> dict:
    records = requirement_records(requirements)
    upstream = published(catalogue)
    causes = classify(records, options=overrides(make_conf), upstream=upstream)
    customized = set(causes)
    cascade = dependents(records, customized)
    build = customized | cascade
    system = reachable(records, requirements["roots"]["system"])
    optional = reachable(records, requirements["roots"]["packages"])
    return {
        "schema_version": "freesense.delta-closure/v1",
        "abi": requirements["abi"],
        "build": [
            {"name": name, "origin": records[name]["origin"],
             "cause": causes.get(name, "reverse-dependency")}
            for name in sorted(build)
        ],
        # The complement: every package in the closure the mirror must serve.
        # Carried explicitly so the mirror plan is a function of this document
        # alone and cannot disagree with it about membership.
        "take": sorted(set(records) - build),
        "components": {
            "system": sorted(system & build),
            "optional": sorted(optional & build),
        },
        # Delta packages whose name also exists upstream. These are the ports
        # that must carry a distinguishing suffix, so the pkg solver can never
        # substitute one layer's build for the other's.
        "collisions": [{"name": name, "origin": records[name]["origin"]}
                       for name in sorted(build & set(upstream))],
        "churn": churn(records, upstream, build),
        "counts": {
            "records": len(records),
            "customized": len(customized),
            "cascade": len(cascade),
            "build": len(build),
            "take": len(records) - len(build),
            "causes": {reason: sum(1 for value in causes.values() if value == reason)
                       for reason in sorted(set(causes.values()))},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--catalogue", type=Path, required=True,
                        help="signed upstream catalogue records, one JSON object per line")
    parser.add_argument("--make-conf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--roots-output", type=Path,
                        help="port origins for the Poudriere bulk list")
    args = parser.parse_args()
    catalogue = [json.loads(line) for line in args.catalogue.read_text(encoding="utf-8").splitlines() if line.strip()]
    document = plan(json.loads(args.requirements.read_text(encoding="utf-8")), catalogue,
                    args.make_conf.read_text(encoding="utf-8"))
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.roots_output:
        origins = sorted({entry["origin"] for entry in document["build"]})
        args.roots_output.write_text("".join(origin + "\n" for origin in origins), encoding="utf-8")


if __name__ == "__main__":
    main()
