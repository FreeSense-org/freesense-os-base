from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
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

    def test_arm64_poudriere_jail_builds_native_xtools_from_pinned_source(self) -> None:
        common = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        create_jail = common[common.index("create_jail() {") :]
        arm64_case = create_jail.index(
            'if [ "${FREEBSD_TARGET_ARCH}" = aarch64 ]; then'
        )
        source_path = create_jail.index(
            "poudriere_source=/root/freesense-src/tmp/FreeBSD-src", arm64_case
        )
        jail_command = create_jail.index('poudriere jail -c', source_path)
        self.assertLess(arm64_case, source_path)
        self.assertLess(source_path, jail_command)
        self.assertIn(
            'clone_exact https://github.com/freebsd/freebsd-src.git', create_jail
        )
        self.assertIn('"${poudriere_source}" "${FREEBSD_SHA}"', create_jail)
        self.assertIn(
            '-b -J 4 -v 16.0-CURRENT -m "src=${poudriere_source}"', create_jail
        )
        self.assertNotIn("poudriere_cross_args=-X", create_jail)
        self.assertIn('-v 16.0-CURRENT -m tar=/root/jail-base.txz', create_jail)
        self.assertIn(
            'qemu-aarch64-static -L "${jail_root}" "${probe}"', create_jail
        )
        self.assertNotIn(
            '    "${probe}" freesense-aarch64-probe', create_jail
        )

    def test_arm64_poudriere_closes_only_directory_fds_before_jexec(self) -> None:
        common = (ROOT / "scripts/runner/worker-common.sh").read_text(
            encoding="utf-8"
        )
        installer = common[
            common.index("install_poudriere_jexec_launcher() {") :
            common.index("\nconfigure_poudriere() {")
        ]
        configure = common[
            common.index("configure_poudriere() {") :
            common.index("\nrun_poudriere_build() {")
        ]
        self.assertIn("fstat(fd, &sb) == 0 && S_ISDIR(sb.st_mode)", installer)
        self.assertIn("for (fd = 3; fd < maxfd; fd++)", installer)
        self.assertIn('execvp(argv[1], &argv[1])', installer)
        self.assertIn('cc -O2 -Wall -Wextra -o "${launcher}"', installer)
        arm64_case = configure.index(
            'if [ "${FREEBSD_TARGET_ARCH}" = aarch64 ]; then'
        )
        install = configure.index("install_poudriere_jexec_launcher", arm64_case)
        prefix = configure.index(
            "JEXEC_SETSID=/usr/local/libexec/freesense-close-dir-fds",
            install,
        )
        arm64_end = configure.index("\n  fi", prefix)
        self.assertLess(arm64_case, install)
        self.assertLess(install, prefix)
        self.assertLess(prefix, arm64_end)
        self.assertNotIn("mv /usr/sbin/jexec", common)

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

    def test_cloud_assembly_installs_and_validates_platform_packages(self) -> None:
        cloud = (ROOT / "scripts/runner/stages/cloud.sh").read_text(encoding="utf-8")
        for package in (
            "FreeSense-base",
            "FreeSense-kernel-FreeSense",
            "FreeSense-rc",
            "FreeSense-repoc",
            "FreeSense-system",
        ):
            with self.subTest(package=package):
                self.assertIn(package, cloud)
        self.assertIn('${root}/boot/kernel/kernel"', cloud)
        self.assertIn('${root}/boot/kernel/kernel.gz"', cloud)
        self.assertIn("for kernel_module in zfs.ko opensolaris.ko", cloud)
        self.assertIn("for boot_hook in FreeSense-rc FreeSense-rc.shutdown", cloud)
        self.assertIn("install -f -y -r FreeSenseAssembly", cloud)
        self.assertIn('package_owner=$(pkg which "/etc/${boot_hook}")', cloud)
        self.assertIn("*FreeSense-system-*)", cloud)
        self.assertIn('[ -s "${root}/etc/${boot_hook}" ]', cloud)
        self.assertIn('ln -sf FreeSense-rc "${root}/etc/pfSense-rc"', cloud)
        self.assertIn(
            'ln -sf FreeSense-rc.shutdown "${root}/etc/pfSense-rc.shutdown"',
            cloud,
        )

    def test_system_kernel_validation_accepts_compressed_payload(self) -> None:
        system = (ROOT / "scripts/runner/stages/system.sh").read_text(
            encoding="utf-8"
        )
        debug_case = system.index("FreeSense-kernel-debug-*")
        normal_case = system.index("FreeSense-kernel-*)", debug_case)
        self.assertLess(debug_case, normal_case)
        self.assertIn("multiple built kernel packages found", system)
        self.assertIn("boot/kernel/kernel(\\.gz)?$", system)
        self.assertIn('case "${kernel_member}" in', system)
        self.assertIn("gzip -dc /tmp/freesense-built-kernel.gz", system)
        self.assertIn("ELF 64-bit.*ARM aarch64", system)

    def test_cloud_first_boot_uses_supported_growth_and_sanitization(self) -> None:
        cloud = (ROOT / "scripts/runner/stages/cloud.sh").read_text(encoding="utf-8")
        self.assertIn('growfs_enable="YES"', cloud)
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

    def test_cloud_first_boot_keeps_kernel_on_the_serial_console(self) -> None:
        cloud = (ROOT / "scripts/runner/stages/cloud.sh").read_text(encoding="utf-8")
        for value in (
            'cat >"${root}/boot.config"',
            "-S115200 -Dh",
            'boot_multicons="YES"',
            'boot_serial="YES"',
            'comconsole_speed="115200"',
        ):
            with self.subTest(value=value):
                self.assertIn(value, cloud)
        self.assertNotIn('console="comconsole,vidconsole"', cloud)

    def test_cloud_smoke_uses_writable_uefi_vars_and_effective_ssh_policy(self) -> None:
        smoke = (ROOT / "scripts/runner/smoke-cloud.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("OVMF_VARS_4M.fd", smoke)
        self.assertIn('format=raw,file=${work}/OVMF_VARS-two.fd', smoke)
        self.assertIn("prepare_qga()", smoke)
        self.assertIn("shutdown_guest()", smoke)
        self.assertEqual(smoke.count("shutdown_guest 10022"), 2)
        self.assertIn('shutdown_guest 10023 "two-NIC UEFI boot"', smoke)
        self.assertIn("/sbin/shutdown -p now", smoke)
        self.assertIn("-device virtio-serial-pci", smoke)
        self.assertIn("name=org.qemu.guest_agent.0", smoke)
        self.assertEqual(smoke.count('"${qga_args[@]}"'), 2)
        self.assertIn("sshd -T", smoke)
        self.assertIn("passwordauthentication no", smoke)
        self.assertIn("ssh_args=(ssh -q ", smoke)
        self.assertNotIn("ssh_args=(-q ", smoke)
        self.assertIn("'status: done'|'status: degraded'", smoke)
        self.assertIn("virtual_size_gib * 95 * 1024 * 1024 / 100", smoke)
        self.assertIn("virtual_size * 95 / 100", smoke)
        self.assertNotIn(
            'df -k / | awk \'NR == 2 {print \\$2}\')" -gt',
            smoke,
        )
        self.assertNotIn(
            'zpool list -Hp -o size FreeSense)" -gt ${virtual_size}',
            smoke,
        )
        self.assertIn("diagnose_ssh_timeout()", smoke)
        self.assertIn("forwarded TCP/22 is reachable", smoke)
        self.assertIn("verbose public-key attempt for ${user}", smoke)
        self.assertIn("for user in admin root", smoke)
        self.assertIn("IdentitiesOnly=yes", smoke)
        self.assertIn("qga_exec()", smoke)
        self.assertIn("guest-exec-status", smoke)
        self.assertIn("client.settimeout(45)", smoke)
        self.assertIn("package and cloud-init versions", smoke)
        self.assertIn("userdata sources and FreeSense state", smoke)
        self.assertIn("/usr/bin/sockstat -46 -l", smoke)
        self.assertIn("/sbin/pfctl -vvsr", smoke)
        self.assertIn("service qemu-guest-agent status", smoke)
        self.assertIn("cloud-init query userdata", smoke)
        self.assertIn("FreeSense-cloud-init", smoke)
        self.assertIn("/var/lib/cloud/instance/user-data.txt", smoke)
        self.assertIn("/var/db/freesense-cloud-init/instance.json", smoke)
        self.assertIn("/root/.ssh/authorized_keys", smoke)
        self.assertIn("--failure-dir", smoke)
        self.assertIn("package_failure_artifacts()", smoke)
        self.assertIn("disk-post-boot.qcow2", smoke)
        self.assertIn("images/published-", smoke)
        self.assertNotIn('cp -f "${work}/id" ', smoke)

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

    def test_repository_verifier_uses_portable_pkg_checksum(self) -> None:
        shell = shutil.which("bash") or self.shell
        if shell is None or shutil.which("jq") is None:
            self.skipTest("pipefail-capable shell or jq is unavailable")
        checksum = "2$" + "y" * 103

        def shell_path(path: Path) -> str:
            value = path.as_posix()
            if os.name == "nt" and len(value) >= 3 and value[1:3] == ":/":
                return f"/{value[0].lower()}/{value[3:]}"
            return value

        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        start = source.index("verify_repository()")
        end = source.index("\nfetch_repository()", start)
        fragment = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            package_directory = repository / "All"
            package_directory.mkdir(parents=True)
            package = package_directory / "fixture-1.pkg"
            package.write_text("verified payload\n", encoding="utf-8")
            catalog = json.dumps({
                "name": "fixture",
                "version": "1",
                "origin": "devel/fixture",
                "repopath": "All/fixture-1.pkg",
                "sum": checksum,
            }).encode() + b"\n"
            with tarfile.open(repository / "packagesite.pkg", "w") as archive:
                for name, content in (
                    ("packagesite.yaml", catalog),
                    ("packagesite.yaml.sig", b"test signature"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            trusted_key = root / "repo.pub"
            trusted_key.write_text("test key", encoding="utf-8")
            fragment = fragment.replace(
                "/root/sign/repo.pub", shell_path(trusted_key)
            )
            mocks = """
sha256() { printf '%064d\\n' 0; }
openssl() { return 0; }
pkg() {
  test "$1" = checksum && test "$2" = -q && test "$3" = -c
  test "$4" = "$PKG_TEST_CHECKSUM"
  test "$(cat "$5")" = 'verified payload'
}
"""
            command = [
                shell,
                "-eu",
                "-c",
                mocks + fragment + '\nverify_repository "$REPOSITORY"',
            ]
            environment = {
                **os.environ,
                "PKG_TEST_CHECKSUM": checksum,
                "REPOSITORY": shell_path(repository),
            }
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            package.write_text("tampered payload\n", encoding="utf-8")
            tampered = subprocess.run(
                command,
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("checksum does not match", tampered.stderr)
