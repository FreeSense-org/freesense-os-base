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
    ROOT / "scripts/select_ports_pin.py",
    ROOT / "scripts/verify-release.sh",
    ROOT / "scripts/render-worker.py",
    ROOT / "scripts/runner/run-vm.sh",
    ROOT / "scripts/runner/worker-common.sh",
    *sorted((ROOT / "scripts/runner/stages").glob("*.sh")),
    ROOT / "cmd/fsbuild/main.go",
    ROOT / "broker/src/index.js",
]
pipeline = "\n".join(path.read_text(encoding="utf-8") for path in pipeline_files)
for forbidden in (
    "epoch_id", "epoch-id", "epochs/", "candidate", "quick build", "circleci", "ovh", "ry" + "zen", "cas/v1",
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
probe = read("scripts/runner/probe.sh")
policy = json.loads(read("config/build-policy.json"))
if policy.get("runner") != {"vcpus": 12, "memory_mib": 32768, "disk_gib": 160}:
    raise SystemExit("build policy and the dedicated runner resource contract differ")
for required in (
    "runs-on: [self-hosted, build-runner]", "-smp 12", "-m 32768",
    'qemu-img resize -q "$overlay" 160G', "/dev/kvm", "nuageinit",
    "next_report=$((now + 180))", "cleanup_orphans", "trap cleanup EXIT",
    "trap 'exit 130' INT", "show_diagnostics", "qemu_owns_overlay",
):
    if required not in runner + reusable:
        raise SystemExit(f"build-runner KVM contract is missing {required!r}")
if '"$cpus" -eq 12' not in probe:
    raise SystemExit("the pinned-image probe does not enforce the 12-vCPU guest contract")

if "workflow_call:" not in reusable:
    raise SystemExit("the only build-runner executor must be a reusable workflow")
for entry in ("system.yml", "packages.yml", "release.yml"):
    text = read(f".github/workflows/{entry}")
    if "uses: ./.github/workflows/runner-build.yml" not in text:
        raise SystemExit(f"{entry} bypasses the single build-runner executor")
system_workflow = read(".github/workflows/system.yml")
packages_workflow = read(".github/workflows/packages.yml")
if "schedule:" not in system_workflow:
    raise SystemExit("the daily System check is not scheduled")
if "workflow_run:" not in packages_workflow or "workflows: [System]" not in packages_workflow:
    raise SystemExit("optional packages are not chained to successful System completion")
if "schedule:" in packages_workflow:
    raise SystemExit("optional packages retain a racing fixed schedule")
pin_workflow = read(".github/workflows/pin.yml")
for required in (
    "scripts/select_ports_pin.py",
    "repos/freebsd/freebsd-ports/commits/${ports_sha}",
):
    if required not in pin_workflow:
        raise SystemExit(f"FreeBSD ports pin is missing {required!r}")

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
for required in ("store.GetArtifact", "store.HeadArtifact", "--platform-id", '--generation "${GENERATION}"'):
    if required not in read("cmd/fsbuild/main.go") + reusable:
        raise SystemExit(f"artifact reuse closure is missing {required!r}")
if "FREESENSE_DIST_WORLD_ARCHIVE" not in common:
    raise SystemExit("system world is not seeded from pinned base.txz")
source_archive = "create_source_archive"
for required in (
    "configure_poudriere()", "NO_ZFS=yes", "PARALLEL_JOBS=3",
    "PREPARE_PARALLEL_JOBS=3", "ALLOW_MAKE_JOBS=yes",
    "USE_TMPFS=wrkdir", "TMPFS_LIMIT=4",
    "ATOMIC_PACKAGE_REPOSITORY=yes", "COMMIT_PACKAGES_ON_FAILURE=no",
    "NOLINUX=yes", "PKG_REPRODUCIBLE=yes",
    "PRESERVE_TIMESTAMP=yes", "BUILDER_HOSTNAME=freesense-builder",
    "SOURCE_DATE_EPOCH=${FREESENSE_SOURCE_COMMIT_TIME}",
    "FREESENSE_MAKE_JOBS_NUMBER_LIMIT=4",
    "FREESENSE_USE_PACKAGE_FETCH=1",
    'PACKAGE_FETCH_URL=pkg+https://pkg.FreeBSD.org/\\${ABI}',
):
    if required not in common:
        raise SystemExit(f"runner reproducibility contract is missing {required!r}")
if "export DO_NOT_SIGN_PKG_REPO=1" not in common:
    raise SystemExit("runner must bypass the legacy bootstrap signer before applying its own repository signature")
for required in (
    "tool_install_status", "FreeSense phase failed:", "${destination}.part",
    "--error-on-no-transfer", "immutable input checksum mismatch", "/root/sign/repo.pub",
    "DATESTRING", "BUILTDATESTRING", "trusted package fingerprint",
    "verify_repository()", "packagesite.yaml.sig", "invalid signed package catalog record",
    "repository packages do not match the signed catalog",
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
    if stage.find("configure_poudriere") > stage.find("create_jail"):
        raise SystemExit(f"{name} creates its Poudriere jail before applying the checked configuration")
if "optional-closure-check" not in packages_stage or "'%dn|%dv'" not in packages_stage:
    raise SystemExit("optional repository dependency closure is not validated")
for required in (
    "package_metadata()", "inventory_package()", "merge_package()",
    "seed_poudriere_repository()", "poudriere_latest_repository()",
):
    if required not in common:
        raise SystemExit(f"repository composition helper is missing {required!r}")
if "conflicting package name or filename" not in common or "find /usr/local/poudriere/data/packages" in system_stage + packages_stage:
    raise SystemExit("repository composition can overwrite packages or select an ambiguous Poudriere result")
seed_call = "seed_poudriere_repository /root/system-repo"
if packages_stage.count(seed_call) != 1:
    raise SystemExit("optional packages must seed exactly one System repository")
seed_order = (
    packages_stage.find("create_jail"),
    packages_stage.find("--update-poudriere-ports"),
    packages_stage.find(seed_call),
    packages_stage.find("--update-pkg-repo"),
)
if -1 in seed_order or tuple(sorted(seed_order)) != seed_order:
    raise SystemExit("System repository seed is outside the Poudriere build window")
for legacy in ("cache=", "ln -sfn .real_system"):
    if legacy in packages_stage:
        raise SystemExit("optional package stage retains its incomplete inline seed")
for required in ("Latest/pkg.pkg", ".jailversion", "pkg repo"):
    if required not in common:
        raise SystemExit(f"Poudriere System seed is missing {required!r}")
for duplicate in (".real_system", ".latest.new", '.latest/${member_name}'):
    if duplicate in common:
        raise SystemExit("runner duplicates Poudriere's atomic repository conversion")
for required in ("--sort=name", '--mtime="@${source_time}"', "--owner=0", "gzip -n"):
    if required not in common:
        raise SystemExit(f"source archive reproducibility contract is missing {required!r}")
iso_stage = read("scripts/runner/stages/iso.sh")
if common.count("--immutable --checksum --multi-thread-streams 0") != 1:
    raise SystemExit("immutable publication must use single-part checksum uploads")
if common.count("upload_immutable \"") != 2 or iso_stage.count("upload_immutable ") != 2:
    raise SystemExit("every artifact publication must use the immutable upload helper")

planner = read("scripts/plan.py")
channel_reader = read("scripts/channel.py")
channel_control = read("internal/control/control.go")
for required in ("FreeSense-build/1", "error.code == 404", "signing_public_key_sha256", "--system-closure"):
    if required not in planner:
        raise SystemExit(f"planner channel/trust contract is missing {required!r}")
for required in ("payload_sha256", "artifact_signing_public_key_sha256", "system_fingerprint"):
    if required not in channel_reader:
        raise SystemExit(f"signed channel reader is missing {required!r}")
for required in ("SystemFingerprint", "packages publication is not bound", "channel.Packages = nil"):
    if required not in channel_control:
        raise SystemExit(f"channel coherence contract is missing {required!r}")
for required in ("group: freesense-kvm-host", "name: Verify required System result"):
    if required not in reusable:
        raise SystemExit(f"single-runner dependency contract is missing {required!r}")
if "repos.manifest.json" in iso_stage:
    raise SystemExit("ISO worker refetches the mutable channel alias")
if 'fetch_input "${JAIL_OBJECT}" /root/jail-base.txz' not in iso_stage:
    raise SystemExit("ISO worker does not fetch its pinned FreeBSD world seed")
for required in ("CHANNEL_PAYLOAD_B64", "CHANNEL_SIGNATURE_B64"):
    if required not in iso_stage or required not in reusable:
        raise SystemExit(f"ISO exact channel closure is missing {required!r}")
release = read(".github/workflows/release.yml")
if ".promote.out" not in release or 'rm -f "$output"' not in release:
    raise SystemExit("verification outputs can contaminate promotion reads")

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
print(f"Build-runner control plane: valid ({workflow_lines} workflow lines)")
