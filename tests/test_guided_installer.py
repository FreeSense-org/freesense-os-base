"""Exercise the shared guided and unattended state machine without disk devices."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import hashlib
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/installer"))


class GuidedInstaller(unittest.TestCase):
    def test_padding_checks_both_headers_and_volume_size(self):
        builder_path = Path(__file__).resolve().parents[1] / "scripts/installer/build_iso.py"
        spec = importlib.util.spec_from_file_location("guided_builder", builder_path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        data = bytearray(128 * 512)
        for own, other in ((1, 127), (127, 1)):
            header = bytearray(512)
            header[:8] = b"EFI PART"
            struct.pack_into("<I", header, 12, 92)
            struct.pack_into("<QQ", header, 24, own, other)
            struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
            data[own * 512:(own + 1) * 512] = header
        data[32768:32774] = b"\x01CD001"
        struct.pack_into("<I", data, 32768 + 80, 32)
        struct.pack_into(">I", data, 32768 + 84, 32)
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "padding"
            image.write_bytes(data + bytes(2048))
            builder.trim_optical_padding(image)
            self.assertEqual(image.read_bytes(), data)
            for position in (512 + 18, 127 * 512 + 23, 32768 + 84, len(data) + 12):
                changed = data + bytes(2048)
                changed[position] ^= 1
                image.write_bytes(changed)
                with self.assertRaises(ValueError):
                    builder.trim_optical_padding(image)
                self.assertEqual(image.read_bytes(), changed)

    def test_usb_layout_readback(self):
        builder_path = Path(__file__).resolve().parents[1] / "scripts/installer/build_iso.py"
        spec = importlib.util.spec_from_file_location("guided_builder", builder_path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        ranges = [(64, 319), (320, 4415), (4416, 37183)]
        data = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
        efi = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "synthetic-layout"
            with image.open("wb") as disk:
                disk.truncate(37184 * 512)
                disk.seek(512)
                disk.write(b"EFI PART")
                disk.seek(512 + 72)
                disk.write(struct.pack("<QII", 2, 4, 128))
                for index, (bounds, kind) in enumerate(zip(ranges, (data, efi, data))):
                    offset = 1024 + index * 128
                    disk.seek(offset)
                    disk.write(uuid.UUID(kind).bytes_le)
                    disk.seek(offset + 32)
                    disk.write(struct.pack("<QQ", *bounds))
                disk.seek(64 * 512 + 32768)
                disk.write(b"\x01CD001")
            fat_hash = hashlib.sha256(bytes(16 * 1048576)).hexdigest()
            esp_hash = hashlib.sha256(bytes(2097152)).hexdigest()
            builder.verify_usb_layout(image, fat_hash, esp_hash)
            with self.assertRaises(ValueError):
                builder.verify_usb_layout(image, fat_hash, "0" * 64)
            with patch.object(builder, "ESP_HASH", esp_hash):
                builder.verify_usb_layout(image, fat_hash)
                with self.assertRaises(ValueError):
                    builder.verify_usb_layout(image, "0" * 64)
                with image.open("r+b") as disk:
                    disk.seek(64 * 512 + 32768)
                    disk.write(b"broken")
                with self.assertRaises(ValueError):
                    builder.verify_usb_layout(image, fat_hash)

    def test_no_device_rehearsal(self):
        root = Path(__file__).resolve().parents[1]
        directory = root / "scripts/installer/guided"
        for name in ("test_guided_installer.sh", "test_wizard.sh"):
            with self.subTest(script=name):
                # Git Bash on Windows cannot create the symlinks these cases refuse; use the container there.
                if os.name != "nt" and (shell := shutil.which("sh")):
                    command = [shell, str(root / "tests" / name)]
                else:
                    command = ["docker", "run", "--rm", "--network", "none", "--pull=never",
                               "-v", f"{root}:/source:ro", "-w", "/source/scripts/installer/guided",
                               "php:8.6.0-cli", "sh", f"/source/tests/{name}"]
                result = subprocess.run(command, cwd=directory, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

