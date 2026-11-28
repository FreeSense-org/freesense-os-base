# shellcheck disable=SC2034
# State variables are shared by the sourced installer modules.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# Read-only source discovery. All temporary mountpoints belong to the private workspace.
fs_owner() {
	awk -v provider="$1" '
		$1 == 0 {owner=($2=="DISK" ? $3 : "")}
		$3==provider && owner!="" {if (found && owner!=out) bad=1; found=1; out=owner}
		END {if (!found || bad) exit 1; print out}
	' "$FS_WORK/geom"
}

fs_source_file() {
	# Input root comes only from the verified root or this process's read-only mounts.
	for fs_rel in freesense-install.conf fleet-join.conf config/fleet-join.conf; do
		fs_file=$1/$fs_rel
		if [ "$fs_rel" = config/fleet-join.conf ] && [ -L "$1/config" ]; then return 1; fi
		[ -e "$fs_file" ] || [ -L "$fs_file" ] || continue
		case "$fs_rel" in freesense-install.conf) fs_slot=plan; fs_limit=69632 ;; *) fs_slot=seed; fs_limit=65536 ;; esac
		[ -f "$fs_file" ] && [ ! -L "$fs_file" ] && [ "$(stat -f %z "$fs_file")" -le "$fs_limit" ] || return 1
		[ ! -e "$FS_WORK/$fs_slot" ] || return 1
		cp "$fs_file" "$FS_WORK/$fs_slot" && chmod 600 "$FS_WORK/$fs_slot" || return 1
		printf '%s\n' "$2/$fs_rel" >> "$FS_WORK/sources" || return 1
	done
}

fs_media_mount() {
	case "$2" in msdosfs|cd9660) ;; *) return 1 ;; esac
	mount -t "$2" -o ro,nosuid,noexec "/dev/$1" "$FS_WORK/media" >/dev/null 2>&1 || return 1
	fs_mounted=1
	fs_result=0
	fs_source_file "$FS_WORK/media" "$1" || fs_result=1
	umount "$FS_WORK/media" || return 1
	fs_mounted=0
	return "$fs_result"
}

fs_volume_names() {
	# GEOM can taste the same GPT through its ISO and diskid labels. Emit physical
	# providers only. Each alias must describe a matching direct partition.
	awk -v disk="$1" '
		$1==0 {owner=$3; class[0]=$2; if(owner==disk && $2=="DISK") roots++; next}
		owner!=disk {next}
		{level=$1; class[level]=$2}
		$2=="PART" {
			spec=$4 " " $5
			for(i=6;i<=NF;i++) spec=spec " " $i
			if(level==1 && $3~("^"disk"[ps][0-9]+$") && !physical[spec]++) {
				names[++total]=$3
			} else if(level==2 && class[1]=="LABEL") {
				aliases[spec]=1
			} else bad=1
		}
		END {
			for(spec in aliases) if(!(spec in physical)) bad=1
			if(bad || roots!=1) exit 1
			print disk
			for(i=1;i<=total;i++) print names[i]
		}' "$FS_WORK/geom"
}

fs_media_volumes() {
	fs_volume_names "$1" > "$FS_WORK/volumes" || return 1
	fs_recognized=no
	while IFS= read -r fs_volume; do
		printf '%s\n' "$fs_volume" | grep -Eq '^(cd|da|ada|nda|nvd|vtbd)[0-9]+([ps][0-9]+)?$' || return 1
		[ "$fs_volume" != "$FS_ROOT_PROVIDER" ] || continue
		# A hybrid USB may expose the mounted ISO through both its disk and a partition.
		if ! fs_type=$(fstyp "/dev/$fs_volume" 2>/dev/null); then
			# Partition tables are not mountable filesystems. A secondary partition
			# that cannot be identified may hide a competing provision source.
			[ "$1" = "$FS_BOOT_DISK" ] || [ "$fs_volume" = "$1" ] || return 1
			continue
		fi
		case "$fs_type" in
			iso9660|cd9660) [ "$1" != "$FS_BOOT_DISK" ] || continue; fs_type=cd9660 ;;
			msdosfs) ;;
			*) [ "$1" = "$FS_BOOT_DISK" ] || return 1; continue ;;
		esac
		fs_media_mount "$fs_volume" "$fs_type" || return 1
		fs_recognized=yes
	done < "$FS_WORK/volumes"
	if [ "$1" != "$FS_BOOT_DISK" ] && [ "$fs_recognized" = no ]; then
		# Empty optical drive is allowed. Unreadable removable storage fails closed.
		case "$1" in cd[0-9]*) geom disk list "$1" | awk '$1=="Mediasize:" && $2=="0" {empty=1} END {exit !empty}' ;; *) return 1 ;; esac
	fi
}

fs_cam_bus() {
	# CAM's transport is authoritative for USB mass storage. Unknown da transports never become targets.
	awk -v name="$1" '
		/^scbus[-0-9]+ on / {bus=$3}
		/[(][a-z0-9,]+[)]$/ {
			dev=$0; sub(/^.*[(]/,"",dev); sub(/[)]$/,"",dev)
			n=split(dev,a,","); for(i=1;i<=n;i++) if(a[i]==name) {print bus; found++}
		}
		END {exit found!=1}
	' "$FS_WORK/cam"
}

