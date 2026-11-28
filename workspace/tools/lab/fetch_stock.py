"""Put the current FreeBSD packages that the lab repository needs onto the builder, in /build/v2-pkg/stock.

The builder has no route to pkg.freebsd.org: this downloads them here (local-output/stock-packages) and copies them over.
The set is the one the build driver resolves (freesense-os-base/build/packages.py): the dependencies of the installer's
packages and of our ports. Run it once per lab, and again when a dependency changes or newer packages are wanted.
"""
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "freesense-os-base/build"))
import packages

sys.path.insert(0, str(ROOT / "tools"))
import lab_accept

CACHE = ROOT.parents[1] / "local-output/stock-packages"
BUILDER_DIRECTORY = "/build/v2-pkg/stock"


def main() -> int:
    build = ROOT / "freesense-os-base/build"
    lock = json.loads((build / "lock.json").read_text(encoding="utf-8"))
    base = json.loads((build / "base-provides.json").read_text(encoding="utf-8"))
    repository = lock["packages"]["repository"]
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / "packagesite.pkg"
    urllib.request.urlretrieve(repository + "/packagesite.pkg", archive)
    # tar reads zstd; on Windows that is the system's bsdtar, not Git's.
    text = subprocess.run(["tar", "-xOf", str(archive), "packagesite.yaml"], check=True, capture_output=True).stdout
    ours, wanted = packages.requirements(ROOT, lock["php"])
    result = packages.resolve(ours, wanted, packages.read_catalogue(text.decode("utf-8")), lock, base)
    for error in result["errors"]:
        print(f"warning: {error}")
    files = []
    for entry in packages.manifest(result["catalogue"]):
        target = CACHE / f"{entry['name']}-{entry['version']}.pkg"
        if not target.is_file() or target.stat().st_size != entry["size"]:
            print(f"downloading {target.name}", flush=True)
            urllib.request.urlretrieve(f"{repository}/{entry['repopath']}", target)
        if target.stat().st_size != entry["size"]:
            raise OSError(f"incomplete download: {target.name}")
        files.append(target)
    config = lab_accept.setting("FREESENSE_BUILDER_SSH_CONFIG")
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode="w") as bundle:
            for path in files:
                bundle.add(path, arcname=path.name)
        stream.seek(0)
        subprocess.run([lab_accept.find_ssh(), "-F", config, lab_accept.setting("FREESENSE_BUILDER"),
                        f"rm -rf {BUILDER_DIRECTORY} && mkdir -p {BUILDER_DIRECTORY} && tar -xf - -C {BUILDER_DIRECTORY}"],
                       input=stream.read(), check=True, timeout=900)
    print(f"{len(files)} packages in {BUILDER_DIRECTORY} on the builder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
