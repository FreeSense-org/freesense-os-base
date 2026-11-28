"""Exercise optical firmware selection and BIOS boot data with ordinary fixtures."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import hashlib
import importlib.util
import json
import lzma
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/installer"))


spec = importlib.util.spec_from_file_location("builder", Path(__file__).resolve().parents[1] / "scripts/installer/build_iso.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BiosFirmwareTest(unittest.TestCase):
    def test_optical_catalog(self):
        data = bytearray(22 * 2048)
        descriptor = 17 * 2048
        data[descriptor:descriptor + 30] = b"\x00CD001\x01EL TORITO SPECIFICATION"
        struct.pack_into("<I", data, descriptor + 71, 19)
        catalog = 19 * 2048
        data[catalog] = 1
        data[catalog + 30:catalog + 40] = bytes.fromhex("55aa8800000000000400")
        struct.pack_into("<H", data, catalog + 28, -sum(struct.unpack_from("<16H", data, catalog)) % 65536)
        struct.pack_into("<I", data, catalog + 40, 21)
        sector = b"cd" * 1024
        data[21 * 2048:] = sector
        with tempfile.TemporaryDirectory() as tmp, patch.object(builder, "CDBOOT_HASH", hashlib.sha256(sector).hexdigest()):
            path = Path(tmp) / "catalog.fixture"
            path.write_bytes(data)
            self.assertEqual(builder.bios_boot_sector(path), sector)
            for pos in (descriptor, descriptor + 71, catalog + 1, catalog + 28, catalog + 32,
                        catalog + 33, catalog + 38, catalog + 40, 21 * 2048 + 1300):
                with self.subTest(position=pos):
                    damaged = bytearray(data)
                    damaged[pos] ^= 1
                    path.write_bytes(damaged)
                    with self.assertRaises(ValueError):
                        builder.bios_boot_sector(path)

    def test_installed_disk_boot_chain(self):
        data = bytearray(155 * 512)
        sectors = 300
        data[:440] = b"M" * 440
        data[450] = 0xee
        struct.pack_into("<II", data, 454, 1, sectors - 1)
        data[510:512] = b"\x55\xaa"
        data[512:520] = b"EFI PART"
        struct.pack_into("<II", data, 520, 65536, 92)
        struct.pack_into("<QQQQ", data, 536, 1, sectors - 1, 34, sectors - 34)
        struct.pack_into("<QII", data, 584, 2, 128, 128)
        for index, first, last, part in ((0, 34, 154, "83bd6b9d-7f41-11dc-be0b-001560b84f0f"),
                                         (1, 155, 266, "516e7cb6-6ecf-11d6-8ff8-00022d09712b")):
            entry = 1024 + index * 128
            data[entry:entry + 16] = uuid.UUID(part).bytes_le
            struct.pack_into("<QQ", data, entry + 32, first, last)
        data[34 * 512:] = b"B" * (121 * 512)

        def checksums(prefix):
            struct.pack_into("<I", prefix, 600, zlib.crc32(prefix[1024:34 * 512]))
            struct.pack_into("<I", prefix, 528, 0)
            struct.pack_into("<I", prefix, 528, zlib.crc32(prefix[512:604]))

        checksums(data)
        with (patch.object(builder, "PMBR_HASH", hashlib.sha256(data[:440]).hexdigest()),
              patch.object(builder, "GPTBOOT_HASH", hashlib.sha256(data[34 * 512:]).hexdigest())):
            builder.verify_bios_disk(data, sectors * 512)
            secondary = bytearray(data[512:1024])
            struct.pack_into("<QQ", secondary, 24, sectors - 1, 1)
            struct.pack_into("<Q", secondary, 72, sectors - 33)
            struct.pack_into("<I", secondary, 16, 0)
            struct.pack_into("<I", secondary, 16, zlib.crc32(secondary[:92]))
            full = data + bytes((sectors - 155 - 33) * 512) + data[1024:34 * 512] + secondary
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "disk.fixture.xz"
                for corrupt in (False, True):
                    if corrupt:
                        full[-512 + 32] ^= 1
                    image.write_bytes(lzma.compress(full))
                    info = {"raw_bytes": len(full), "raw_sha256": hashlib.sha256(full).hexdigest(),
                            "xz_bytes": image.stat().st_size, "xz_sha256": builder.sha256(image)}
                    if corrupt:
                        with self.assertRaisesRegex(ValueError, "secondary GPT"):
                            builder.validate_payload(image, info, "bios")
                    else:
                        builder.validate_payload(image, info, "bios")
            for pos in (12, 450, 454, 462, 510, 512, 528, 600, 1024, 34 * 512 + 1):
                with self.subTest(position=pos):
                    damaged = bytearray(data)
                    damaged[pos] ^= 1
                    with self.assertRaises(ValueError):
                        builder.verify_bios_disk(damaged, sectors * 512)
            for pos, replacement in ((1024, b"X" * 16), (1152, bytes(16)), (1184, struct.pack("<Q", 154)),
                                     (1192, struct.pack("<Q", 290)), (1280, bytes(data[1152:1280]))):
                with self.subTest(layout=pos):
                    damaged = bytearray(data)
                    damaged[pos:pos + len(replacement)] = replacement
                    checksums(damaged)
                    with self.assertRaises(ValueError):
                        builder.verify_bios_disk(damaged, sectors * 512)

    def test_builder_selects_firmware_before_assembly(self):
        # Stop at the xorriso invocation. No media, filesystems or images are built.
        class ReachedAuthoring(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            base = work / builder.UPSTREAM
            base.write_bytes(bytes(20 * 2048 + 2097152))
            (work / "payload.json").write_text(json.dumps({"raw_bytes": 512, "raw_sha256": "a" * 64, "xz_sha256": "b" * 64}))
            for mode in ("uefi", "bios"):
                def intercept(command, **kwargs):
                    if command[0] == "mkfs.fat":
                        self.assertEqual(mode, "uefi")
                        return
                    self.assertEqual(command[0], "xorriso")
                    stage = (work / command[command.index("-outdev") + 1]).parent
                    self.assertEqual((stage / "freesense/firmware").read_text(), mode.upper() + "\n")
                    display = "vidconsole" if mode == "bios" else "efi"
                    self.assertIn(f'console="{display},comconsole"', (stage / "loader.conf").read_text())
                    if mode == "bios":
                        for option in ("bin_path=/boot/freesense-cdboot.img", "platform_id=0x00", "load_size=2048", "emul_type=no_emulation"):
                            self.assertIn(option, command)
                        self.assertNotIn("-append_partition", command)
                    else:
                        self.assertIn("efi_path=/boot/freesense-efi.img", command)
                        self.assertIn("appended_part_as=gpt", command)
                    raise ReachedAuthoring()

                with (self.subTest(firmware=mode), patch.object(builder, "BASE_SHA", builder.sha256(base)),
                      patch.object(builder, "ESP_HASH", hashlib.sha256(bytes(2097152)).hexdigest()),
                      patch.object(builder, "bios_boot_sector", return_value=bytes(2048)) as optical,
                      patch.object(builder, "validate_payload") as validated,
                      patch.object(builder.subprocess, "run", side_effect=intercept), self.assertRaises(ReachedAuthoring)):
                    try:
                        builder.assemble(work, None, *([mode] if mode == "bios" else []))
                    finally:
                        self.assertEqual(validated.call_args.args[2], mode)
                        self.assertEqual(optical.call_count, int(mode == "bios"))
