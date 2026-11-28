#!/bin/sh
# shellcheck disable=SC2034,SC1091,SC2329
# Sourced modules share functions and state with this script.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# Run from scripts/installer/guided. No character/block device is opened.
set -eu
. ./backend.sh
. ./rehearsal.sh
begin() { FS_INSTALL_STATE=new; guided_preflight; }
refuse() { if "$@"; then echo "Unexpected acceptance: $1" >&2; exit 1; fi; }
unattended() { FS_FIXTURE_PLAN=ready; guided_scan; guided_unattended; }
begin; guided_select da1; guided_back; guided_select da0; refuse guided_install wrong; guided_cancel
test "$FS_INSTALL_STATE" = cancelled && test -z "$FS_INSTALL_TRACE"
refuse guided_install ignored
begin; guided_select da1; guided_install "$FS_INSTALL_CONFIRM"
test "$FS_INSTALL_TRACE" = 'verify blank-da1 write-da1 readback-da1'
refuse guided_install ignored
begin; FS_INSTALL_FIRMWARE=BIOS; guided_select da1; guided_install "$FS_INSTALL_CONFIRM"
test "$FS_INSTALL_TRACE" = 'verify blank-da1 write-da1 readback-da1 handoff-da1'
begin; FS_INSTALL_FIRMWARE=BIOS; guided_select da1; FS_FIXTURE_FAIL=handoff-da1
refuse guided_install "$FS_INSTALL_CONFIRM"; test "$FS_INSTALL_STATE" = failed
begin; guided_select da0; FS_INSTALL_USE_SEED=yes; guided_install "$FS_INSTALL_CONFIRM"
test "$FS_INSTALL_TRACE" = 'verify blank-da0 write-da0 readback-da0 handoff-da0'
begin; guided_select da0; FS_INSTALL_LAN=em1; guided_install "$FS_INSTALL_CONFIRM"
test "$FS_INSTALL_TRACE" = 'verify blank-da0 write-da0 readback-da0 handoff-da0'
begin; guided_select da0; FS_INSTALL_LAN=phantom0; refuse guided_install "$FS_INSTALL_CONFIRM"; test -z "$FS_INSTALL_TRACE"
for change in device source seed; do
	begin; guided_select da0
	case "$change" in
		device) FS_FIXTURE_TARGETS=$(printf 'da0\tSWAPPED\tDisk A\t8589934592') ;;
		source) FS_FIXTURE_SOURCE=changed ;;
		seed) FS_FIXTURE_SEED=ambiguous ;;
	esac
	refuse guided_install "$FS_INSTALL_CONFIRM"; test -z "$FS_INSTALL_TRACE"
done
for stage in verify blank-da0 write-da0 readback-da0 handoff-da0; do
	begin; guided_select da0; FS_INSTALL_USE_SEED=yes; FS_FIXTURE_FAIL=$stage
	refuse guided_install "$FS_INSTALL_CONFIRM"
	test "$FS_INSTALL_STATE" = failed
	test "${FS_INSTALL_TRACE##* }" = "$stage"
