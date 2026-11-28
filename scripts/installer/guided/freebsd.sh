# shellcheck disable=SC2034
# State variables are shared by the sourced installer modules.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# Boot-media adapter for the generic FreeBSD installer. Only this adapter opens devices.
fs_check_platform() {
	[ "$1" = "$3" ] && [ "$2" = "$4" ] || return 1
	case "$1/$2" in amd64/BIOS|amd64/UEFI|arm64/UEFI) return 0 ;; *) return 1 ;; esac
}

fs_boot_method() {
	if [ "$1" = arm64 ]; then
		# FreeBSD's arm64 EFI loader exports efi-version. machdep.bootmethod is x86-specific.
		fs_efi_version=$(kenv -q efi-version) || return 1
		case "$fs_efi_version" in ''|*[!0-9.]*) return 1 ;; esac
		printf '%s\n' "$fs_efi_version" | grep -Eq '^[1-9][0-9]{0,4}\.[0-9]{1,5}$' || return 1
		printf '%s\n' UEFI
	else
		sysctl -n machdep.bootmethod 2>/dev/null || kenv -q machdep.bootmethod
	fi
}

fs_adapter_init() {
	export PATH=/sbin:/bin:/usr/sbin:/usr/bin LC_ALL=C
	# FreeBSD sh supports pipefail, including short/error reads on disk pipelines.
	# shellcheck disable=SC3040
	set -o pipefail
	umask 077
	[ "$(id -u)" = 0 ] || return 1
	FS_INSTALL_FIRMWARE=$(cat /freesense/firmware) || return 1
	FS_INSTALL_ARCH=$(cat /freesense/architecture) && fs_machine=$(uname -m) &&
		fs_method=$(fs_boot_method "$fs_machine") || return 1
	fs_check_platform "$fs_machine" "$fs_method" "$FS_INSTALL_ARCH" "$FS_INSTALL_FIRMWARE" || {
		printf '%s\n' 'Use the installer profile that matches the machine architecture and firmware.' >&2; return 1
	}
	FS_WORK=$(mktemp -d /tmp/freesense-install.XXXXXXXX) || return 1
	fs_mounted=0
	fs_target_mounted=0
	mkdir -m 700 "$FS_WORK/media" "$FS_WORK/root" "$FS_WORK/fields" "$FS_WORK/seedfields" || return 1
	FS_IMAGE=/freesense/payload.raw.xz
	FS_RAW_BYTES=$(cat /freesense/raw.bytes) && FS_RAW_HASH=$(cat /freesense/raw.sha256) && FS_XZ_HASH=$(cat /freesense/xz.sha256) || return 1
	printf '%s\n' "$FS_RAW_BYTES" | grep -Eq '^[1-9][0-9]{2,17}$' && [ $((FS_RAW_BYTES % 512)) = 0 ] || return 1
	printf '%s\n' "$FS_RAW_HASH" | grep -Eq '^[0-9a-f]{64}$' && printf '%s\n' "$FS_XZ_HASH" | grep -Eq '^[0-9a-f]{64}$'
}

fs_adapter_cleanup() {
	[ -n "${FS_WORK:-}" ] || return 0
	case "$FS_WORK" in /tmp/freesense-install.????????) ;; *) return 1 ;; esac
	if [ "${fs_mounted:-0}" = 1 ]; then umount "$FS_WORK/media" || return 1; fs_mounted=0; fi
	if [ "${fs_target_mounted:-0}" = 1 ]; then umount "$FS_WORK/root" || return 1; fs_target_mounted=0; fi
	# Never recurse into a still-mounted filesystem or remove an unrecognized path.
	rmdir "$FS_WORK/media" "$FS_WORK/root" || return 1
	find "$FS_WORK" -type f -delete && rmdir "$FS_WORK/seedfields" "$FS_WORK/fields" "$FS_WORK" || return 1
	FS_WORK=''
}

fs_disk_identity() {
	awk '
		$1=="ident:" {sub(/^[[:space:]]*ident:[[:space:]]*/, ""); id=$0}
		$1=="lunid:" {sub(/^[[:space:]]*lunid:[[:space:]]*/, ""); lun=$0}
		function valid(v) {return length(v)>=2 && length(v)<=128 && v~/^[A-Za-z0-9][A-Za-z0-9_.: -]+$/ && v!~/ $/ && tolower(v)!~/^(geom-|unknown$|none$|null$|0x0+$|0+$|f+$)/}
		END {if (!valid(id)) id=lun; if (!valid(id)) exit 1; print id}
	' "$1"
}

fs_disk_fields() {
	awk -v provider="${2:-}" '
		$1=="Mediasize:" {bytes=$2} $1=="Sectorsize:" {sector=$2} $1=="Mode:" {mode=$2}
		$1=="descr:" {sub(/^[[:space:]]*descr:[[:space:]]*/, ""); model=$0}
		END {
			# The virtio-blk driver exports its serial but may omit the model.
			if ((model=="" || model=="(null)") && provider~/^vtbd[0-9]+$/) model="VirtIO block device"
			if (model!~/^[ -~]+$/ || model~/[|]/ || length(model)>128 || bytes!~/^[1-9][0-9]*$/ ||
			    length(bytes)>18 || sector!=512 || mode!="r0w0e0") exit 1
			printf "%s\t%s\n",model,bytes
		}' "$1"
}

