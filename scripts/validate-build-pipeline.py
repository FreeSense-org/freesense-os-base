#!/usr/bin/env python3
"""Check the few trust boundaries that must remain true across the build."""

from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


expected_workflows = {
    "broker.yml", "ci.yml", "packages.yml", "pin.yml", "release.yml",
    "runner-build.yml", "stable-1.0.yml", "system.yml",
}
workflow_paths = sorted(WORKFLOWS.glob("*.yml"))
require({path.name for path in workflow_paths} == expected_workflows,
        "workflow surface differs from the supported control plane")

action_reference = re.compile(r"uses:\s+[^\s]+@([^\s#]+)")
for workflow in workflow_paths:
    text = workflow.read_text(encoding="utf-8")
    for reference in action_reference.findall(text):
        require(bool(re.fullmatch(r"[0-9a-f]{40}", reference)),
                f"{workflow.name} has an unpinned action: {reference}")
    if "runs-on: [self-hosted, build-runner]" in text:
        require(workflow.name in {"pin.yml", "runner-build.yml"},
                f"{workflow.name} bypasses the supported build-runner entry points")

reusable = read(".github/workflows/runner-build.yml")
pin_workflow = read(".github/workflows/pin.yml")
for workflow in (reusable, pin_workflow):
    require("apt-get" not in workflow,
            "dedicated runners must be provisioned outside build workflows")
for name in ("system.yml", "packages.yml", "release.yml", "stable-1.0.yml"):
    require("uses: ./.github/workflows/runner-build.yml" in read(f".github/workflows/{name}"),
            f"{name} bypasses the reusable KVM executor")
require("schedule:" in read(".github/workflows/system.yml"),
        "the daily System check is not scheduled")
packages_workflow = read(".github/workflows/packages.yml")
require("workflow_run:" in packages_workflow and "workflows: [System]" in packages_workflow,
        "optional packages are not chained to System")
require("schedule:" not in packages_workflow,
        "optional packages retain a racing fixed schedule")
require('cron: "0 6 * * *"' in read(".github/workflows/system.yml"),
        "the daily System check is not fixed at 06:00 UTC")
stable_workflow = read(".github/workflows/stable-1.0.yml")
require("schedule:" not in stable_workflow and "channel seal-stable" in stable_workflow,
        "stable 1.0 is not an explicit one-time seal")
require("freebsd_pin_id" in reusable and "product_version" in reusable,
        "reusable builds omit the release or FreeBSD pin identity")

policy = json.loads(read("config/build-policy.json"))
require(policy.get("runner") == {"vcpus": 12, "memory_mib": 32768, "disk_gib": 160},
        "runner policy differs from the dedicated host contract")
runner = read("scripts/runner/run-vm.sh")
for value in ("-smp 12", "-m 32768", 'qemu-img resize -q "$overlay" 160G',
              "/dev/kvm", "cleanup_orphans", "trap cleanup EXIT", "qemu_owns_overlay"):
    require(value in runner, f"KVM runner contract is missing {value!r}")
require('sha256sum "$base_image"' in runner and 'sha256sum "$download"' in runner,
        "cached and downloaded worker images are not SHA-256 checked")

installer = read("scripts/runner/install-worker-tools.sh")
common = read("scripts/runner/worker-common.sh")
worker = installer + "\n" + common
require(not re.search(
    r"(?m)^[ \t]*(?:env[ \t]+[^#\n]*[ \t]+)?pkg[ \t]+(?:update|install|bootstrap)(?:[ \t]|$)",
    worker,
), "worker depends on a mutable live package catalogue")
require("FREESENSE_USE_PACKAGE_FETCH=1" not in common and "PACKAGE_FETCH_URL=" not in common,
        "Poudriere binary-package fetching bypasses the pinned ports tree")
for value in ("/inputs/sha256/${WORKER_TOOLS_SHA256}",
              'sha256 -q "${worker_tools_archive}"',
              'pkg add "${worker_tools}/${package}"', "pkg check -d -n -q -a"):
    require(value in installer, f"worker-tool integrity contract is missing {value!r}")
