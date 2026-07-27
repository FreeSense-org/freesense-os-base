from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkerVersionValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("sh") or shutil.which("bash")
        if cls.shell is None and os.name == "nt":
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            if candidate.exists():
                cls.shell = str(candidate)

    def validate(self, version: str, train: str) -> subprocess.CompletedProcess[str]:
        if self.shell is None:
            self.skipTest("POSIX shell is unavailable")
        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        start = source.index("printf '%s\\n' \"${PRODUCT_VERSION}\"")
        end = source.index('if [ "${STAGE}" = iso ]', start)
        fragment = source[start:end]
        return subprocess.run(
            [self.shell, "-eu", "-c", fragment],
            text=True,
            capture_output=True,
            env={**os.environ, "PRODUCT_VERSION": version, "PACKAGE_TRAIN": train},
            check=False,
        )

    def test_accepts_configured_stable_and_development_versions(self) -> None:
        for version, train in (
            ("1.0.5-RELEASE", "1.0"),
            ("1.1.0-DEVELOPMENT", "1.1"),
        ):
            with self.subTest(version=version):
                result = self.validate(version, train)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_mismatched_package_train(self) -> None:
        result = self.validate("1.1.0-DEVELOPMENT", "1.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match package train", result.stderr)

    def test_heavy_ports_use_disk_for_every_poudriere_stage(self) -> None:
        common = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        packages = (ROOT / "scripts/runner/stages/packages.sh").read_text(
            encoding="utf-8"
        )
        for value in (
            'TMPFS_BLACKLIST="rust telegraf"',
            "TMPFS_BLACKLIST_TMPDIR=/usr/local/poudriere/data/cache/tmp",
        ):
            with self.subTest(value=value):
                self.assertIn(value, common)
                self.assertNotIn(value, packages)

    def test_cloud_assembly_uses_fingerprint_repository_trust(self) -> None:
        cloud = (ROOT / "scripts/runner/stages/cloud.sh").read_text(encoding="utf-8")
        self.assertNotIn('signature_type: "pubkey"', cloud)
        self.assertIn('signature_type: "fingerprints"', cloud)
        self.assertIn('fingerprints: "/tmp/assembly-keys"', cloud)
        self.assertIn(
            'fingerprints: "/usr/local/share/FreeSense/keys/pkg"', cloud
        )
        self.assertIn("FreeBSD: { enabled: no }", cloud)
        self.assertIn("FreeBSD-kmods: { enabled: no }", cloud)
        self.assertIn("REPOS_DIR=/tmp/cloud-repos", cloud)
        self.assertIn("REPOS_DIR=/tmp/assembly-repos", cloud)
        self.assertIn("run_in_cloud_chroot()", cloud)
        self.assertIn("mount -t devfs devfs", cloud)
        self.assertIn('chroot "${cloud_chroot_root}"', cloud)
        self.assertIn("pkg-bootstrap.pkg", cloud)
        self.assertNotIn('pkg -r "${root}"', cloud)

    def test_cloud_first_boot_uses_supported_growth_and_sanitization(self) -> None:
        cloud = (ROOT / "scripts/runner/stages/cloud.sh").read_text(encoding="utf-8")
        self.assertIn('growfs_swap_size="0"', cloud)
        self.assertIn('touch "${root}/root/force_growfs"', cloud)
        self.assertNotIn("freesense_growroot", cloud)
        self.assertIn("newfs_msdos -F 32 -c 1 -L FREESENSE ", cloud)
        self.assertNotIn("FREESENSE_EFI", cloud)
        self.assertIn(
            'rm -rf "${root}/var/lib/cloud" "${root}/var/db/cloud-init" \\\n'
            '  "${root}/var/db/entropy"',
            cloud,
        )

    def test_cloud_smoke_uses_writable_uefi_vars_and_effective_ssh_policy(self) -> None:
        smoke = (ROOT / "scripts/runner/smoke-cloud.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("OVMF_VARS_4M.fd", smoke)
        self.assertIn('format=raw,file=${work}/OVMF_VARS-two.fd', smoke)
        self.assertIn("sshd -T", smoke)
        self.assertIn("passwordauthentication no", smoke)

    def test_poudriere_failure_emits_the_exact_error_log(self) -> None:
        if self.shell is None:
            self.skipTest("POSIX shell is unavailable")
        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        start = source.index("run_poudriere_build()")
        end = source.index("\npackage_metadata()", start)
        fragment = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            error_directory = Path(directory, "run", "logs", "errors")
            error_directory.mkdir(parents=True)
            Path(error_directory, "rust.log").write_text(
                "exact rust extraction failure\n", encoding="utf-8"
            )
            result = subprocess.run(
                [
                    self.shell,
                    "-eu",
                    "-c",
                    fragment + "\nrun_poudriere_build sh -c 'exit 7'",
                ],
                text=True,
                capture_output=True,
                env={**os.environ, "POUDRIERE_LOGS_ROOT": directory},
                check=False,
            )
        self.assertEqual(result.returncode, 7)
        self.assertIn("exact rust extraction failure", result.stderr)
        self.assertIn("failure diagnostics end status=7", result.stderr)