fs_adapter_scan() {
	FS_DISKS=$(sysctl -n kern.disks) && [ -n "$FS_DISKS" ] && sysctl -n kern.geom.conftxt > "$FS_WORK/geom" &&
		camcontrol devlist -v > "$FS_WORK/cam" || return 1
	for fs_dev in $FS_DISKS; do printf '%s\n' "$fs_dev" | grep -Eq '^[a-z][a-z0-9]*[0-9]$' || return 1; done
	fs_scan_media / || return 1
	fs_uuid=$(kenv -q smbios.system.uuid 2>/dev/null | tr A-F a-f) || fs_uuid=''
	if printf '%s\n' "$fs_uuid" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
		case "$fs_uuid" in 00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff) ;; *) FS_INSTALL_MACHINE_UUID=$fs_uuid ;; esac
	fi
	FS_INSTALL_INTERFACES=''
	fs_interfaces=$(ifconfig -l) || return 1
	for fs_if in $fs_interfaces; do
		case "$fs_if" in lo[0-9]*|enc[0-9]*|pflog*|pfsync*|bridge*|vlan*|lagg*|tun*|tap*) continue ;; esac
		printf '%s\n' "$fs_if" | grep -Eq '^[a-z][a-z0-9]{0,12}[0-9]$' || continue
		if ifconfig "$fs_if" | grep -Eq '^[[:space:]]+ether [0-9a-f:]+$'; then
			FS_INSTALL_INTERFACES="${FS_INSTALL_INTERFACES:+$FS_INSTALL_INTERFACES }$fs_if"
		fi
	done
	: > "$FS_WORK/targets" || return 1
	: > "$FS_WORK/identities" || return 1
	cp "$FS_WORK/protected" "$FS_WORK/exclusions" || return 1
	for fs_dev in $FS_DISKS; do
		case "$fs_dev" in da[0-9]*|ada[0-9]*|nda[0-9]*|nvd[0-9]*|vtbd[0-9]*) ;; *) continue ;; esac
		geom disk list "$fs_dev" > "$FS_WORK/dev-$fs_dev" || return 1
		if fs_id=$(fs_disk_identity "$FS_WORK/dev-$fs_dev"); then printf '%s\t%s\n' "$fs_dev" "$fs_id" >> "$FS_WORK/identities" || return 1; fi
	done
	for fs_dev in $FS_DISKS; do
		if awk -F '\t' -v d="$fs_dev" '$1==d {seen=1} END {exit !seen}' "$FS_WORK/protected"; then continue; fi
		fs_reason=''
		case "$fs_dev" in da[0-9]*|ada[0-9]*|nda[0-9]*|nvd[0-9]*|vtbd[0-9]*) ;; *) fs_reason='Unsupported storage provider' ;; esac
		if [ -z "$fs_reason" ]; then
			if [ ! -c "/dev/$fs_dev" ] || [ -L "/dev/$fs_dev" ]; then fs_reason='Invalid device node'
			elif gpart show "$fs_dev" >/dev/null 2>&1; then fs_reason='Existing partition table'
			elif awk -v d="$fs_dev" '$1==0 {base=$3} base==d && $1>0 && !($2=="LABEL" && $3~/^diskid\//) {found=1} END {exit !found}' "$FS_WORK/geom"; then fs_reason='Existing storage consumer'
			elif ! fs_id=$(fs_disk_identity "$FS_WORK/dev-$fs_dev"); then fs_reason='Missing trusted disk identity'
			elif ! awk -F '\t' -v id="$fs_id" '("id:"$2)==("id:"id) {n++} END {exit n!=1}' "$FS_WORK/identities"; then fs_reason='Duplicate device identity'
			elif ! fs_fields=$(fs_disk_fields "$FS_WORK/dev-$fs_dev" "$fs_dev"); then fs_reason='Unsupported geometry or active disk'
			elif [ "$(printf '%s\n' "$fs_fields" | cut -f2)" -lt "$FS_RAW_BYTES" ]; then fs_reason='Insufficient disk capacity'
			else printf '%s\t%s\t%s\n' "$fs_dev" "$fs_id" "$fs_fields" >> "$FS_WORK/targets" || return 1
			fi
		fi
		[ -z "$fs_reason" ] || printf '%s\t%s\n' "$fs_dev" "$fs_reason" >> "$FS_WORK/exclusions" || return 1
	done
	FS_INSTALL_TARGETS=$(cat "$FS_WORK/targets") && FS_INSTALL_EXCLUSIONS=$(cat "$FS_WORK/exclusions")
}

