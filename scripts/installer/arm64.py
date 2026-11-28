"""Validate FreeBSD generic arm64 boot files and the binaries in the installed UFS root."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import hashlib
from pathlib import Path
import re
import struct
import subprocess
import uuid

import disk_layout


UPSTREAM = "FreeBSD-15.1-RELEASE-arm64-aarch64-bootonly.iso"
BASE_URL = "https://download.freebsd.org/releases/arm64/aarch64/ISO-IMAGES/15.1/"
BASE_SHA = "359136c2af73e03da6f15ad59f0c67bc561ca8b69631d78bfb8f2225e2c9a5ef"
ESP_HASH = "5a09328dc99eb8b02f61f8071049260dda83574608c47abaedc4c691761e78d9"
CD_LOADER_HASH = "f7fce7a5282a78da5c0cf183fa7ea5bb6070edd48d9d0ae261965e08221e2cb2"
# EFI contents are kept intact when staging the System on the dated official
# 16.0-CURRENT arm64-aarch64 UFS snapshot, revision 611046b15dbb.
DISK_ESP_HASH = "364e062714f6cc695a7375aa43700a5a0766e0e1b80b74be35b05a66823659c0"
DISK_LOADER_HASH = "a8af916dfd984b0c5b0126a82da5ebfa1066e048f2817962a13c7d1d7af4b5e1"
EFI_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b").bytes_le
UFS_TYPE = uuid.UUID("516e7cb6-6ecf-11d6-8ff8-00022d09712b").bytes_le


def require_efi_application(data: bytes) -> None:
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("missing arm64 EFI application")
    pe = struct.unpack_from("<I", data, 60)[0]
    if pe < 64 or pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("invalid arm64 PE header")
    machine, sections = struct.unpack_from("<HH", data, pe + 4)
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    if (machine != 0xaa64 or not 1 <= sections <= 96 or optional_size < 112
            or pe + 24 + optional_size + sections * 40 > len(data)
            or struct.unpack_from("<H", data, pe + 24)[0] != 0x20b
            or struct.unpack_from("<H", data, pe + 24 + 68)[0] != 10):
        raise ValueError("expected an AArch64 PE32+ EFI application")


def require_elf(data: bytes) -> None:
    if (len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01"
            or struct.unpack_from("<H", data, 16)[0] not in (2, 3)
            or struct.unpack_from("<H", data, 18)[0] != 183):
        raise ValueError("expected an AArch64 64-bit kernel or executable")


def check_esp(esp: Path, expected_loader: str, work: Path, runner: list[str]) -> None:
    loader = esp.with_suffix(".aa64.efi")
    subprocess.run([*runner, "mcopy", "-i", esp.relative_to(work).as_posix(), "::/EFI/BOOT/BOOTAA64.EFI",
                    loader.relative_to(work).as_posix()], cwd=work, check=True, capture_output=True)
    data = loader.read_bytes()
    require_efi_application(data)
    if hashlib.sha256(data).hexdigest() != expected_loader:
        raise ValueError("arm64 fallback loader differs from its pinned FreeBSD input")


def ufs_and_esp(raw: Path) -> tuple[int, tuple[int, int]]:
    """Read both GPT copies, then identify one ESP and one UFS root by type."""
    size = raw.stat().st_size
    if size < 68 * 512 or size % 512:
        raise ValueError("invalid arm64 disk size")
    with raw.open("rb") as disk:
        primary = disk.read(34 * 512)
        disk.seek(-33 * 512, 2)
        ending = disk.read(33 * 512)
    parts = disk_layout.partitions(primary, size)
    disk_layout.verify_backup(primary, ending, size)
    roots = [p[0] for p in parts if p[2] == UFS_TYPE]
    esps = [p[:2] for p in parts if p[2] == EFI_TYPE]
    if len(roots) != 1 or len(esps) != 1 or esps[0][1] - esps[0][0] + 1 != 66584:
        raise ValueError("expected a unique arm64 UFS root and stock EFI partition")
    return roots[0], esps[0]


def check_installed(raw: Path, work: Path, runner: list[str]) -> None:
    root, (start, end) = ufs_and_esp(raw)
    esp = raw.with_suffix(".esp")
    with raw.open("rb") as disk:
        disk.seek(start * 512)
        data = disk.read((end - start + 1) * 512)
    if hashlib.sha256(data).hexdigest() != DISK_ESP_HASH:
        raise ValueError("installed EFI partition differs from the pinned arm64 disk")
    esp.write_bytes(data)
    check_esp(esp, DISK_LOADER_HASH, work, runner)
    disk_args = ["-f", "ufs2", "-o", str(root)]
    name = raw.relative_to(work).as_posix()
    for executable in ("/boot/kernel/kernel", "/usr/local/bin/php"):
        found = subprocess.run([*runner, "ifind", *disk_args, "-n", executable, name], cwd=work,
                               check=True, capture_output=True).stdout.strip()
        if not re.fullmatch(rb"[0-9]+", found):
            raise ValueError(f"missing installed {executable}")
        with subprocess.Popen([*runner, "icat", *disk_args, name, found.decode("ascii")],
                              stdout=subprocess.PIPE, cwd=work) as proc:
            header = proc.stdout.read(64)
            while proc.stdout.read(1024 * 1024):
                pass
            if proc.wait() != 0:
                raise ValueError(f"cannot read installed {executable}")
        require_elf(header)
