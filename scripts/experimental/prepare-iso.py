#!/usr/bin/env python3
"""Render an unsigned-output ISO experiment from a verified public channel.

Reuse the production assembler and its verification functions. No private key,
OIDC credential, R2 configuration, or production upload function is included.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def function(source, name, following):
    start = source.index(f"{name}() {{")
    end = source.index(f"\n{following}()", start)
    return source[start:end]


def render(values):
    if values["verified"] != "true" or values["packages_verified"] != "true":
        raise ValueError("experiment requires a verified System/Packages pair")
    common = (ROOT / "scripts/runner/worker-common.sh").read_text()
    configure = function(common, "configure_source", "configure_poudriere")
    start = configure.index("  printf '%s' \"${FREESENSE_REPO_SIGNING_KEY}\"")
    end = configure.index("  trusted_fingerprint=", start)
    configure = configure[:start] + "  cp /root/sign/repo.pub /root/sign/channel-public.pem\n" + configure[end:]
    identity = hashlib.sha256((values["payload_sha256"] + "github-iso-experiment").encode()).hexdigest()
    env = {
        "STAGE": "iso", "CHANNEL": "devel", "TARGET": "amd64",
        "ARCHITECTURE": "amd64", "PACKAGE_ARCH": "amd64",
        "FREEBSD_TARGET": "amd64", "FREEBSD_TARGET_ARCH": "amd64",
        "POUDRIERE_ARCH": "amd64.amd64", "KERNEL": "FreeSense",
        "ABI": values["abi"], "ALTABI": values["altabi"],
        "PUBLIC_BASE_URL": "https://pkg.freesense.org/v1",
        "SYSTEM_ID": values["fingerprint"], "PACKAGES_ID": values["packages_fingerprint"],
        "SOURCE_SHA": values["artifact_source_sha"],
        "FREEBSD_SHA": values["artifact_freebsd_sha"],
        "PORTS_SHA": values["artifact_ports_sha"],
        "JAIL_OBJECT": values["artifact_jail_object"],
        "WORKER_TOOLS_SHA256": values["artifact_worker_tools_sha256"],
        "IMAGE_SHA256": values["artifact_image_sha256"],
        "PLATFORM_ID": values["artifact_platform"],
        "PACKAGE_TRAIN": values["package_train"],
        "PRODUCT_VERSION": values["release_version"] + "-DEVELOPMENT",
        "GENERATION": str(values["generation"]), "SYSTEM_GENERATION": str(values["generation"]),
        "CHANNEL_PAYLOAD_SHA256": values["payload_sha256"],
        "CHANNEL_PAYLOAD_B64": values["payload_base64"],
        "CHANNEL_SIGNATURE_B64": values["signature_base64"],
        "PUBLISH_ENABLED": "false", "FINGERPRINT": identity, "BUNDLE_ID": identity,
        "RESULT": "/root/experiment-output", "IMAGE_PROFILE": "generic-amd64",
        "FIRMWARE": "bios,uefi", "INSTALLER_FORMAT": "iso",
        "IMAGE_CAPABILITIES": json.dumps({"bios": True, "uefi": True, "iso": True}),
        "FREESENSE_INSTALLER_PATCH_B64": base64.b64encode((ROOT / "patches/0005-installer.patch").read_bytes()).decode(),
    }
    lines = ["#!/bin/sh", "set -eu", "export HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin",
             "export ASSUME_ALWAYS_YES=yes LC_ALL=C LANG=C TZ=UTC", "umask 022"]
    lines += [f"export {key}={shlex.quote(value)}" for key, value in env.items()]
    lines += ["mkdir -p /root/sign /root/work /root/experiment-output",
              "printf '%s' " + shlex.quote(base64.b64encode((ROOT / "config/channel-signing-public.pem").read_bytes()).decode()) + " | openssl base64 -d -A >/root/sign/repo.pub",
              'phase() { printf "FreeSense phase: %s\\n" "$1"; df -k /; }',
              (ROOT / "scripts/runner/install-worker-tools.sh").read_text(),
              "install_worker_tools",
              function(common, "clone_exact", "configure_source"), configure]
    # Preserve the production catalogue signature, ABI and every-package checksum checks.
    start = common.index("verify_repository() (")
    end = common.index("\nfetch_repository()", start)
    lines += [common[start:end], (ROOT / "scripts/experimental/public-inputs.sh").read_text(),
              (ROOT / "scripts/runner/assembly-common.sh").read_text(),
              (ROOT / "scripts/runner/stages/iso.sh").read_text()]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    descriptor = args.output / "channel.json"
    subprocess.run([sys.executable, str(ROOT / "scripts/channel.py"),
                    "--public-key", str(ROOT / "config/channel-signing-public.pem"),
                    "--channel", "devel", "--component", "system",
                    "--json-output", str(descriptor)], check=True)
    values = json.loads(descriptor.read_text())
    (args.output / "worker.sh").write_text(render(values), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
