"""Validate ownership of admitted System and Optional Packages port overlays."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

from pathlib import Path, PurePosixPath
import re
import public_php

SYSTEM = "freesense-system-ports"
OPTIONAL = "freesense-packages"
SYSTEM_PORT = "security/FreeSense-system"
FRAMEWORK = "Mk/bsd.freesense-package.mk"

# Origin names are case sensitive. The guest-tools wrapper belongs to Optional
# Packages even though the underlying open-vm-tools overlay belongs to System.
RESERVED = frozenset("""
devel/FreeSense-composer-deps devel/php-FreeSense-module emulators/open-vm-tools
net/filterdns security/FreeSense security/FreeSense-system security/php-openssl_x509_crl
security/phpseclib sysutils/FreeSense-Status_Monitoring sysutils/FreeSense-default-config
sysutils/FreeSense-default-config-serial sysutils/FreeSense-platform-abi sysutils/FreeSense-repoc
sysutils/FreeSense-upgrade sysutils/check_reload_status sysutils/cpustats sysutils/dhcpleases
sysutils/dhcpleases6 sysutils/filterlog sysutils/minicron sysutils/qstats sysutils/ssh_tunnel_shell
sysutils/voucher sysutils/wrapalixresetbutton
""".split())

# Check all supported lldpd options, including paths absent from the tiny flavor.
LLDP_PLIST_SUBSTITUTIONS = {
    "%%ETCDIR%%": "etc/lldpd",
    "%%DOCSDIR%%": "share/doc/lldpd",
    "%%PORTDOCS%%": "",
    "%%BASH%%": "",
    "%%ZSH%%": "",
}


def installed_path(entry: str) -> str:
    absolute = entry if entry.startswith("/") else "/usr/local/" + entry
    if (str(PurePosixPath(absolute)) != absolute or ".." in PurePosixPath(absolute).parts
            or absolute.startswith("//") or "\\" in absolute):
        raise ValueError(f"invalid package install path: {entry}")
    return absolute


def file_claims(port: Path, optional: bool) -> set[str]:
    """Read concrete file claims; require resolution of new dynamic plist syntax."""
    manifest = port / "pkg-plist"
    lines = manifest.read_text(encoding="utf-8").splitlines() if manifest.is_file() else []
    # Some local-only ports name individual static files directly in the Makefile.
    makefile = (port / "Makefile").read_text(encoding="utf-8").replace("\\\n", " ")
    for values in re.findall(r"^PLIST_FILES\s*[+?:]?=([^#\n]*)", makefile, re.MULTILINE):
        lines.extend(values.split())
    paths = set()
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith(("@comment ", "@dir ", "@dir(")):
            continue
        entry = re.sub(r"^@\([^)]*\)\s+", "", entry)
        # @shell installs one concrete file and lists it in /etc/shells.
        entry = re.sub(r"^@shell\s+", "", entry)
        if optional and port.parent.name == "net-mgmt" and port.name == "lldpd":
            for variable, path in LLDP_PLIST_SUBSTITUTIONS.items():
                entry = entry.replace(variable, path)
        if entry.startswith("@sample "):
            files = entry.split()[1:]
            if len(files) == 1 and files[0].endswith(".sample"):
                files.append(files[0].removesuffix(".sample"))
            if len(files) != 2:
                raise ValueError(f"{port}: @sample requires a concrete sample and destination: {entry}")
        else:
            files = [entry]
        if any(file.startswith("@") or "%%" in file or "$" in file for file in files):
            raise ValueError(f"{port}: resolve dynamic packing list before checking ownership: {entry}")
        for file in files:
            if optional and file.startswith("etc/inc/priv/"):
                raise ValueError(f"{port}: privilege registrations must install under absolute /etc/inc/priv/")
            path = installed_path(file)
            public_php.check_name(path)
            public_php.check_asset_location(PurePosixPath(path), optional=optional)
            paths.add(path)
    return paths


def audit(root: Path, system_files: dict[str, int]) -> None:
    system = root / SYSTEM
    optional = root / OPTIONAL
    for parent, directories, files in system.walk():
        directories[:] = [name for name in directories if name != ".git"]
        for name in directories + files:
            if name.startswith("FreeSense-pkg-"):
                raise ValueError(f"optional wrapper in System overlay: {(parent / name).relative_to(system)}")
    if (system / FRAMEWORK).exists():
        raise ValueError("optional package framework is installed in the System overlay")
    if not optional.exists():
        print("Port ownership checked (System overlay only).")
        return
    if not (optional / FRAMEWORK).is_file():
        raise ValueError(f"optional package overlay requires {FRAMEWORK}")

    system_ports = {mf.parent.relative_to(system).as_posix(): mf.parent for mf in system.glob("*/*/Makefile")}
    optional_entries = {path.relative_to(optional).as_posix() for path in optional.glob("*/*")}
    conflicts = sorted((RESERVED | system_ports.keys()) & optional_entries)
    if conflicts:
        raise ValueError(f"System origin is present in the optional overlay: {', '.join(conflicts)}")
    owners = {path: SYSTEM_PORT for path in system_files}
    for origin, port in system_ports.items():
        if origin != SYSTEM_PORT:
            for path in file_claims(port, False):
                owners[path] = origin
    for mf in sorted(optional.glob("*/*/Makefile")):
        origin = mf.parent.relative_to(optional).as_posix()
        public_php.check_package(mf.parent)
        for path in sorted(file_claims(mf.parent, True)):
            if path in owners:
                raise ValueError(f"installed file {path} claimed by System {owners[path]} and Optional Packages {origin}")
    print("Port ownership checked (System and Optional Packages).")
