#!/usr/bin/env python3
"""Render a credential-free complete System worker for the hosted-runner trial."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def function(source, name, following):
    start = source.index(f"{name}() {{")
    end = source.index(f"\n{following}()", start)
    return source[start:end]


def render(values, os_base_sha):
    if values["verified"] != "true" or not re.fullmatch(r"[0-9a-f]{40}", os_base_sha):
        raise ValueError("Complete System experiment requires verified inputs and an exact branch commit")
    common = (ROOT / "scripts/runner/worker-common.sh").read_text()
    runtime_start = common.index("clone_exact() {")
    runtime_end = common.index("\nfetch_input()", runtime_start)
    runtime = common[runtime_start:runtime_end]
    start = runtime.index('  if [ -n "${FREESENSE_REPO_SIGNING_KEY}" ]; then')
    end = runtime.index("  trusted_fingerprint=", start)
    runtime = runtime[:start] + "  cp /root/sign/repo.pub /root/sign/channel-public.pem\n" + runtime[end:]

    installer = (ROOT / "scripts/runner/install-worker-tools.sh").read_text()
    entry = "install_worker_tools() (\n  set -eu\n"
    installer = installer.replace(entry, entry + "  unset ALTABI\n")
    package_env = 'env ASSUME_ALWAYS_YES=no DEFAULT_ALWAYS_YES=no IGNORE_OSVERSION="${ignore_osversion}" \\\n'
    installer = installer.replace(
        package_env,
        'env ASSUME_ALWAYS_YES=no DEFAULT_ALWAYS_YES=no IGNORE_OSVERSION="${ignore_osversion}" '
        'OSVERSION="${required_osversion}" \\\n',
    )
    if "unset ALTABI" not in installer or 'OSVERSION="${required_osversion}"' not in installer:
        raise ValueError("worker-tool installer contract changed")

    identity = hashlib.sha256(
        (values["fingerprint"] + os_base_sha + "github-system-full-v1").encode()
    ).hexdigest()
    env = {
        "STAGE": "system", "FINGERPRINT": identity, "SYSTEM_ID": identity,
        "PLATFORM_ID": values["artifact_platform"],
        "SOURCE_SHA": values["artifact_source_sha"],
        "SYSTEM_SHA": values["artifact_system_sha"],
        "PACKAGES_SHA": values["artifact_packages_sha"], "OS_BASE_SHA": os_base_sha,
        "FREEBSD_SHA": values["artifact_freebsd_sha"],
        "PORTS_SHA": values["artifact_ports_sha"],
        "JAIL_OBJECT": values["artifact_jail_object"],
        "FREEBSD_PIN_ID": values["artifact_freebsd_pin_id"],
        "IMAGE_SHA256": values["artifact_image_sha256"],
        "WORKER_TOOLS_SHA256": values["artifact_worker_tools_sha256"],
        "PUBLIC_BASE_URL": "https://pkg.freesense.org/v1",
        "PACKAGE_TRAIN": values["package_train"],
        "PRODUCT_VERSION": values["release_version"] + "-DEVELOPMENT",
        "GENERATION": "1", "TARGET": "amd64", "ARCHITECTURE": "amd64",
        "SYSTEM_PART": "full", "SYSTEM_SHARD_INDEX": "0", "SYSTEM_SHARD_COUNT": "1",
        "PACKAGE_ARCH": "amd64", "ABI": values["abi"], "ALTABI": values["altabi"],
        "OSVERSION": str(values["osversion"]), "FREEBSD_TARGET": "amd64",
        "FREEBSD_TARGET_ARCH": "amd64", "POUDRIERE_ARCH": "amd64.amd64",
        "KERNEL": "FreeSense",
    }
    fetch = r'''
fetch_input() {
  object=$1 destination=$2
  fetch -qo "${destination}" "${PUBLIC_BASE_URL}/${object}"
  test "$(sha256 -q "${destination}")" = "${object##*/}"
}
'''
    stage = (ROOT / "scripts/runner/stages/system.sh").read_text()
    terminal = '  sign_repository "${system_repository}"\n  publish_repository "${system_repository}"\n'
    if terminal not in stage:
        raise ValueError("System stage publication contract changed")
    package = r'''
phase experimental-system-catalog
pkg repo /root/work/system
test -s /root/work/system/packagesite.pkg
package_count=$(find /root/work/system/All -type f -name '*.pkg' | wc -l | tr -d ' ')
test "${package_count}" -gt 5
phase experimental-system-package
tar -cf - -C /root/work/system . | zstd -T2 -3 -o /tmp/system-full.tar.zst
archive_sha=$(sha256 -q /tmp/system-full.tar.zst)
archive_size=$(stat -f %z /tmp/system-full.tar.zst)
split -b 1900m -a 2 /tmp/system-full.tar.zst /root/experiment-output/system-full.tar.zst.part-
jq -n --arg schema freesense.github-system-full/v1 --arg fingerprint "${FINGERPRINT}" \
  --arg os_base_sha "${OS_BASE_SHA}" --arg source_sha "${SOURCE_SHA}" \
  --arg system_ports_sha "${SYSTEM_SHA}" --arg freebsd_sha "${FREEBSD_SHA}" \
  --arg ports_sha "${PORTS_SHA}" \
  --arg sha256 "${archive_sha}" --argjson size "${archive_size}" \
  --argjson package_count "${package_count}" \
  '{schema_version:$schema,fingerprint:$fingerprint,os_base_sha:$os_base_sha,source_sha:$source_sha,system_ports_sha:$system_ports_sha,freebsd_sha:$freebsd_sha,ports_sha:$ports_sha,sha256:$sha256,size:$size,package_count:$package_count}' \
  >/root/experiment-output/system-full.json
phase experimental-system-ready
'''
    stage = stage.replace(terminal, package, 1)
    lines = ["#!/bin/sh", "set -eu",
             "export HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin",
             "export ASSUME_ALWAYS_YES=yes LC_ALL=C LANG=C TZ=UTC", "umask 022"]
    lines += [f"export {key}={shlex.quote(str(value))}" for key, value in env.items()]
    public_key = base64.b64encode((ROOT / "config/channel-signing-public.pem").read_bytes()).decode()
    lines += ["mkdir -p /root/sign /root/work /root/experiment-output",
              "printf '%s' " + shlex.quote(public_key) + " | openssl base64 -d -A >/root/sign/repo.pub",
              'phase() { printf "FreeSense phase: %s\\n" "$1"; df -k /; }',
              installer, "install_worker_tools", runtime,
              fetch, function(common, "create_source_archive", "sign_repository"), stage]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--os-base-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    descriptor = args.output / "channel.json"
    subprocess.run([sys.executable, str(ROOT / "scripts/channel.py"), "--public-key",
                    str(ROOT / "config/channel-signing-public.pem"), "--channel", "devel",
                    "--component", "system", "--json-output", str(descriptor)], check=True)
    worker = render(json.loads(descriptor.read_text()), args.os_base_sha)
    (args.output / "worker.sh").write_text(worker, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
