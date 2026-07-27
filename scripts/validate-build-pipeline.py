#!/usr/bin/env python3
"""Check the few trust boundaries that must remain true across the build."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


expected_workflows = {
    "broker.yml", "ci.yml", "packages.yml", "pin.yml", "release.yml",
    "retention.yml", "runner-build.yml", "stable.yml", "system.yml",
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
for name in ("system.yml", "packages.yml", "release.yml", "stable.yml"):
    require("uses: ./.github/workflows/runner-build.yml" in read(f".github/workflows/{name}"),
            f"{name} bypasses the reusable KVM executor")
require("schedule:" in read(".github/workflows/system.yml"),
        "the daily System check is not scheduled")
packages_workflow = read(".github/workflows/packages.yml")
require("workflow_run:" in packages_workflow and "workflows: [System]" in packages_workflow,
        "optional packages are not chained to System")
require("schedule:" not in packages_workflow,
        "optional packages retain a racing fixed schedule")
require("--built-against-system" in packages_workflow,
        "optional package publication omits its immutable build System")
require('cron: "0 6 * * *"' in read(".github/workflows/system.yml"),
        "the daily System check is not fixed at 06:00 UTC")
retention_workflow = read(".github/workflows/retention.yml")
for value in ('cron: "30 4 * * *"', "scripts/r2_retention.py",
              "--keep-devel 4", "--orphan-grace-hours 168",
              "--completed-grace-hours 0", "--keep-smoke 1",
              "--role retention-build-reader",
              "--role retention-download-reader",
              "--role retention-build-deleter",
              "--role retention-download-deleter",
              "--role retention-state-writer",
              "v1/state/retention.json", "two-run-confirmation"):
    require(value in retention_workflow,
            f"daily guarded R2 retention is missing {value!r}")
retention_source = read("scripts/r2_retention.py")
for value in ("minimum_interval: timedelta = timedelta(hours=20)",
              "retention deletion exceeds the per-run safety cap",
              '"/stable/" in key', "superseded broker smoke marker",
              'for entry in devel["iso"] + devel["cloud"]',
              'inputs.get("packages")',
              "retained legacy Development ISO has no Packages fingerprint"):
    require(value in retention_source,
            f"R2 retention safety boundary is missing {value!r}")
stable_workflow = read(".github/workflows/stable.yml")
require("schedule:" not in stable_workflow and "channel seal-stable" in stable_workflow and
        '--release "${{ inputs.release }}"' in stable_workflow,
        "Stable train is not an explicit checked patch publication")
for value in ("--packages-built-against-system", "packages-complete.json",
              ".inputs.built_against_system // .inputs.system"):
    require(value in stable_workflow,
            f"stable package reuse provenance is missing {value!r}")
for value in ("queue_development", "needs: publish-download",
              "gh workflow run system.yml", "actions: write"):
    require(value in stable_workflow,
            f"stable-to-development ordered handoff is missing {value!r}")
release_workflow = read(".github/workflows/release.yml")
require("retry-stable-bundle" in release_workflow and
        "recovery operation requires the configured sealed Stable train" in release_workflow,
        "the Stable bundle recovery entry point is missing or unguarded")
for value in ("Require an upstream publication and complete release pair",
              'select(.name == "publish")', "packages_verified",
              "needs.release_gate.outputs.ready == 'true'"):
    require(value in release_workflow,
            f"automatic ISO publication gating is missing {value!r}")
for value in ("Reserve immutable release generation", "system_generation",
              "--fingerprint \"${{ steps.plan.outputs.bundle }}\"",
              "--proposed \"${GITHUB_RUN_NUMBER}\""):
    require(value in release_workflow,
            f"independent ISO release generation is missing {value!r}")
require(release_workflow.count(
            "if: always() && needs.iso-plan.result == 'success'") == 3,
        "manual bundle artifact jobs do not survive the intentionally skipped release gate")
for value in ("needs: [iso-plan, iso]", "needs: [iso-plan, cloud-ufs]"):
    require(value in release_workflow,
            f"release KVM jobs are not serialized by {value!r}")
require('expected_system = values["built_against_system"]' in read("scripts/channel.py"),
        "rebound optional packages are not verified against their build System")
for value in ("sync-downloads", "scripts/migrate_downloads.py",
              "scripts/publish_iso.sh", "vars.R2_DOWNLOAD_BUCKET",
              "--role download-writer",
              "/v1/releases/${{ needs.iso-plan.outputs.channel }}.json"):
    require(value in release_workflow,
            f"independent download publication is missing {value!r}")
require("/v1/releases/stable.json" in stable_workflow,
        "stable publication does not write its independent download document")
for value in ("--system-ports", "--packages", "GITHUB_TOKEN",
              'needs.iso-plan.outputs.os_base_sha'):
    require(value in release_workflow,
            f"development release changelog provenance is missing {value!r}")
for value in ("--system-ports", "--packages", "GITHUB_TOKEN",
              'needs.plan.outputs.os_base_sha'):
    require(value in stable_workflow,
            f"stable release changelog provenance is missing {value!r}")
release_publisher = read("scripts/publish_download.py")
for value in ("github_compare", '"changes": build_changes', "FreeSense-org/freesense-packages"):
    require(value in release_publisher,
            f"canonical release changelog generation is missing {value!r}")
require("s3://${R2_BUCKET}/v1/releases.json" not in release_workflow + stable_workflow,
        "release workflows still overwrite the combined legacy download index")
broker_source = read("broker/src/index.js")
for value in ("releases/stable.json", "releases/devel.json"):
    require(value in broker_source,
            f"channel-writer credentials do not authorize {value!r}")
require("`${R2_PREFIX}/releases.json`" not in broker_source,
        "channel-writer credentials still authorize the legacy combined download index")
for value in ('"download-writer"', 'bucket: "downloads"', "R2_DOWNLOAD_BUCKET"):
    require(value in broker_source,
            f"downloads-bucket credential boundary is missing {value!r}")
publisher = read("scripts/publish_iso.sh")
for value in ("https://downloads.freesense.org/v1/releases/", "sha256sum --check",
              "refusing to overwrite a conflicting downloads object"):
    require(value in publisher, f"ISO publisher is missing {value!r}")
require("freebsd_pin_id" in reusable and "product_version" in reusable,
        "reusable builds omit the release or FreeBSD pin identity")
for value in ("packages_id", "PACKAGES_ID", '--packages-id "${PACKAGES_ID}"',
              '--packages "${PACKAGES_ID}"'):
    require(value in reusable,
            f"reusable ISO build omits the Packages identity binding {value!r}")
require("system_generation" in reusable and "SYSTEM_GENERATION" in
        read("scripts/runner/worker-common.sh") + read("scripts/runner/stages/iso.sh"),
        "ISO builds do not separate release and signed System generations")

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
require({path.stem for path in stage_dir.glob("*.sh")} == {"system", "packages", "iso", "cloud"},
        "stage surface differs from system/packages/iso/cloud")
system_stage = read("scripts/runner/stages/system.sh")
packages_stage = read("scripts/runner/stages/packages.sh")
iso_stage = read("scripts/runner/stages/iso.sh")
cloud_stage = read("scripts/runner/stages/cloud.sh")
require("-S115200 -Dh" in iso_stage and not re.search(
            r'(?m)^console="comconsole,vidconsole"$', iso_stage),
        "ISO console selection is not firmware-aware dual-console mode")
for value in ("recover_configxml.sh", "import_foreign_config.sh",
              "config_import_pkgmap.map", "installer recovery helper was not installed",
              "installer foreign-config package map was not installed"):
    require(value in iso_stage,
            f"exact ISO assembly omits installer recovery payload {value!r}")
for value in ('TMPFS_BLACKLIST="rust telegraf"',
              "TMPFS_BLACKLIST_TMPDIR=/usr/local/poudriere/data/cache/tmp"):
    require(value in common and value not in packages_stage,
            f"shared heavy-package disk workdir contract is missing {value!r}")
for value in ("run_poudriere_build()", "logs/errors/*.log",
              "FreeSense Poudriere failure diagnostics begin"):
    require(value in common,
            f"Poudriere failure diagnostics are missing {value!r}")
require("run_poudriere_build env NOLINUX=yes" in system_stage and
        "run_poudriere_build env NOLINUX=yes" in packages_stage,
        "System and Optional Packages builds do not preserve failed port logs")
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
cloud_payload = cloud_stage.rfind('upload_immutable /root/')
cloud_complete = cloud_stage.rfind('upload_immutable /tmp/complete.json')
require(cloud_payload >= 0 and cloud_complete > cloud_payload,
        "cloud completion marker is not uploaded last")
assembly_common = read("scripts/runner/assembly-common.sh")
require("repos.manifest.json" not in iso_stage + assembly_common and
        "CHANNEL_PAYLOAD_B64" in assembly_common and "CHANNEL_SIGNATURE_B64" in assembly_common,
        "ISO does not consume the exact selected signed channel payload")
planner_source = read("scripts/plan.py")
require('"packages": current_packages_fingerprint' in planner_source and
        '"packages_fingerprint": (' in planner_source and
        '"channel_payload": channel_payload_sha256' in planner_source,
        "ISO identity omits the optional package pair or signed channel payload")
for value in ('"bundle": bundle', '"kind": "cloud"', '"cloud_policy": policy["cloud"]'):
    require(value in planner_source, f"bundle/cloud identity is missing {value!r}")
for value in ("freesense.cloud-image/v1", "qemu-img convert",
              "CLOUD_VIRTUAL_SIZE_GIB", "CLOUD_FILESYSTEM",
              "FreeSense/ROOT/default", "gptzfsboot", "force_growfs",
              'growfs_swap_size="0"',
              "FreeSense-base", "FreeSense-kernel-FreeSense", "FreeSense-repoc",
              "FreeSense-cloud-init", "qemu-guest-agent", "prepare_release_inputs",
              '${root}/boot/kernel/kernel', '${root}/boot/kernel/kernel.gz',
              "for kernel_module in zfs.ko opensolaris.ko",
              'cat >"${root}/boot.config"', "-S115200 -Dh",
              'boot_multicons="YES"', 'boot_serial="YES"',
              'comconsole_speed="115200"'):
    require(value in cloud_stage, f"cloud image stage is missing {value!r}")
require('console="comconsole,vidconsole"' not in cloud_stage,
        "cloud console hard-codes the BIOS-only video console")
for value in ('signature_type: "fingerprints"',
              'fingerprints: "/tmp/assembly-keys"',
              'fingerprints: "/usr/local/share/FreeSense/keys/pkg"',
              "FreeBSD: { enabled: no }",
              "FreeBSD-kmods: { enabled: no }",
              "REPOS_DIR=/tmp/cloud-repos",
              "REPOS_DIR=/tmp/assembly-repos",
              "run_in_cloud_chroot()",
              "mount -t devfs devfs",
              'chroot "${cloud_chroot_root}"',
              "pkg-bootstrap.pkg",
              'PKG_INSTALL_EPOCH="${SOURCE_DATE_EPOCH}"'):
    require(value in cloud_stage,
            f"cloud package trust boundary is missing {value!r}")
require('signature_type: "pubkey"' not in cloud_stage,
        "cloud assembly uses an incompatible repository trust mode")
require('pkg -r "${root}"' not in cloud_stage,
        "cloud package installation bypasses the image-root chroot")
require('schema_version:"freesense.iso/v2"' in iso_stage and
        "packages:$packages" in iso_stage,
        "ISO completion markers omit the exact Packages artifact")
for value in ("FREESENSE_INSTALLER_PATCH_B64", "git apply --check",
              "FREESENSE_ASSEMBLY_INSTALLER_OVERLAY", "startbsdinstall",
              "copy_configxml_from_usb", "fix_fstab"):
    require(value in iso_stage or value in read("scripts/render-worker.py"),
            f"ISO installer payload contract is missing {value!r}")
installer_patch = read("patches/0005-installer.patch")
patch_check = subprocess.run(
    ["git", "apply", "--stat", "patches/0005-installer.patch"],
    cwd=ROOT,
    text=True,
    capture_output=True,
)
require(
    patch_check.returncode == 0,
    "installer patch is structurally invalid: " + patch_check.stderr.strip(),
)
require("mdconfig -a -u 3 -s 32m" in installer_patch and
        "mdconfig -a -u 3 -s 8m" not in installer_patch,
        "installer /etc memory disk is too small for configuration recovery")
require("OPTIONAL_PACKAGE_CONFIG_PATHS" in planner_source and
        '"package_build_config": package_build_config' in planner_source,
        "optional package identity omits its source build configuration")
stable_planner_source = read("scripts/stable_plan.py")
require("OPTIONAL_PACKAGE_CONFIG_PATHS" in stable_planner_source and
        '"package_build_config": package_build_config' in stable_planner_source,
        "stable optional package identity omits its pinned source build configuration")
require('INSTALLED_VERSION="${INSTALLED_VERSION%%-*}"' in iso_stage and
        "v1.0 repoc compatibility overlay" in iso_stage,
        "the sealed 1.0 System repoc compatibility overlay is missing")
require('printf \'%s\\n\' "${PRODUCT_VERSION}" >src/etc/version' in common and
        '"${_repoc_root}/etc/version"' in iso_stage,
        "checked release versions are not stamped into source and ISO roots")
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
abi_major = int(policy["abi"].split(":")[1])
osversion = lock.get("freebsd_source", {}).get("osversion")
require(isinstance(osversion, int) and
        abi_major * 100000 <= osversion < (abi_major + 1) * 100000,
        "FreeBSD source is not pinned to an exact OSVERSION matching the ABI")
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
for role in ("coordinator", "artifact-writer", "pin-writer", "channel-writer",
             "download-writer", "retention-build-reader",
             "retention-download-reader", "retention-build-deleter",
             "retention-download-deleter", "retention-state-writer",
             "broker-smoke", "download-smoke"):
    require(role in broker, f"credential broker lacks {role}")
require("GITHUB_REPOSITORY_ID" in broker and "ref_protected" in broker,
        "credential broker is not bound to protected main")

print("Build control-plane integrity boundaries: valid")
