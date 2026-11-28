"""GPT checks shared by installed-disk admission for the pinned FreeBSD UFS images."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import struct
import zlib


def partitions(primary: bytes, disk_bytes: int) -> list[tuple[int, int, bytes]]:
    sectors = disk_bytes // 512
    if len(primary) < 34 * 512 or disk_bytes % 512 or sectors < 68:
        raise ValueError("invalid installed GPT disk size")
    header = primary[512:1024]
    table = primary[1024:34 * 512]
    if (header[:8] != b"EFI PART" or struct.unpack_from("<II", header, 8) != (65536, 92)
            or struct.unpack_from("<QQQQ", header, 24) != (1, sectors - 1, 34, sectors - 34)
            or struct.unpack_from("<QIII", header, 72) != (2, 128, 128, zlib.crc32(table))
            or zlib.crc32(header[:16] + bytes(4) + header[20:92]) != struct.unpack_from("<I", header, 16)[0]):
        raise ValueError("invalid installed primary GPT")
    parts = []
    for offset in range(0, len(table), 128):
        kind = bytes(table[offset:offset + 16])
        if any(kind):
            first, last = struct.unpack_from("<QQ", table, offset + 32)
            parts.append((first, last, kind))
    previous = 33
    for first, last, _ in sorted(parts):
        if not previous < first <= last <= sectors - 34:
            raise ValueError("invalid installed GPT partition bounds")
        previous = last
    return parts


def verify_backup(primary: bytes, ending: bytes, disk_bytes: int) -> None:
    last = disk_bytes // 512 - 1
    expected = bytearray(primary[512:604])
    struct.pack_into("<QQ", expected, 24, last, 1)
    struct.pack_into("<Q", expected, 72, last - 32)
    struct.pack_into("<I", expected, 16, 0)
    struct.pack_into("<I", expected, 16, zlib.crc32(expected))
    secondary = ending[-512:]
    if ending[:-512] != primary[1024:34 * 512] or secondary[:len(expected)] != expected:
        raise ValueError("secondary GPT differs from the verified installed disk layout")
