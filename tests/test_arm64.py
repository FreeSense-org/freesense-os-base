"""Portable generic arm64 installer checks using synthetic GPT, PE and ELF headers."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import hashlib
import io
import json
import lzma
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/installer"))
import arm64
import build_iso


def arm_elf(machine=183):
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", header, 16, 2, machine)
    return header


class Arm64Installer(unittest.TestCase):
    def test_fallback_application(self):
        data = bytearray(512)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 60, 128)
        data[128:132] = b"PE\0\0"
        struct.pack_into("<HH", data, 132, 0xaa64, 2)
        struct.pack_into("<HH", data, 148, 240, 0)
        struct.pack_into("<H", data, 152, 0x20b)
        struct.pack_into("<H", data, 220, 10)
        arm64.require_efi_application(data)
        for offset, value in ((0, 0), (60, 65535), (128, 0), (132, 0x8664), (134, 0), (148, 20),
                              (148, 65535), (152, 0x10b), (220, 11)):
            bad = data.copy()
            struct.pack_into("<H", bad, offset, value)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                arm64.require_efi_application(bad)

    def test_mismatched_machine_and_board_profiles(self):
        for arch, fw in (("arm64", "bios"), ("arm64-rpi4b", "uefi"), ("arm64-rpi5-d0", "uefi"), ("aarch64", "bios")):
            with self.subTest(arch=arch), self.assertRaisesRegex(ValueError, "unsupported"):
                build_iso.assemble(Path("."), None, fw, arch)
        for data in (arm_elf(62), bytes(64), arm_elf()[:63]):
            with self.assertRaises(ValueError):
                arm64.require_elf(data)
        arm64.require_elf(arm_elf())

    def test_normal_arm64_entry_rejects_foreign_disk_before_authoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            base = work / arm64.UPSTREAM
            base.write_bytes(bytes(20 * 2048 + 2097152))
            raw = bytes(140 * 512)
            image = work / "payload.raw.xz"
            image.write_bytes(lzma.compress(raw))
            info = {"raw_bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "xz_bytes": image.stat().st_size, "xz_sha256": build_iso.sha256(image)}
            (work / "payload.json").write_text(json.dumps(info))
            with (patch.object(arm64, "BASE_SHA", build_iso.sha256(base)),
                  patch.object(arm64, "ESP_HASH", hashlib.sha256(bytes(2097152)).hexdigest()),
                  patch.object(arm64, "check_esp") as checked, patch.object(subprocess, "run") as tool,
                  self.assertRaisesRegex(ValueError, "GPT")):
                build_iso.assemble(work, None, arch="arm64")
            checked.assert_called_once()
            tool.assert_not_called()
            self.assertFalse(any(work.glob("FreeSense-*.iso")))

    def test_selected_arm64_profile_reaches_the_uefi_authoring_entry(self):
        class AuthoringCalled(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            base = work / arm64.UPSTREAM
            base.write_bytes(bytes(20 * 2048 + 2097152))
            (work / "payload.json").write_text(json.dumps({"raw_bytes": 512, "raw_sha256": "a" * 64, "xz_sha256": "b" * 64}))

            def observe(argv, **kwargs):
                if argv[0] == "mkfs.fat":
                    return
                self.assertEqual(argv[:3], ["xorriso", "-indev", arm64.UPSTREAM])
                stage = (work / argv[argv.index("-outdev") + 1]).parent
                self.assertEqual((stage / "freesense/architecture").read_text(), "arm64\n")
                self.assertEqual((stage / "freesense/firmware").read_text(), "UEFI\n")
                self.assertIn("efi_path=/boot/freesense-efi.img", argv)
                self.assertIn("appended_part_as=gpt", argv)
                raise AuthoringCalled

            with (patch.object(arm64, "BASE_SHA", build_iso.sha256(base)),
                  patch.object(arm64, "ESP_HASH", hashlib.sha256(bytes(2097152)).hexdigest()),
                  patch.object(arm64, "check_esp") as checked_optical, patch.object(arm64, "check_installed") as checked_disk,
                  patch.object(build_iso, "validate_payload") as checked_digest,
                  patch.object(subprocess, "run", side_effect=observe), self.assertRaises(AuthoringCalled)):
                build_iso.assemble(work, None, arch="arm64")
            checked_optical.assert_called_once()
            checked_disk.assert_called_once()
            checked_digest.assert_called_once()

    def test_both_gpt_copies_and_partition_types(self):
        last = 70000
        table = bytearray(32 * 512)
        for slot, kind, first, end in ((0, arm64.EFI_TYPE, 34, 66617), (1, arm64.UFS_TYPE, 66618, 69000)):
            i = slot * 128
            table[i:i + 16] = kind
            struct.pack_into("<QQ", table, i + 32, first, end)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gpt.fixture"

            def write_tables():
                with path.open("wb") as disk:
                    disk.truncate((last + 1) * 512)
                    for own, other, start in ((1, last, 2), (last, 1, last - 32)):
                        h = bytearray(512)
                        h[:8] = b"EFI PART"
                        struct.pack_into("<II", h, 8, 65536, 92)
                        struct.pack_into("<QQQQ", h, 24, own, other, 34, last - 33)
                        struct.pack_into("<QIII", h, 72, start, 128, 128, zlib.crc32(table))
                        struct.pack_into("<I", h, 16, zlib.crc32(h[:92]))
                        disk.seek(own * 512)
                        disk.write(h)
                        disk.seek(start * 512)
                        disk.write(table)

            write_tables()
            self.assertEqual(arm64.ufs_and_esp(path), (66618, (34, 66617)))
            with path.open("r+b") as disk:
                disk.seek((last - 32) * 512)
                disk.write(b"x")
            with self.assertRaises(ValueError):
                arm64.ufs_and_esp(path)
            table[128:144] = arm64.EFI_TYPE
            write_tables()
            with self.assertRaises(ValueError):
                arm64.ufs_and_esp(path)

    def test_installed_kernel_and_php_are_checked_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            raw = work / "disk.fixture"
            raw.write_bytes(bytes(512))
            for kernel, php in ((183, 183), (62, 183), (183, 62)):
                paths = []
                machines = iter((kernel, php))

                def ifind(args, **kwargs):
                    self.assertEqual(args[:6], ["ifind", "-f", "ufs2", "-o", "100", "-n"])
                    paths.append(args[6])
                    return subprocess.CompletedProcess(args, 0, b"123\n")

                def icat(args, **kwargs):
                    self.assertEqual(args, ["icat", "-f", "ufs2", "-o", "100", raw.name, "123"])
                    proc = MagicMock()
                    proc.stdout = io.BytesIO(arm_elf(next(machines)))
                    proc.wait.return_value = 0
                    proc.__enter__.return_value = proc
                    return proc

                with (self.subTest(kernel=kernel, php=php), patch.object(arm64, "ufs_and_esp", return_value=(100, (0, 0))),
                      patch.object(arm64, "DISK_ESP_HASH", hashlib.sha256(bytes(512)).hexdigest()),
                      patch.object(arm64, "check_esp") as checked_esp, patch.object(subprocess, "run", side_effect=ifind),
                      patch.object(subprocess, "Popen", side_effect=icat)):
                    if (kernel, php) == (183, 183):
                        arm64.check_installed(raw, work, [])
                    else:
                        with self.assertRaises(ValueError):
                            arm64.check_installed(raw, work, [])
                    checked_esp.assert_called_once()
                    self.assertEqual(paths, ["/boot/kernel/kernel"] if kernel != 183 else ["/boot/kernel/kernel", "/usr/local/bin/php"])
