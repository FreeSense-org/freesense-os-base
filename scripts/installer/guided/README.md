# Guided installer

The boot medium runs `/etc/rc.local` as root. It loads the shell backend, the FreeBSD hardware adapter, the provisioning parser, and the console wizard. FreeBSD base supplies the runtime. UI code has no calls that open target disks. The host-side builder requires Python, xorriso and mkfs.fat, either locally or in the supplied offline container.

## Boot and select

The `--arch` and `--firmware` options of `scripts/installer/build_iso.py` select a generic installer and record the platform in provenance.

| Profile | Installer boot | Installed UFS disk |
| --- | --- | --- |
| `amd64` / `uefi` (default) | El Torito EFI optical boot or raw-written USB with an EFI System Partition and a FAT16 `FSSETUP` volume | GPT with EFI System Partition, EFI video and COM1 |
| `amd64` / `bios` | Optical drive with the stock FreeBSD x86 no-emulation CD loader | GPT with the verified FreeBSD protective MBR and `freebsd-boot` partition, BIOS video and COM1 |
| `arm64` / `uefi` | Generic AArch64 UEFI optical or hybrid USB candidate with `EFI/BOOT/BOOTAA64.EFI` | GPT with the pinned stock AArch64 EFI partition, UFS kernel and PHP runtime |

The BIOS profile requires a kernel-observed BIOS boot and an optical root device. A separate FAT USB can supply the plan. Before authoring, the builder checks the protective MBR, both GPT headers and tables, partition bounds, complete `gptboot` partition and unique UFS root. The writer verifies the restored disk before setting `console=vidconsole,comconsole` in that root. Both profiles exclude the boot disk and recognized USB and optical devices from target selection. Press Enter at the keyboard or serial console for guided installation. Serial uses 115200 baud.

A selectable target needs a unique, non-placeholder GEOM serial (`ident`) or logical unit ID (`lunid`), an unoccupied provider, 512-byte logical sectors and room for the expanded payload. A partition table, an existing consumer, missing stable identity or unknown SCSI transport disqualifies storage. With multiple eligible disks, select one explicitly. The typed confirmation includes the device path, physical ID, model and capacity. Final checks independently verify the payload, every byte of the blank target and the restored data. Larger devices also get a verified secondary GPT at their final sector.

## Unattended install

Place one regular file named `freesense-install.conf` at the root of the install medium or a FAT USB/ISO9660 provisioning volume. For a raw-written installer USB use the root of `FSSETUP`. The minimum file contains four lines. Replace the example selectors with the actual values.

```ini
version=1
action=install
machine_uuid=823c4d25-c32a-4740-a345-56ce023b16a3
target_id=EXAMPLE-SERIAL-001
```

`machine_uuid` matches the SMBIOS system UUID. `target_id` matches the physical GEOM ID exactly. The optional `lan` field names an available Ethernet interface. The optional `fleet_seed` field holds one line of validated Factory seed JSON. Store private provisioning files on media under your control. Successful validation of a unique plan invokes the same blank-disk and image verification stages as the attended wizard. A malformed, duplicated or mismatched plan stops installation. A standalone `fleet-join.conf` at the volume root or under `config/` can be accepted explicitly by a console operator. Combining it with an unattended plan is ambiguous. Fleet payloads stay in a root-private workspace and are passed to the verified installed root after readback.

## Source interfaces and checks

After sourcing the provision, backend, media and FreeBSD modules, call `guided_preflight` in the current shell. A ready plan calls `guided_unattended`. An absent plan calls `guided_terminal` and `guided_wizard`. The UI reads `FS_INSTALL_TARGETS` (provider, ID, model and capacity as tab-separated fields), `FS_INSTALL_EXCLUSIONS`, `FS_INSTALL_SEED_STATUS` and `FS_INSTALL_INTERFACES`. `guided_select provider` publishes the required `FS_INSTALL_CONFIRM`. `guided_install "$typed"` consumes an exact typed confirmation. `guided_back` returns to selection and `guided_cancel` discards the private workspace.

Run `python tools/check.py --component freesense-os-base` from the refactor workspace. The host test sources the rehearsal adapter in place of the hardware adapter and exercises selection, back, cancel, wrong identity, changed media, missing or ambiguous provisioning, write/readback failure, and UI navigation. The rehearsal is excluded from boot media. Boot and installed-system acceptance require an isolated blank-disk VM.

BIOS acceptance remains pending. Use legacy BIOS optical boot with an identified blank 512-byte-sector disk, confirm the complete disk readback and GPT recovery on a larger target, then remove the CD and cold-boot the installed disk. Check the video and 115200-baud serial consoles, saved configuration and a second boot. Repeat the existing UEFI optical and USB cases when validating newly assembled media.

For generic arm64 the builder checks the FreeBSD 15.1 arm64-aarch64 bootonly medium, its 2 MiB optical EFI image and its actual AArch64 PE32+ fallback loader. To check just this upstream input, run `python scripts/installer/build_iso.py WORK --arch arm64 --verify-base-only`, adding `--container NAME` when the required utilities are in a local container. [FreeBSD arm64 optical checksums](https://download.freebsd.org/releases/arm64/aarch64/ISO-IMAGES/15.1/CHECKSUM.SHA256-FreeBSD-15.1-RELEASE-arm64-aarch64)

The arm64 UFS input starts with the 2026-11-17 FreeBSD 16.0-CURRENT arm64-aarch64 BASIC-CLOUDINIT disk, revision `611046b15dbb-291235`. Stage the reviewed System and native PHP 8.6 final on a fresh copy. Normal arm64 assembly validates both GPT copies, extracts the stock EFI partition and checks the machine types of `/boot/kernel/kernel` and `/usr/local/bin/php` from the verified UFS root. Provide temporary space for the expanded raw disk and install mtools and The Sleuth Kit (`mcopy`, `ifind`, `icat`) alongside xorriso and mkfs.fat. Native installer acceptance still requires a matched, staged arm64 payload, an AArch64 UEFI machine, an identified blank disk, a verified write and a cold boot of the installed system. [FreeBSD arm64 stock disk checksums](https://download.freebsd.org/snapshots/VM-IMAGES/16.0-CURRENT/aarch64/20261117/CHECKSUM.SHA256-20261117-611046b15dbb-291235)

The Development reference at `d068e7b3a90e66ff5d260d914fa902cb713c8db1` installs `pmbr` and `gptboot` for amd64 UFS in `scripts/runner/stages/cloud.sh`. Its generic arm64 disk uses `BOOTAA64.EFI`. The Raspberry Pi profiles in `scripts/runner/stages/appliance.sh` stage board-specific firmware. The `arm64-rpi4b` profile loads U-Boot and `arm64-rpi5-d0` loads `RPI_EFI.fd`. Both then start the arm64 EFI loader. These paths need their own installer inputs and board acceptance.