fs_adapter_verify_payload() {
	printf '%s\n' 'Verifying compressed and expanded payload.'
	fs_packed=$(sha256 -q "$FS_IMAGE") && [ "$fs_packed" = "$FS_XZ_HASH" ] &&
		fs_raw=$(xz -dc "$FS_IMAGE" | sha256 -q) && [ "$fs_raw" = "$FS_RAW_HASH" ] || return 1
	fs_expanded=$(xz -dc "$FS_IMAGE" | wc -c) && [ "$fs_expanded" -eq "$FS_RAW_BYTES" ]
}

fs_read_extent() {
	# Payload and eligible devices use 512-byte sectors. Read most bytes in MiB blocks.
	fs_blocks=$(($2 / 1048576))
	fs_tail=$(($2 % 1048576 / 512))
	dd if="$1" bs=1048576 count="$fs_blocks" 2>/dev/null || return 1
	[ "$fs_tail" -eq 0 ] || dd if="$1" bs=512 skip="$((fs_blocks * 2048))" count="$fs_tail" 2>/dev/null
}

fs_is_blank() {
	fs_zero=$(fs_read_extent /dev/zero "$2" | sha256 -q) && fs_contents=$(dd if="$1" bs=1048576 2>/dev/null | sha256 -q) &&
		[ "$fs_zero" = "$fs_contents" ]
}

fs_adapter_blank() {
	printf '%s\n' 'Verifying every byte of the target is zero.'
	fs_is_blank "/dev/$1" "$2"
}

fs_adapter_write() {
	printf '%s\n' 'Writing verified disk image.'
	xz -dc "$FS_IMAGE" | dd of="/dev/$1" ibs=64k obs=1m || return 1
	sync
}

fs_adapter_readback() {
	printf '%s\n' 'Verifying on-disk bytes.'
	fs_actual=$(fs_read_extent "/dev/$1" "$FS_RAW_BYTES" | sha256 -q) && [ "$fs_actual" = "$FS_RAW_HASH" ] || return 1
	printf 'FREESENSE_RAW_READBACK %s %s\n' "$FS_RAW_BYTES" "$fs_actual"
	if [ "$2" -gt "$FS_RAW_BYTES" ]; then
		fs_layout=$(gpart backup "$1") || return 1
		[ "$(printf '%s\n' "$fs_layout" | awk 'NR==1 {print $1}')" = GPT ] || return 1
		gpart recover "$1" && sync && [ "$(gpart backup "$1")" = "$fs_layout" ] || return 1
		dd if="/dev/$1" bs=512 skip="$(($2 / 512 - 1))" count=1 of="$FS_WORK/last-sector" 2>/dev/null &&
			[ "$(head -c 8 "$FS_WORK/last-sector")" = 'EFI PART' ] || return 1
		printf 'FREESENSE_GPT_RECOVERED %s\n' "$1"
	fi
}

fs_configure_target_boot() {
	case "$FS_INSTALL_FIRMWARE" in UEFI) return 0 ;; BIOS) ;; *) return 1 ;; esac
	# The verified UFS payload supplies the BIOS loader. Preserve other settings
	# in its loader.conf, including the appliance boot service and serial speed.
	[ -d "$1/boot" ] && [ ! -L "$1/boot" ] &&
		[ -f "$1/boot/loader.conf" ] && [ ! -L "$1/boot/loader.conf" ] || return 1
	fs_loader=$(realpath "$1/boot/loader") || return 1
	case "$fs_loader" in "$1/boot/"*) [ -f "$fs_loader" ] && [ -s "$fs_loader" ] || return 1 ;; *) return 1 ;; esac
	sysrc -f "$1/boot/loader.conf" console=vidconsole,comconsole >/dev/null &&
		[ "$(sysrc -f "$1/boot/loader.conf" -n console)" = vidconsole,comconsole ]
}

fs_adapter_handoff() {
	# Resolve root inside the selected disk's GEOM tree. Labels may collide with other attached disks.
	fs_part=$(sysctl -n kern.geom.conftxt | awk -v d="$1" '$1==0 {disk=$3} disk==d && $1==1 && $2=="PART" {
		for(i=4;i<NF;i++) if($i=="ty" && $(i+1)=="freebsd-ufs") {print $3; found++}
	} END {exit found!=1}') || return 1
	printf '%s\n' "$fs_part" | grep -Eq "^$1[p][0-9]+$" || return 1
	fs_include=skip
	if [ "$2" = plan ] || [ "$FS_INSTALL_USE_SEED" = yes ]; then fs_include=include; fi
	if [ "$2" = console ] && [ -n "${FS_INSTALL_LAN:-}" ]; then printf '%s' "$FS_INSTALL_LAN" > "$FS_WORK/fields/lan" || return 1; fi
	mount -t ufs -o rw,noexec,nosuid "/dev/$fs_part" "$FS_WORK/root" || return 1
	fs_target_mounted=1
	fs_handoff=0
	fs_configure_target_boot "$FS_WORK/root" &&
		guided_apply_plan "$FS_WORK/fields" "$FS_WORK/root" "$fs_include" && sync || fs_handoff=1
	umount "$FS_WORK/root" || return 1
	fs_target_mounted=0
	return "$fs_handoff"
}

