#!/usr/bin/env python3
"""Static invariants for the small build-runner/R2 control plane."""

from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


expected_workflows = {
    "broker.yml", "ci.yml", "packages.yml", "pin.yml", "release.yml",
    "runner-build.yml", "system.yml",
}
actual_workflows = {path.name for path in WORKFLOWS.glob("*.yml")}
if actual_workflows != expected_workflows:
    raise SystemExit(f"workflow surface differs: {sorted(actual_workflows)}")

pipeline_files = [
    *sorted(WORKFLOWS.glob("*.yml")),
    ROOT / "scripts/plan.py",
    ROOT / "scripts/channel.py",
    ROOT / "scripts/render-worker.py",
    ROOT / "scripts/runner/run-vm.sh",
    ROOT / "scripts/runner/worker-common.sh",
    *sorted((ROOT / "scripts/runner/stages").glob("*.sh")),
    ROOT / "cmd/fsbuild/main.go",
    ROOT / "broker/src/index.js",
]
pipeline = "\n".join(path.read_text(encoding="utf-8") for path in pipeline_files)
for forbidden in (
    "epoch", "candidate", "quick build", "circleci", "ovh", "ry" + "zen", "cas/v1",
    "freesense-build/v1", "multipartupload", "deleteobject",
):
    if forbidden in pipeline.lower():
        raise SystemExit(f"removed build concept remains: {forbidden}")

for required in (
    "inputs/sha256/", "artifacts/", "repos.manifest.json",
    "state/generations/", "freesense.repositories/v3", "PutIfAbsent",
    "CompareAndSwap", "upload_cutoff = 5G",
):
    if required not in pipeline:
        raise SystemExit(f"immutable storage contract is missing {required!r}")

runner = read("scripts/runner/run-vm.sh")
reusable = read(".github/workflows/runner-build.yml")
for required in (
    "runs-on: [self-hosted, build-runner]", "-smp 16", "-m 32768",
    'qemu-img resize -q "$overlay" 160G', "/dev/kvm", "nuageinit",
    "next_report=$((now + 180))", "cleanup_orphans", "trap cleanup EXIT",
    "trap 'exit 130' INT", "show_diagnostics", "qemu_owns_overlay",
):
    if required not in runner + reusable:
        raise SystemExit(f"build-runner KVM contract is missing {required!r}")

if "workflow_call:" not in reusable:
    raise SystemExit("the only build-runner executor must be a reusable workflow")
for entry in ("system.yml", "packages.yml", "release.yml"):
    text = read(f".github/workflows/{entry}")
    if "uses: ./.github/workflows/runner-build.yml" not in text:
        raise SystemExit(f"{entry} bypasses the single build-runner executor")
if "schedule:" not in read(".github/workflows/system.yml") or "schedule:" not in read(".github/workflows/packages.yml"):
    raise SystemExit("system and optional package checks must be scheduled")

common = read("scripts/runner/worker-common.sh")
system_stage = read("scripts/runner/stages/system.sh")
packages_stage = read("scripts/runner/stages/packages.sh")
for stage in ("system", "packages", "iso"):
    if not (ROOT / f"scripts/runner/stages/{stage}.sh").is_file():
        raise SystemExit(f"missing stage {stage}")
if any((ROOT / f"scripts/runner/stages/{old}.sh").exists() for old in ("base", "repository")):
    raise SystemExit("a removed intermediate stage still exists")
if common.rfind('"${RESULT}/complete.json"') < common.rfind("--immutable"):
    raise SystemExit("completion marker is not the last immutable repository write")
if 'rclone cat "${RESULT}/complete.json"' in common:
    raise SystemExit("guest duplicates the host's authoritative result check")
if "name: Reuse completed immutable result" not in reusable:
    raise SystemExit("host immutable-result reuse check is missing")
if "FREESENSE_DIST_WORLD_ARCHIVE" not in common:
    raise SystemExit("system world is not seeded from pinned base.txz")
source_archive = "create_source_archive"
if "configure_poudriere()" not in common or "NOLINUX=yes" not in common:
    raise SystemExit("runner must explicitly configure Poudriere without Linux compatibility modules")
if "export DO_NOT_SIGN_PKG_REPO=1" not in common:
    raise SystemExit("runner must bypass the legacy bootstrap signer before applying its own repository signature")
for required in (
    "tool_install_status", "FreeSense phase failed:", "${destination}.part",
    "--error-on-no-transfer", "immutable input checksum mismatch",
):
    if required not in common:
        raise SystemExit(f"worker failure contract is missing {required!r}")
for name, stage in (("system", system_stage), ("packages", packages_stage)):
    if source_archive not in stage:
        raise SystemExit(f"{name} does not create the pinned source archive")
    nolinux_config = "configure_poudriere"
    nolinux_bulk = "env NOLINUX=yes ./build.sh --update-pkg-repo"
    if nolinux_config not in stage or nolinux_bulk not in stage:
        raise SystemExit(f"{name} bulk build may load unused Linux compatibility modules")
for required in ("--sort=name", '--mtime="@${source_time}"', "--owner=0", "gzip -n"):
    if required not in common:
        raise SystemExit(f"source archive reproducibility contract is missing {required!r}")

lock = json.loads(read("config/freebsd-16.json"))
if lock.get("schema_version") != "freesense.freebsd-pin/v2" or not lock.get("ready"):
    raise SystemExit("FreeBSD lock is not ready schema v2")
for key in ("freebsd_source", "freebsd_ports", "jail_seed", "worker_image"):
    if key not in lock:
        raise SystemExit(f"FreeBSD lock lacks {key}")
if not re.fullmatch(r"[0-9a-f]{40}", lock["freebsd_source"]["commit"]):
    raise SystemExit("FreeBSD source is not a full Git commit")
if not re.fullmatch(r"[0-9a-f]{40}", lock["freebsd_ports"]["commit"]):
    raise SystemExit("FreeBSD ports is not a full Git commit")
for item in (lock["jail_seed"], lock["worker_image"]):
    if item["object"] != "inputs/sha256/" + item["sha256"]:
        raise SystemExit("pinned input path is not content addressed")

broker = read("broker/src/index.js")
for role in ("coordinator", "artifact-writer", "pin-writer", "channel-writer", "broker-smoke"):
    if role not in broker:
        raise SystemExit(f"broker lacks {role}")
if "GITHUB_REPOSITORY_ID" not in broker or "ref_protected" not in broker:
    raise SystemExit("broker does not bind the recreated repository and protected main")

action_reference = re.compile(r"uses:\s+[^\s]+@([^\s#]+)")
for workflow in sorted(WORKFLOWS.glob("*.yml")):
    for reference in action_reference.findall(workflow.read_text(encoding="utf-8")):
        if not re.fullmatch(r"[0-9a-f]{40}", reference):
            raise SystemExit(f"{workflow.name} has an unpinned action: {reference}")

workflow_lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in WORKFLOWS.glob("*.yml"))
if workflow_lines > 900:
    raise SystemExit(f"workflow surface exceeds simplicity budget: {workflow_lines}")
print(f"Build-runner control plane: valid ({workflow_lines} workflow lines)")
