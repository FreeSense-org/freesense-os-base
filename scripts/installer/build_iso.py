"""Assemble the local guided installer from verified offline inputs."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import argparse
import hashlib
import json
import lzma
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import uuid
import zlib

import disk_layout

SOURCES = Path(__file__).resolve().parent
UPSTREAM = "FreeBSD-15.1-RELEASE-amd64-bootonly.iso"
BASE_URL = "https://download.freebsd.org/releases/amd64/amd64/ISO-IMAGES/15.1/"
BASE_SHA = "3e74120a59512cefc35840443bdd05087c8a010a27cf8a6fbb4f7450824f092e"
VOLUME = "FREESENSE_INSTALL"
ESP_HASH = "2c1ad7eb94eac84e854d02326699522a587719c80633062c436c84283733451c"
# No-emulation boot sector from the pinned 15.1 amd64 optical medium.
CDBOOT_HASH = "cdbb7e0c30551e26a03c25bf08303e0def7cf889ea8a1ada0015217b1a5bc159"
# First 440 PMBR bytes and complete freebsd-boot partition from the pinned
# FreeBSD 16.0-CURRENT amd64 UFS cloud disk, revision 611046b15dbb.
PMBR_HASH = "6312dc8f3b5dc644d8c374b08dfb3f9e7b994ad3fc050a504ad190b4e1895e88"
GPTBOOT_HASH = "ad91b4f7343bdd42acdbdd4786189c811aa408e6516c906385bd99170256f563"
MEDIA_FILES = (
    "provision.sh", "provision.lua", "vendor/decoder.lua", "vendor/LICENSE", "vendor/README.md",
    "guided/backend.sh", "guided/freebsd.sh", "guided/media.sh", "guided/terminal.sh", "guided/wizard.sh",
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bios_boot_sector(path: Path) -> bytes:
    """Read the bootable x86 no-emulation default entry used by this optical profile."""
    with path.open("rb") as disk:
        disk.seek(17 * 2048)
        record = disk.read(2048)
        if len(record) != 2048 or record[:39] != b"\x00CD001\x01" + b"EL TORITO SPECIFICATION".ljust(32, b"\x00"):
            raise ValueError("missing optical boot record")
        disk.seek(struct.unpack_from("<I", record, 71)[0] * 2048)
        catalog = disk.read(64)
        if (len(catalog) != 64 or catalog[:2] != b"\x01\x00" or catalog[30:32] != b"\x55\xaa"
                or sum(struct.unpack_from("<16H", catalog)) % 65536 or catalog[32:40] != bytes.fromhex("8800000000000400")):
            raise ValueError("missing x86 BIOS no-emulation boot entry")
        disk.seek(struct.unpack_from("<I", catalog, 40)[0] * 2048)
        sector = disk.read(2048)
        if hashlib.sha256(sector).hexdigest() != CDBOOT_HASH:
            raise ValueError("BIOS optical boot sector differs from the pinned CD loader")
        return sector


def verify_bios_disk(prefix: bytes, disk_bytes: int) -> None:
    """Admit the pinned amd64 UFS boot code and a nonoverlapping, usable GPT."""
    sectors = disk_bytes // 512
    if (len(prefix) < 155 * 512 or disk_bytes % 512 or sectors <= 155
            or hashlib.sha256(prefix[:440]).hexdigest() != PMBR_HASH or prefix[510:512] != b"\x55\xaa"
            or prefix[450] != 0xee or struct.unpack_from("<II", prefix, 454) != (1, min(sectors - 1, 0xffffffff))
            or any(prefix[462:510])):
        raise ValueError("missing pinned BIOS protective MBR")
    partitions = disk_layout.partitions(prefix, disk_bytes)
    boot = uuid.UUID("83bd6b9d-7f41-11dc-be0b-001560b84f0f").bytes_le
    ufs = uuid.UUID("516e7cb6-6ecf-11d6-8ff8-00022d09712b").bytes_le
    if ([p[:2] for p in partitions if p[2] == boot] != [(34, 154)] or sum(p[2] == ufs for p in partitions) != 1
            or hashlib.sha256(prefix[34 * 512:155 * 512]).hexdigest() != GPTBOOT_HASH):
        raise ValueError("missing pinned gptboot partition or unique UFS system")


def validate_payload(image: Path, metadata: dict, firmware: str = "uefi", raw_copy=None) -> None:
    if image.stat().st_size != metadata["xz_bytes"] or sha256(image) != metadata["xz_sha256"]:
        raise ValueError("compressed disk image does not match manifest")
    digest = hashlib.sha256()
    size = 0
    primary = ending = b""
    with lzma.open(image, "rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            if firmware == "bios":
                if size == 0:
                    verify_bios_disk(block, metadata["raw_bytes"])
                    primary = block[:34 * 512]
                tail_bytes = 33 * 512
                ending = (ending + block[-tail_bytes:])[-tail_bytes:]
            digest.update(block)
            size += len(block)
            if size > metadata["raw_bytes"]:
                raise ValueError("expanded disk exceeds the manifest size")
            if raw_copy is not None:
                raw_copy.write(block)
    if size == 0 or size % 512 or size != metadata["raw_bytes"] or digest.hexdigest() != metadata["raw_sha256"]:
        raise ValueError("expanded disk image does not match manifest")
    if firmware == "bios":
        disk_layout.verify_backup(primary, ending, size)


def trim_optical_padding(path: Path) -> None:
    """Remove only checked recording padding beyond both the declared volume and backup GPT."""
    with path.open("r+b") as disk:
        disk.seek(512)
        primary = disk.read(512)
        if primary[:8] != b"EFI PART":
            raise ValueError("missing primary GPT")
        last = struct.unpack_from("<Q", primary, 32)[0]
        end = (last + 1) * 512
        if end > path.stat().st_size or path.stat().st_size - end > 2 * 1048576 or end % 2048:
            raise ValueError("unexpected GPT extent")
        disk.seek(last * 512)
        backup = disk.read(512)
        for header, own, other in ((primary, 1, last), (backup, last, 1)):
            size, checksum = struct.unpack_from("<II", header, 12)
            if (header[:8] != b"EFI PART" or not 92 <= size <= 512
                    or struct.unpack_from("<QQ", header, 24) != (own, other)
                    or zlib.crc32(header[:16] + bytes(4) + header[20:size]) != checksum):
                raise ValueError("invalid GPT header identity or checksum")
        disk.seek(32768)
        descriptor = disk.read(2048)
        if (descriptor[:6] != b"\x01CD001" or struct.unpack_from("<I", descriptor, 80)[0] * 2048 != end
                or struct.unpack_from(">I", descriptor, 84)[0] * 2048 != end):
            raise ValueError("ISO9660 volume size does not end at the backup GPT")
        disk.seek(end)
        if any(disk.read()):
            raise ValueError("unexpected data beyond declared ISO/GPT volume")
        disk.truncate(end)


def verify_usb_layout(path: Path, expected_fat: str, expected_esp: str | None = None) -> None:
    """Inspect partition bytes independently of xorriso's report."""
    with path.open("rb") as disk:
        disk.seek(512)
        header = disk.read(512)
        if header[:8] != b"EFI PART":
            raise ValueError("missing GPT")
        table, count, span = struct.unpack_from("<QII", header, 72)
        if span != 128 or count > 4096 or count < 3:
            raise ValueError("invalid GPT table size")
        disk.seek(512 * table)
        entries = [disk.read(span) for _ in range(count)]
        entries = [entry for entry in entries if any(entry[:16])]
        efi_type = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
        data_type = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
        if [str(uuid.UUID(bytes_le=entry[:16])) for entry in entries] != [data_type, efi_type, data_type]:
            raise ValueError("expected ISO9660, EFI and provisioning GPT partitions")
        extents = [struct.unpack_from("<QQ", entry, 32) for entry in entries]
        if not (extents[0][0] < extents[0][1] < extents[1][0] < extents[1][1] < extents[2][0] < extents[2][1]):
            raise ValueError("invalid hybrid GPT extents")
        disk.seek(extents[0][0] * 512 + 32768)
        if disk.read(6) != b"\x01CD001":
            raise ValueError("partition one lacks the ISO9660 volume descriptor")
        for (first, last), size, expected in zip(extents[1:], (2097152, 16 * 1048576), (expected_esp or ESP_HASH, expected_fat)):
            if (last - first + 1) * 512 != size:
                raise ValueError("unexpected USB partition size")
            disk.seek(first * 512)
            if hashlib.sha256(disk.read(size)).hexdigest() != expected:
                raise ValueError("USB partition readback failed")