require("pkg add -f" not in installer,
        "worker-tool installation bypasses package ABI checks")

stage_dir = ROOT / "scripts" / "runner" / "stages"
require({path.stem for path in stage_dir.glob("*.sh")} == {"system", "packages", "iso"},
        "stage surface differs from system/packages/iso")
system_stage = read("scripts/runner/stages/system.sh")
packages_stage = read("scripts/runner/stages/packages.sh")
iso_stage = read("scripts/runner/stages/iso.sh")
for value in ('TMPFS_BLACKLIST="rust telegraf"',
              "TMPFS_BLACKLIST_TMPDIR=/usr/local/poudriere/data/cache/tmp"):
    require(value in packages_stage,
            f"optional heavy-package disk workdir contract is missing {value!r}")
for value in ("verify_repository()", "packagesite.yaml.sig",
              "repository packages do not match the signed catalog"):
    require(value in common, f"signed repository verification is missing {value!r}")
require("--immutable --checksum --multi-thread-streams 0" in common,
        "artifact uploads can overwrite or duplicate immutable results")
require("optional-closure-check" in packages_stage and "'%dn|%dv'" in packages_stage,
        "optional dependency closure is not validated")
repository_payload = common.rfind('upload_immutable "${file}"')
repository_complete = common.rfind('upload_immutable "${directory}/complete.json"')
require(repository_payload >= 0 and repository_complete > repository_payload,
        "repository completion marker is not uploaded last")
iso_payload = iso_stage.rfind('upload_immutable "${iso}"')
iso_complete = iso_stage.rfind('upload_immutable /tmp/complete.json')
require(iso_payload >= 0 and iso_complete > iso_payload,
        "ISO completion marker is not uploaded last")
require("repos.manifest.json" not in iso_stage and
        "CHANNEL_PAYLOAD_B64" in iso_stage and "CHANNEL_SIGNATURE_B64" in iso_stage,
        "ISO does not consume the exact selected signed channel payload")
for value in ("FREESENSE_INSTALLER_PATCH_B64", "git apply --check",
              "FREESENSE_ASSEMBLY_INSTALLER_OVERLAY", "startbsdinstall",
              "copy_configxml_from_usb", "fix_fstab"):
    require(value in iso_stage or value in read("scripts/render-worker.py"),
            f"ISO installer payload contract is missing {value!r}")
require("name: Reuse completed immutable result" in reusable and
        "name: Verify required System result" in reusable,
        "host-side immutable reuse or prerequisite check is missing")
require("create_source_archive" in system_stage and "create_source_archive" in packages_stage,
        "package builds do not create their deterministic source distfile")

lock = json.loads(read("config/freebsd-16.json"))
require(lock.get("schema_version") == "freesense.freebsd-pin/v2" and lock.get("ready") is True,
        "FreeBSD lock is not ready schema v2")
for name in ("freebsd_source", "freebsd_ports"):
    require(bool(re.fullmatch(r"[0-9a-f]{40}", lock.get(name, {}).get("commit", ""))),
            f"{name} is not pinned to a full Git commit")
for name in ("jail_seed", "worker_image", "worker_tools"):
    item = lock.get(name, {})
    sha = item.get("sha256", "")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", sha)) and
            item.get("object") == f"inputs/sha256/{sha}" and
            isinstance(item.get("size"), int) and item["size"] > 0,
            f"{name} is not a complete content-addressed pin")

pin_contract = pin_workflow + read("scripts/pin-worker-tools.sh")
for value in ("scripts/resolve_worker_tools.py", "packagesite.yaml.sig",
              "packagesite.yaml.pub", "install_worker_tools"):
    require(value in pin_contract, f"Pin FreeBSD trust contract is missing {value!r}")

broker = read("broker/src/index.js")
for role in ("coordinator", "artifact-writer", "pin-writer", "channel-writer", "broker-smoke"):
    require(role in broker, f"credential broker lacks {role}")
require("GITHUB_REPOSITORY_ID" in broker and "ref_protected" in broker,
        "credential broker is not bound to protected main")

print("Build control-plane integrity boundaries: valid")
