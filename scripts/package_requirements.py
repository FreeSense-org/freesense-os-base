#!/usr/bin/env python3
"""Collect seed requirements from a configured native FreeBSD ports tree.

This only evaluates make metadata; it never builds or installs a package. Run
inside the target worker after overlay application and Poudriere make.conf
rendering. Unknown make.conf assignments fail closed pending an explicit audit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from multiarch_pin import ARCHES

# These assignments affect metadata, dependency selection, fetch location or
# packaging, rather than silently changing a package's compiled interfaces.
# Version/dependency/OPTIONS differences are checked separately by binary_seed.
METADATA_KNOBS = {
    "ALLOW_UNSUPPORTED_SYSTEM", "PKG_COMPRESSION_FORMAT", "FREESENSE_PKG_SET_VERSION",
    "FREESENSE_PACKAGE_TRAIN", "PRODUCT_NAME", "PRODUCT_VERSION", "POUDRIERE_PORTS_NAME",
    "MASTER_SITE_OVERRIDE", "DEFAULT_VERSIONS", "CUR_ARCH", "NATIVE_BUILD",
    "IGNORE_OSVERSION", "PKG_ENV",
    # Renames FreeSense's own layer of the repository so the pkg solver can never
    # substitute it for the upstream package of the same name. It changes
    # packaging identity, not compiled interfaces, which is what this set is for.
    # Rendered at build time by delta_suffix.py from the sealed mirror plan.
    "PKGNAMESUFFIX",
}
SOURCE_KNOBS = {"ETCDIR": r"net/frr"}
PORT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*/[A-Za-z0-9][A-Za-z0-9+_.-]*(?:@[A-Za-z0-9][A-Za-z0-9+_.-]*)?$")
ASSIGNMENT = re.compile(r"^([A-Za-z0-9_.-]+)\s*(?:\+|\?|:|!)?=")
OPTIONS_KNOB = re.compile(r"^(?:OPTIONS_(?:SET|UNSET)(?:_FORCE)?|.+_(?:SET|UNSET)(?:_FORCE)?)$")
DEPENDENCY_FIELDS = ("PKG_DEPENDS", "FETCH_DEPENDS", "EXTRACT_DEPENDS", "PATCH_DEPENDS", "BUILD_DEPENDS", "LIB_DEPENDS", "RUN_DEPENDS")


def audit_make_conf(text: str) -> set[str]:
    logical = text.replace("\\\n", " ")
    assignments = set()
    for line in logical.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("."):
            if not re.match(r"^\.\s*(?:if|elif|else|endif)\b", stripped):
                raise ValueError("unaudited make.conf directive")
            continue
        match = ASSIGNMENT.match(stripped)
        if not match:
            raise ValueError("unaudited make.conf syntax")
        name = match[1]
        if name not in METADATA_KNOBS and name not in SOURCE_KNOBS and not OPTIONS_KNOB.fullmatch(name):
            raise ValueError(f"unaudited non-OPTIONS knob: {name}")
        assignments.add(name)
    return assignments


def dependency_origin(value: str) -> str:
    parts = value.split(":")
    if len(parts) not in {2, 3} or not PORT.fullmatch(parts[1]):
        raise ValueError(f"unsupported dependency specification: {value}")
    return parts[1]


class Collector:
    def __init__(self, ports: Path, make_conf: Path, architecture: str, ports_sha: str):
        self.ports, self.make_conf, self.architecture = ports.resolve(), make_conf.resolve(), architecture
        self.abi = ARCHES[architecture]
        self.knobs = audit_make_conf(make_conf.read_text())
        self.records = {}
        self.by_origin = {}
        if not re.fullmatch(r"[0-9a-f]{40}", ports_sha):
            raise ValueError("ports revision must be an exact commit")
        actual = subprocess.check_output(["git", "-C", str(self.ports), "rev-parse", "HEAD"], text=True).strip()
        if actual != ports_sha:
            raise ValueError("configured ports tree does not match the pinned revision")
        changed = subprocess.check_output(["git", "-C", str(self.ports), "diff", "--name-only", "HEAD"], text=True)
        untracked = subprocess.check_output(["git", "-C", str(self.ports), "ls-files", "--others", "--exclude-standard"], text=True)
        self.changed = set((changed + "\n" + untracked).splitlines())
        audited_mk = {"Mk/bsd.freesense-package.mk"}
        if any(path.startswith(("Templates/", "Keywords/")) or (path.startswith("Mk/") and path not in audited_mk) for path in self.changed):
            raise ValueError("customized ports framework requires a separate compatibility audit")

    def values(self, origin: str, names: tuple[str, ...]) -> dict[str, str]:
        if not PORT.fullmatch(origin):
            raise ValueError("invalid port origin/flavor")
        path, _, flavor = origin.partition("@")
        directory = self.ports / path
        directory.resolve().relative_to(self.ports)
        target_arch = self.abi.split(":")[-1]
        command = ["make", "-C", str(directory), f"__MAKE_CONF={self.make_conf}", f"ARCH={target_arch}", "BATCH=yes"]
        if flavor:
            command.append(f"FLAVOR={flavor}")
        for name in names:
            command.extend(["-V", "${" + name + "}"])
        output = subprocess.check_output(command, text=True, timeout=120).splitlines()
        if len(output) != len(names) or any("${" in value or "%%" in value for value in output):
            raise ValueError(f"unresolved or multiline package metadata for {origin}")
        return dict(zip(names, output))

    def visit(self, origin: str) -> str:
        if origin in self.by_origin:
            return self.by_origin[origin]
        values = self.values(origin, ("PKGBASE", "PKGVERSION", "PKGORIGIN", "COMPLETE_OPTIONS_LIST", "PORT_OPTIONS",
                                      "USES", "EXTRA_PATCHES", "SUBPACKAGES", *DEPENDENCY_FIELDS))
        if values["SUBPACKAGES"]:
            raise ValueError(f"subpackage metadata requires explicit collection support: {origin}")
        name = values["PKGBASE"]
        options = {key: key in values["PORT_OPTIONS"].split() for key in values["COMPLETE_OPTIONS_LIST"].split()}
        path = origin.split("@")[0]
        record = {
            "name": name, "version": values["PKGVERSION"], "origin": values["PKGORIGIN"],
            "abi": self.abi, "options": options, "deps": {},
            "overlay": any(changed.startswith(path + "/") for changed in self.changed),
            "custom_patches": bool(values["EXTRA_PATCHES"]),
            "non_options_knobs": any(re.match(SOURCE_KNOBS[key], path) for key in self.knobs if key in SOURCE_KNOBS),
            "kernel_sensitive": "kmod" in values["USES"].split(),
            "base_package": False,
        }
        if name in self.records:
            existing = self.records[name]
            if (existing["origin"] == record["origin"] and
                existing["version"] == record["version"] and
                existing["options"] == record["options"] and
                existing["overlay"] == record["overlay"] and
                existing["custom_patches"] == record["custom_patches"] and
                existing["non_options_knobs"] == record["non_options_knobs"] and
                existing["kernel_sensitive"] == record["kernel_sensitive"]):
                self.by_origin[origin] = name
                return name
            raise ValueError(f"conflicting package name across ports/flavors: {name}")
        self.by_origin[origin], self.records[name] = name, record
        for field in DEPENDENCY_FIELDS:
            for spec in values[field].split():
                dep_origin = dependency_origin(spec)
                if dep_origin == origin:
                    continue
                dep_name = self.visit(dep_origin)
                if field in {"LIB_DEPENDS", "RUN_DEPENDS"}:
                    dep = self.records[dep_name]
                    record["deps"][dep_name] = {"version": dep["version"], "origin": dep["origin"]}
        return name

    def collect(self, roots: dict[str, list[str]]) -> dict:
        if set(roots) != {"system", "packages"} or any(not items for items in roots.values()):
            raise ValueError("both component root lists are required")
        root_names = {component: sorted({self.visit(origin) for origin in origins}) for component, origins in roots.items()}
        self.visit("lang/rust")
        self.visit("ports-mgmt/pkg")
        return {"schema_version": "freesense.package-requirements/v1", "abi": self.abi,
                "roots": root_names, "packages": [self.records[name] for name in sorted(self.records)],
                "make_conf_sha256": hashlib.sha256(self.make_conf.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports", type=Path, required=True)
    parser.add_argument("--make-conf", type=Path, required=True)
    parser.add_argument("--architecture", choices=ARCHES, required=True)
    parser.add_argument("--ports-sha", required=True)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected = "aarch64" if args.architecture == "arm64" else "amd64"
    if os.uname().sysname != "FreeBSD" or subprocess.check_output(["uname", "-p"], text=True).strip() != expected:
        raise SystemExit("requirements must be evaluated on the native target FreeBSD worker")
    result = Collector(args.ports, args.make_conf, args.architecture, args.ports_sha).collect(json.loads(args.roots.read_text()))
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
