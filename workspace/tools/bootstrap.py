"""Install the staged local System files on an explicitly selected, pristine FreeBSD lab guest."""
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile

from check import CheckError, PLIST, PORTS, ROOT, SYSTEM, plist_check

GUEST_SCRIPT = """set -eu
test "$(uname -s)" = FreeBSD && test "$(id -u)" = 0 || { echo 'FreeBSD lab guest with uid 0 required' >&2; exit 1; }
command -v php >/dev/null || { echo 'FreeBSD PHP 8.6 CLI required' >&2; exit 1; }
umask 077
d=$(mktemp -d /var/tmp/freesense-bootstrap.XXXXXXXX)
trap 'rm -rf "$d"' EXIT
cd "$d"
tar -xzf -
php php-check.php preflight
php bootstrap-test.php
sh stage-system.sh src stage pkg-plist
unset FREESENSE_CONFIG_DIR
php bootstrap-install.php
"""


def bundle() -> bytes:
    modes = plist_check()
    source = ROOT / SYSTEM / "src"
    port = ROOT / PORTS / PLIST.parent
    receipt = {}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, mode in sorted(modes.items()):
            if not path.startswith("/usr/local/") or not re.fullmatch(r"(?:/[A-Za-z0-9_.-]+)+", path):
                raise CheckError(f"review lab install path before expanding managed paths: {path}")
            file = source / path[1:]
            data = file.read_bytes()
            receipt[path] = {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode}
            member = tarfile.TarInfo("src" + path)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        for file in [port / PLIST.name, port / "files/stage-system.sh", ROOT / "tools/php-check.php",
                     ROOT / "tools/bootstrap-install.php", ROOT / "tools/bootstrap-test.php"]:
            archive.add(file, arcname=file.name)
        for name, data in [("modes.json", modes), ("receipt.json", receipt)]:
            raw = json.dumps(data, sort_keys=True).encode()
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guest", required=True, help="SSH alias of the dedicated disposable guest")
    parser.add_argument("--ssh-config", default=os.environ.get("FREESENSE_TEST_SSH_CONFIG"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.guest):
        parser.error("expected a single SSH alias")
    command = ["ssh"]
    if args.ssh_config:
        command += ["-F", str(Path(args.ssh_config).resolve(strict=True))]
    command += ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
                args.guest, "/bin/sh -c " + shlex.quote(GUEST_SCRIPT)]
    subprocess.run(command, input=bundle(), check=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, CheckError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Bootstrap failed: {error}")