fs_scan_media() {
	FS_INSTALL_SEED_STATUS=ambiguous
	FS_INSTALL_PLAN_STATUS=invalid
	: > "$FS_WORK/sources" || return 1
	rm -f "$FS_WORK/plan" "$FS_WORK/seed" &&
		find "$FS_WORK/fields" "$FS_WORK/seedfields" -type f -delete || return 1
	# Match the mounted read-only optical filesystem and resolve the exact label to one GEOM disk.
	mount -p > "$FS_WORK/mounts" && glabel status -s > "$FS_WORK/labels" || return 1
	awk '$2=="/" && $1=="/dev/iso9660/FREESENSE_INSTALL" && $3=="cd9660" && $4~/(^|,)ro(,|$)/ {n++} END {exit n!=1}' "$FS_WORK/mounts" || return 1
	FS_ROOT_PROVIDER=$(awk '$1=="iso9660/FREESENSE_INSTALL" {print $NF; n++} END {exit n!=1}' "$FS_WORK/labels") || return 1
	FS_BOOT_DISK=$(fs_owner "$FS_ROOT_PROVIDER") || return 1
	if [ "$FS_INSTALL_FIRMWARE" = BIOS ]; then
		printf '%s\n' "$FS_BOOT_DISK" | grep -Eq '^cd[0-9]+$' || {
			printf '%s\n' 'The BIOS installer requires an optical boot drive.' >&2; return 1
		}
	fi
	printf 'boot %s %s\n' "$FS_ROOT_PROVIDER" "$FS_BOOT_DISK" >> "$FS_WORK/sources" || return 1
	fs_source_file "$1" "$FS_ROOT_PROVIDER" || return 1
	: > "$FS_WORK/protected" || return 1
	for fs_dev in $FS_DISKS; do
		fs_external=no
		case "$fs_dev" in
			cd[0-9]*) fs_external=yes ;;
			da[0-9]*)
				fs_bus=$(fs_cam_bus "$fs_dev") || return 1
				case "$fs_bus" in
					umass*|uas*) fs_external=yes ;;
					mpt[0-9]*|mpr[0-9]*|mps[0-9]*|ahd[0-9]*|vtscsi[0-9]*|isci[0-9]*|pvscsi[0-9]*|mrsas[0-9]*|smartpqi[0-9]*) ;;
					*) return 1 ;;
				esac ;;
		esac
		if [ "$fs_dev" = "$FS_BOOT_DISK" ]; then
			printf '%s\tInstaller boot medium\n' "$fs_dev" >> "$FS_WORK/protected"
		elif [ "$fs_external" = yes ]; then
			printf '%s\tRead-only removable medium\n' "$fs_dev" >> "$FS_WORK/protected"
		fi
		if [ "$fs_external" = yes ] || [ "$fs_dev" = "$FS_BOOT_DISK" ]; then
			fs_media_volumes "$fs_dev" || return 1
		fi
	done
	FS_INSTALL_SEED_STATUS=none
	FS_INSTALL_PLAN_STATUS=absent
	FS_INSTALL_FLEET_MODE=''
	FS_INSTALL_TRANSPORT=''
	FS_INSTALL_PLAN_LAN=''
	if [ -f "$FS_WORK/plan" ]; then
		guided_parse_plan "$FS_WORK/plan" "$FS_WORK/fields" || { FS_INSTALL_PLAN_STATUS=invalid; return 1; }
		FS_INSTALL_PLAN_ID=$(cat "$FS_WORK/fields/target_id") && FS_INSTALL_PLAN_UUID=$(cat "$FS_WORK/fields/machine_uuid") || return 1
		FS_INSTALL_PLAN_STATUS=ready
		[ ! -f "$FS_WORK/fields/lan" ] || FS_INSTALL_PLAN_LAN=$(cat "$FS_WORK/fields/lan") || return 1
	fi
	if [ -f "$FS_WORK/seed" ]; then
		# Unattended Fleet enrollment must be explicitly included by the installation plan.
		[ "$FS_INSTALL_PLAN_STATUS" = absent ] || { FS_INSTALL_SEED_STATUS=ambiguous; return 1; }
		guided_preview_seed "$FS_WORK/seed" "$FS_WORK/seedfields" || { FS_INSTALL_SEED_STATUS=ambiguous; return 1; }
		for fs_name in fleet_seed fleet_mode transport; do
			[ ! -f "$FS_WORK/seedfields/$fs_name" ] || mv "$FS_WORK/seedfields/$fs_name" "$FS_WORK/fields/$fs_name" || return 1
		done
	fi
	if [ -f "$FS_WORK/fields/fleet_seed" ]; then
		FS_INSTALL_SEED_STATUS=protected
		FS_INSTALL_FLEET_MODE=$(cat "$FS_WORK/fields/fleet_mode") && FS_INSTALL_TRANSPORT=$(cat "$FS_WORK/fields/transport") || return 1
	fi
	# Fingerprint content without ever displaying, passing or exporting it.
	for fs_input in "$FS_WORK/plan" "$FS_WORK/seed"; do
		[ ! -f "$fs_input" ] || sha256 -q "$fs_input" >> "$FS_WORK/sources" || return 1
	done
	FS_INSTALL_SOURCE_STAMP=$(sha256 -q "$FS_WORK/sources") || return 1
}

