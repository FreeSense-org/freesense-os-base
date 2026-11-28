"""Which packages a build needs: the ones we build ourselves and the closure taken from the FreeBSD catalogue.

The names are read from the installer and from our port Makefiles, so the check follows them without a second list.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
import re

INSTALLER = "freesense/src/usr/local/FreeSense/lib/Installer/PackageInstaller.php"
PORTS = "freesense-system-ports"


def installer_packages(text: str) -> list[str]:
    block = re.search(r"\bDEFAULT_PACKAGES\s*=\s*\[(.*?)\]\s*;", text, re.S)
    names = re.findall(r"'([^']+)'", block[1]) if block else []
    if not names:
        raise ValueError("DEFAULT_PACKAGES was not found in the installer")
    return names


def make_words(text: str, variable: str) -> list[str]:
    """Every word assigned to a make variable, in all branches of the file."""
    joined = re.sub(r"\\\n", " ", text)
    words = []
    for value in re.findall(rf"^[ \t]*{variable}[ \t]*[+?]?=(.*)$", joined, re.M):
        words += value.split("#", 1)[0].split()
    return words


def dependency(word: str) -> tuple[str, str, str, str]:
    """'kea>=3.2.1:net/kea' -> ('kea', '>=', '3.2.1', 'net/kea')."""
    match = re.fullmatch(r"([^<>=:]+)(>=|<=|=|>|<)((?:\$\{[^}]*\}|[^:$])+):([^:]+)(?::.*)?", word)
    if not match:
        raise ValueError(f"unsupported dependency form: {word}")
    return match[1], match[2], match[3], match[4]


def port_files(workspace: Path) -> list[Path]:
    return sorted((workspace / PORTS).glob("*/*/Makefile"))


def requirements(workspace: Path, php: dict) -> tuple[dict, list]:
    """Our own ports by package name, and the requests as (name, who asks, operator, version, origin)."""
    wanted = [(name, "installer", "", "", "") for name in installer_packages((workspace / INSTALLER).read_text(encoding="utf-8"))]
    ours = {}
    for makefile in port_files(workspace):
        text = makefile.read_text(encoding="utf-8")
        port = makefile.parent.relative_to(workspace / PORTS).as_posix()
        names = make_words(text, "PORTNAME")
        if len(names) != 1:
            raise ValueError(f"{port}: expected one PORTNAME")
        ours["".join(make_words(text, "PKGNAMEPREFIX")) + names[0] + "".join(make_words(text, "PKGNAMESUFFIX"))] = port
        for word in make_words(text, "RUN_DEPENDS") + make_words(text, "LIB_DEPENDS"):
            name, operator, version, origin = dependency(word)
            wanted.append((name, port, operator, version, origin))
        if any(use.split(":")[0] == "php" for use in make_words(text, "USES")):
            wanted.append((php["package"], port, "", "", ""))
        for extension in make_words(text, "USE_PHP"):
            name = extension.split(":")[0]
            if name not in php["core_extensions"]:
                wanted.append((f"{php['package']}-{name}", port, "", "", ""))
    return ours, wanted


def with_custom_ports(ours: dict, wanted: list, custom: dict) -> tuple[dict, list]:
    """Third-party ports we build with our own options: not taken from the catalogue, but what they run with is."""
    ours = dict(ours)
    wanted = list(wanted)
    for origin, port in sorted(custom.items()):
        ours[port["package"]] = origin
        wanted += [(name, origin, "", "", "") for name in port.get("runtime", [])]
    return ours, wanted


def repository_conf(url: str) -> str:
    return f'Locked: {{ url: "{url}", signature_type: "fingerprints", fingerprints: "/usr/share/keys/pkg", enabled: yes }}\n'


def version_key(version: str) -> tuple:
    """Order FreeBSD package versions: epoch, then the dotted version, then the port revision."""
    version, _, epoch = version.partition(",")
    version, _, revision = version.partition("_")
    parts = []
    for piece in re.findall(r"\d+|[A-Za-z]+", version):
        if piece.isdigit():
            parts.append((1, int(piece), ""))
        else:
            word = piece.lower()
            # alpha, beta, pre and rc come before the release; other letters (p1, a) after it.
            parts.append((0 if word in {"alpha", "beta", "pre", "rc"} else 1, 0, word))
    # A shorter version counts as padded with zeros: 1.0 == 1.0.0, 1.0rc1 < 1.0.
    parts += [(1, 0, "")] * (16 - len(parts))
    return int(epoch or 0), tuple(parts), int(revision or 0)


def satisfies(have: str, operator: str, want: str) -> bool:
    left, right = version_key(have), version_key(want)
    return {">=": left >= right, "<=": left <= right, ">": left > right, "<": left < right, "=": left == right}[operator]


def read_catalogue(text: str) -> dict:
    """packagesite.yaml is one JSON document per line."""
    catalogue = {}
    for line in text.splitlines():
        if line.strip():
            entry = json.loads(line)
            catalogue[entry["name"]] = entry
    return catalogue


def resolve(ours: dict, wanted: list, catalogue: dict, lock: dict, base: dict) -> dict:
    """Close the requests over the catalogue. All problems are returned together in 'errors'."""
    target = lock["targets"][lock["target"]]
    built_extensions = {f"{lock['php']['package']}-{name}": why for name, why in lock["php"]["built_extensions"].items()}
    errors, taken, built, from_base = [], {}, {}, set()
    pending = [(name, who) for name, who, *_ in wanted]
    while pending:
        name, who = pending.pop(0)
        if name in ours:
            built[name] = ours[name]
        elif name in base["packages"]:
            from_base.add(name)
        elif name in built_extensions:
            built[name] = built_extensions[name]
            # We build it against the stock interpreter.
            pending.append((lock["php"]["package"], name))
        elif name in taken:
            continue
        elif name in catalogue:
            taken[name] = catalogue[name]
            pending += [(dep, name) for dep in sorted(catalogue[name].get("deps", {}))]
        else:
            errors.append(f"package {name} (asked for by {who}) is not in the catalogue, the base packages or our ports")

    for name, who, operator, version, origin in wanted:
        entry = taken.get(name)
        if not entry:
            continue
        if operator and "$" not in version and not satisfies(entry["version"], operator, version):
            errors.append(f"{who} needs {name}{operator}{version}; the catalogue has {entry['version']}")
        if origin and entry["origin"].split("@")[0] != origin.split("@")[0]:
            errors.append(f"{who} names origin {origin} for {name}; the catalogue says {entry['origin']}")

    provided = set(base["shlibs"])
    for entry in taken.values():
        provided.update(entry.get("shlibs_provided", []))
    osversion = lock["freebsd_source"]["osversion"]
    for name, entry in sorted(taken.items()):
        if not entry["abi"].startswith(f"{target['abi'].rsplit(':', 1)[0]}:"):
            errors.append(f"{name}: built for {entry['abi']}, not {target['abi']}")
        newer = int(entry.get("annotations", {}).get("FreeBSD_version", 0))
        if newer > osversion:
            errors.append(f"{name}: built on __FreeBSD_version {newer}, newer than the pinned {osversion}")
        for library in entry.get("shlibs_required", []):
            if library not in provided:
                errors.append(f"{name}: needs {library}, which neither the pinned base nor the package set provides")
    return {"errors": sorted(set(errors)), "catalogue": taken, "built": built, "base": sorted(from_base)}


def manifest(entries: dict) -> list[dict]:
    """What the build records about every package it takes from the catalogue."""
    return [{"name": name, "version": entry["version"], "origin": entry["origin"], "repopath": entry["repopath"],
             "sum": entry["sum"], "size": entry["pkgsize"],
             "freebsd_version": entry.get("annotations", {}).get("FreeBSD_version")}
            for name, entry in sorted(entries.items())]