done
begin; unattended
test "$FS_INSTALL_TRACE" = 'verify blank-da1 write-da1 readback-da1 handoff-da1'
begin; FS_FIXTURE_PLAN_UUID=unknown; refuse unattended; test -z "$FS_INSTALL_TRACE"
begin; FS_FIXTURE_PLAN_ID=unknown; refuse unattended; test -z "$FS_INSTALL_TRACE"
begin; FS_FIXTURE_PLAN=ready; guided_scan; FS_FIXTURE_PLAN=absent; refuse guided_unattended; test -z "$FS_INSTALL_TRACE"
begin; FS_FIXTURE_TARGETS=$(printf 'da0\tDUPLICATE\tDisk A\t8589934592\nda1\tDUPLICATE\tDisk B\t8589934592'); refuse guided_scan
begin; FS_FIXTURE_SEED=ambiguous; refuse guided_scan
begin; FS_FIXTURE_PLAN=invalid; refuse guided_scan
begin; fs_adapter_blank() { fs_rehearse "blank-$1" && FS_FIXTURE_SOURCE=changed; }
guided_select da0; refuse guided_install "$FS_INSTALL_CONFIRM"; test "$FS_INSTALL_TRACE" = 'verify blank-da0'
# Test physical classification and exact partial-MiB readback on ordinary files.
. ./freebsd.sh
. ./media.sh
FS_WORK=$(mktemp -d)
trap 'rm -f "$FS_WORK/geom" "$FS_WORK/cam" "$FS_WORK/description" "$FS_WORK/file" "$FS_WORK/expected" "$FS_WORK/read"; rmdir "$FS_WORK"' EXIT
fs_check_platform amd64 BIOS amd64 BIOS; fs_check_platform amd64 UEFI amd64 UEFI
fs_check_platform arm64 UEFI arm64 UEFI
refuse fs_check_platform amd64 BIOS amd64 UEFI; refuse fs_check_platform amd64 UEFI amd64 BIOS
refuse fs_check_platform arm64 BIOS arm64 BIOS; refuse fs_check_platform arm64 UEFI amd64 UEFI
refuse fs_check_platform amd64 UEFI arm64 UEFI; refuse fs_check_platform amd64 unknown amd64 unknown
(
	sysctl() { return 1; }
	kenv() { test "$1" = -q && test "$2" = efi-version && printf '%s\n' "$version"; }
	version=2.70; test "$(fs_boot_method arm64)" = UEFI
	version=unknown; refuse fs_boot_method arm64
	version=$(printf '2.70\nunknown'); refuse fs_boot_method arm64
	version=0.0; refuse fs_boot_method arm64
	kenv() { return 1; }; refuse fs_boot_method arm64
)
(
	mkdir "$FS_WORK/boot"; printf 'loader' > "$FS_WORK/boot/loader"
	printf 'console="efi,comconsole"\n' > "$FS_WORK/boot/loader.conf"
	# Only a synthetic root is writable. Verify the exact sysrc file, arguments and readback.
	sysrc() {
		[ "$1" = -f ] && [ "$2" = "$FS_WORK/boot/loader.conf" ] || return 1
		case "$3" in
			console=vidconsole,comconsole) printf 'vidconsole,comconsole\n' > "$2" ;;
			-n) [ "$4" = console ] && [ "$misread" = no ] && cat "$2" ;;
			*) return 1 ;;
		esac
	}
	misread=no; FS_INSTALL_FIRMWARE=UEFI; fs_configure_target_boot "$FS_WORK"
	test "$(cat "$FS_WORK/boot/loader.conf")" = 'console="efi,comconsole"'
	FS_INSTALL_FIRMWARE=BIOS; fs_configure_target_boot "$FS_WORK"
	test "$(cat "$FS_WORK/boot/loader.conf")" = vidconsole,comconsole
	misread=yes; refuse fs_configure_target_boot "$FS_WORK"; misread=no
	rm "$FS_WORK/boot/loader.conf"; ln -s loader "$FS_WORK/boot/loader.conf"
	refuse fs_configure_target_boot "$FS_WORK"
	rm "$FS_WORK/boot/loader.conf"; touch "$FS_WORK/boot/loader.conf"
	rm "$FS_WORK/boot/loader"; ln -s ../bootloader "$FS_WORK/boot/loader"
	printf 'outside' > "$FS_WORK/bootloader"; refuse fs_configure_target_boot "$FS_WORK"
	rm "$FS_WORK/boot/loader" "$FS_WORK/bootloader" "$FS_WORK/boot/loader.conf"; rmdir "$FS_WORK/boot"
)
printf '0 DISK da0 1000 512\n1 PART da0p1 100 512\n2 LABEL msdosfs/FLEET 100 512\n0 DISK cd0 2000 2048\n1 LABEL iso9660/FREESENSE_INSTALL 2000 2048\n' > "$FS_WORK/geom"
test "$(fs_owner msdosfs/FLEET)" = da0; test "$(fs_owner cd0)" = cd0; refuse fs_owner other
printf 'scbus0 on mpt0 bus 0:\n<disk> at scbus0 target 0 lun 0 (pass0,da0)\nscbus1 on umass-sim0 bus 0:\n<usb> at scbus1 target 0 lun 0 (da1,pass1)\n' > "$FS_WORK/cam"
test "$(fs_cam_bus da0)" = mpt0; test "$(fs_cam_bus da1)" = umass-sim0; refuse fs_cam_bus da3
printf '   ident: (null)\n   lunid: 0x0000\n' > "$FS_WORK/description"; refuse fs_disk_identity "$FS_WORK/description"
printf '   ident: \n   lunid: 001-AB CD\n' > "$FS_WORK/description"; test "$(fs_disk_identity "$FS_WORK/description")" = '001-AB CD'
printf '   Mediasize: 8589934592 (8G)\n   Sectorsize: 512\n   Mode: r0w0e0\n   descr: Test Disk\n' > "$FS_WORK/description"
test "$(fs_disk_fields "$FS_WORK/description")" = "$(printf 'Test Disk\t8589934592')"
printf '   Mode: r1w0e0\n' >> "$FS_WORK/description"; refuse fs_disk_fields "$FS_WORK/description"
(
	cp "$FS_WORK/geom" "$FS_WORK/original-geom"
	cat > "$FS_WORK/geom" <<'GEOM'
0 DISK da0 524288 512 hd 255
1 LABEL iso9660/FREESENSE_INSTALL 524288 512 i 0 o 0
2 PART iso9660/FREESENSE_INSTALLp3 8192 512 i 3 o 65536 ty ms-basic-data xs GPT
1 LABEL diskid/DISK-123 524288 512 i 0 o 0
2 PART diskid/DISK-123p3 8192 512 i 3 o 65536 ty ms-basic-data xs GPT
1 PART da0p3 8192 512 i 3 o 65536 ty ms-basic-data xs GPT
2 LABEL msdosfs/FSSETUP 8192 512 i 0 o 0
0 DISK da1 8388608 512
GEOM
	test "$(fs_volume_names da0)" = "$(printf 'da0\nda0p3')"
	# A nested table below an actual primary partition is a different source layout.
	printf '0 DISK da0 524288 512\n1 PART da0p1 8192 512\n2 PART da0p1s1 2048 512\n' > "$FS_WORK/geom"
	refuse fs_volume_names da0
	printf '0 DISK da0 524288 512\n1 LABEL diskid/DISK-123 524288 512\n2 PART diskid/DISK-123p2 2048 512\n' > "$FS_WORK/geom"
	refuse fs_volume_names da0
	mv "$FS_WORK/original-geom" "$FS_WORK/geom"
)
dd if=/dev/zero of="$FS_WORK/expected" bs=1048576 count=1 2>/dev/null
printf 'x' | dd of="$FS_WORK/expected" bs=1 seek=1049087 conv=notrunc 2>/dev/null
cp "$FS_WORK/expected" "$FS_WORK/file"; printf 'trailing-bytes' >> "$FS_WORK/file"
fs_read_extent "$FS_WORK/file" 1049088 > "$FS_WORK/read"; cmp "$FS_WORK/expected" "$FS_WORK/read"
# Exercise the media scanner with synthetic mounted volumes and CAM/GEOM evidence.
(
	mkdir -m 700 "$FS_WORK/boot" "$FS_WORK/fields" "$FS_WORK/seedfields" "$FS_WORK/volume"
	mount() { printf '/dev/iso9660/FREESENSE_INSTALL / cd9660 ro 0 0\n'; }
	glabel() { printf 'iso9660/FREESENSE_INSTALL N/A cd0\n'; }
	fake_sha() { cksum "$1" | cut -d ' ' -f1; }
	sha256() { fake_sha "$2"; }
	# Compatible size query for ordinary test files.
	stat() { wc -c < "$3"; }
	fs_media_volumes() { [ "$1" != da1 ] || fs_source_file "$FS_WORK/volume" da1p1; }
	guided_parse_plan() { [ "$(cat "$1")" = good ] || return 1; printf '%s' matched > "$2/target_id"; printf '%s' machine > "$2/machine_uuid"; }
	guided_preview_seed() { printf '%s' example > "$2/fleet_seed"; printf '%s' attended > "$2/fleet_mode"; printf '%s' tunnel > "$2/transport"; }
	FS_DISKS='cd0 da0 da1'
	fs_scan_media "$FS_WORK/boot"
	test "$FS_INSTALL_SEED_STATUS" = none && test "$FS_INSTALL_PLAN_STATUS" = absent
	(
		FS_INSTALL_FIRMWARE=BIOS; fs_scan_media "$FS_WORK/boot"
		glabel() { printf 'iso9660/FREESENSE_INSTALL N/A da0\n'; }
		refuse fs_scan_media "$FS_WORK/boot"
	)
	grep 'cd0' "$FS_WORK/protected" >/dev/null; grep 'da1' "$FS_WORK/protected" >/dev/null; refuse grep 'da0' "$FS_WORK/protected"
	printf 'good\n' > "$FS_WORK/volume/freesense-install.conf"; fs_scan_media "$FS_WORK/boot"; test "$FS_INSTALL_PLAN_STATUS" = ready
	printf 'bad\n' > "$FS_WORK/volume/freesense-install.conf"; refuse fs_scan_media "$FS_WORK/boot"
	printf 'good\n' > "$FS_WORK/volume/freesense-install.conf"; touch "$FS_WORK/volume/fleet-join.conf"; refuse fs_scan_media "$FS_WORK/boot"
	rm "$FS_WORK/volume/freesense-install.conf"; fs_scan_media "$FS_WORK/boot"; test "$FS_INSTALL_SEED_STATUS" = protected
	mkdir "$FS_WORK/volume/config"; touch "$FS_WORK/volume/config/fleet-join.conf"; refuse fs_scan_media "$FS_WORK/boot"
	rm "$FS_WORK/volume/config/fleet-join.conf" "$FS_WORK/volume/fleet-join.conf"
	ln -s missing "$FS_WORK/volume/freesense-install.conf"; refuse fs_scan_media "$FS_WORK/boot"
	rm "$FS_WORK/volume/freesense-install.conf"; rmdir "$FS_WORK/volume/config" "$FS_WORK/volume"
	FS_DISKS='da2 cd0'; refuse fs_scan_media "$FS_WORK/boot"
	find "$FS_WORK/fields" "$FS_WORK/seedfields" -type f -delete
	rmdir "$FS_WORK/boot" "$FS_WORK/fields" "$FS_WORK/seedfields"
	rm -f "$FS_WORK/seed" "$FS_WORK/plan" "$FS_WORK/mounts" "$FS_WORK/labels" "$FS_WORK/protected" "$FS_WORK/sources"
)
printf '%s\n' 'Verified diskless guided and unattended installer safety cases.'