def assemble(work: Path, container: str | None, firmware: str = "uefi", arch: str = "amd64",
             verify_base_only: bool = False) -> None:
    if (arch, firmware) not in (("arm64", "uefi"), ("amd64", "uefi"), ("amd64", "bios")):
        raise ValueError("unsupported guided installer architecture and firmware")
    base_name, base_url, base_hash, esp_hash = UPSTREAM, BASE_URL, BASE_SHA, ESP_HASH
    record_name = "installer-provenance.txt" if firmware == "uefi" else "installer-bios-provenance.txt"
    if arch == "arm64":
        import arm64
        base_name, base_url, base_hash, esp_hash = arm64.UPSTREAM, arm64.BASE_URL, arm64.BASE_SHA, arm64.ESP_HASH
        record_name = "installer-arm64-uefi-provenance.txt"
    base = work / base_name
    output = work / f"FreeSense-2.0.0-guided-{arch}-{firmware}.iso"
    record = work / record_name
    if not verify_base_only and (output.exists() or record.exists()):
        raise FileExistsError("installer artifact or its provenance already exists")
    if sha256(base) != base_hash:
        raise ValueError("FreeBSD optical media failed the pinned official checksum")
    runner = []
    if container:
        runner = ["docker", "run", "--rm", "--pull=never", "--network", "none", "-v", f"{work}:/work",
                  "-w", "/work", container]
    tool = [*runner, "xorriso"]
    with tempfile.TemporaryDirectory(prefix="iso-stage-", dir=work) as tmp:
        stage = Path(tmp)
        if firmware == "uefi":
            esp = stage / "efi.img"
            with base.open("rb") as stream:
                stream.seek(20 * 2048)
                esp.write_bytes(stream.read(4096 * 512))
            if sha256(esp) != esp_hash:
                raise ValueError("EFI boot image differs from the verified upstream input")
            if arch == "arm64":
                arm64.check_esp(esp, arm64.CD_LOADER_HASH, work, runner)
        else:
            (stage / "cdboot.img").write_bytes(bios_boot_sector(base))
        if verify_base_only:
            print(f"Verified {arch} {firmware.upper()} optical input: {base.name}")
            return
        image = work / "payload.raw.xz"
        info = json.loads((work / "payload.json").read_text(encoding="utf-8-sig"))
        if arch == "arm64":
            # Inspect the actual kernel and PHP ELF headers in the verified UFS root.
            raw = stage / "installed.raw"
            with raw.open("xb") as copied:
                validate_payload(image, info, firmware, copied)
            arm64.check_installed(raw, work, runner)
            raw.unlink()
        else:
            validate_payload(image, info, firmware)
        media = stage / "freesense"
        media.mkdir()
        for name in MEDIA_FILES:
            destination = media / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCES / name, destination)
        shutil.copyfile(SOURCES / "rc.local", stage / "rc.local")
        (media / "firmware").write_text(firmware.upper() + "\n", encoding="ascii", newline="\n")
        (media / "architecture").write_text(arch + "\n", encoding="ascii", newline="\n")
        for key, file in [("raw_bytes", "raw.bytes"), ("raw_sha256", "raw.sha256"), ("xz_sha256", "xz.sha256")]:
            (media / file).write_text(str(info[key]) + "\n", encoding="ascii", newline="\n")
        fstab = f"/dev/iso9660/{VOLUME} / cd9660 ro 0 0\n"
        display = "efi" if firmware == "uefi" else "vidconsole"
        loader = (f'console="{display},comconsole"\nboot_multicons="YES"\ncomconsole_speed="115200"\n'
                  'kernels_autodetect="NO"\nautoboot_delay="1"\nvfs.mountroot.timeout="15"\n'
                  f'vfs.root.mountfrom="cd9660:/dev/iso9660/{VOLUME}"\n')
        (stage / "fstab").write_text(fstab, encoding="ascii", newline="\n")
        (stage / "loader.conf").write_text(loader, encoding="ascii", newline="\n")
        relative = stage.relative_to(work).as_posix()
        pending = relative + "/prototype.iso"
        args = ["-indev", base.name, "-outdev", pending, "-boot_image", "any", "discard", "-volid", VOLUME,
                "-map", relative + "/freesense", "/freesense", "-map", image.name, "/freesense/payload.raw.xz",
                "-map", relative + "/rc.local", "/etc/rc.local", "-map", relative + "/loader.conf", "/boot/loader.conf",
                "-map", relative + "/fstab", "/etc/fstab", "-chmod", "0555", "/etc/rc.local", "--"]
        if firmware == "uefi":
            config = stage / "fssetup.img"
            with config.open("xb") as stream:
                stream.truncate(16 * 1048576)
            subprocess.run(runner + ["mkfs.fat", "-F", "16", "-n", "FSSETUP", relative + "/fssetup.img"],
                           cwd=work, check=True, capture_output=True)
            args += ["-map", relative + "/efi.img", "/boot/freesense-efi.img",
                     "-boot_image", "any", "efi_path=/boot/freesense-efi.img", "-boot_image", "any", "partition_offset=16",
                     "-append_partition", "2", "0xef", relative + "/efi.img",
                     "-append_partition", "3", "0x0c", relative + "/fssetup.img", "-boot_image", "any", "appended_part_as=gpt"]
            shipped = {"/boot/freesense-efi.img": esp_hash}
            platform = [f"Embedded EFI System Partition SHA-256 {esp_hash}", f"Empty FSSETUP FAT16 SHA-256 {sha256(config)}",
                        f"Optical: {arch} UEFI El Torito. USB GPT: ISO9660 + official EFI System Partition + FSSETUP FAT16."]
        else:
            args += ["-map", relative + "/cdboot.img", "/boot/freesense-cdboot.img",
                     "-boot_image", "any", "bin_path=/boot/freesense-cdboot.img", "-boot_image", "any", "platform_id=0x00",
                     "-boot_image", "any", "emul_type=no_emulation", "-boot_image", "any", "load_size=2048"]
            shipped = {"/boot/freesense-cdboot.img": CDBOOT_HASH}
            platform = [f"Optical: amd64 BIOS El Torito. CD loader sector SHA-256 {CDBOOT_HASH}",
                        f"Installed disk: PMBR code SHA-256 {PMBR_HASH}, freebsd-boot partition SHA-256 {GPTBOOT_HASH}"]
        subprocess.run(tool + args + ["-commit", "-end"], cwd=work, check=True)
        if firmware == "uefi":
            trim_optical_padding(work / pending)
        catalog = subprocess.run(tool + ["-indev", pending, "-report_el_torito", "plain"],
                                 cwd=work, check=True, capture_output=True, text=True).stdout
        if firmware == "bios":
            bios_boot_sector(work / pending)
        else:
            if "UEFI  y" not in catalog or "/boot/freesense-efi.img" not in catalog:
                raise ValueError("finished ISO has no bootable UEFI entry")
            verify_usb_layout(work / pending, sha256(config), esp_hash)
        # Check what the installer will actually read from the final optical filesystem.
        extract = ["-osirrox", "on", "-indev", pending]
        shipped.update({"/freesense/payload.raw.xz": info["xz_sha256"],
                        "/etc/rc.local": sha256(stage / "rc.local"), "/etc/fstab": sha256(stage / "fstab"),
                        "/boot/loader.conf": sha256(stage / "loader.conf")})
        shipped.update({"/freesense/" + file.relative_to(media).as_posix(): sha256(file)
                        for file in media.rglob("*") if file.is_file()})
        extracted = stage / "readback"
        extracted.mkdir()
        for index, (iso_path, expected) in enumerate(shipped.items()):
            path = extracted / str(index)
            subprocess.run(tool + extract + ["-extract", iso_path, path.relative_to(work).as_posix()],
                           cwd=work, check=True, capture_output=True)
            if sha256(path) != expected:
                raise ValueError(f"ISO readback mismatch for {iso_path}")
        provenance = [f"ISO {output.name}", f"ISO SHA-256 {sha256(work / pending)}",
                      f"Base media {base_url}{base_name}", f"Base media SHA-256 {base_hash}",
                      f"Official checksums {base_url}CHECKSUM.SHA256-{base_name.removesuffix('-bootonly.iso')}",
                      *platform, f"Console: {display} and serial. Root provisioning basename: freesense-install.conf.",
                      *[f"Payload {k} {info[k]}" for k in ("raw_bytes", "raw_sha256", "xz_bytes", "xz_sha256")],
                      *[f"Source SHA-256 {name} {sha256(SOURCES / name)}"
                        for name in (*MEDIA_FILES, "rc.local", "build_iso.py", "disk_layout.py", *(("arm64.py",) if arch == "arm64" else ()))], catalog]
        (work / pending).rename(output)
        record.write_text("\n".join(provenance) + "\n", encoding="utf-8")
        print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path, help="directory containing verified base media, payload.raw.xz and payload.json")
    parser.add_argument("--container", help="use xorriso from this locally available Docker image")
    parser.add_argument("--firmware", choices=("uefi", "bios"), default="uefi",
                        help="firmware profile (default: UEFI optical and USB; amd64 BIOS uses optical media)")
    parser.add_argument("--arch", choices=("amd64", "arm64"), default="amd64",
                        help="generic installer machine architecture (default: amd64)")
    parser.add_argument("--verify-base-only", action="store_true", help="verify the selected optical boot input and exit")
    args = parser.parse_args()
    assemble(args.work.resolve(strict=True), args.container, args.firmware, args.arch, args.verify_base_only)
