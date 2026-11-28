# shellcheck disable=SC2034
# State variables are shared by the sourced installer modules.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# The adapter supplies hardware inventory, private plan storage and disk operations.

guided_fail() {
	FS_INSTALL_ERROR=$1
	FS_INSTALL_STATE=failed
	FS_INSTALL_CONFIRM=''
	fs_adapter_cleanup || :
	return 1
}

guided_scan() {
	FS_INSTALL_TARGETS=''
	FS_INSTALL_EXCLUSIONS=''
	FS_INSTALL_SEED_STATUS=none
	FS_INSTALL_PLAN_STATUS=absent
	FS_INSTALL_SOURCE_STAMP=''
	FS_INSTALL_PLAN_ID=''
	FS_INSTALL_PLAN_UUID=''
	FS_INSTALL_MACHINE_UUID=''
	fs_adapter_scan || return 1
	[ "$FS_INSTALL_SEED_STATUS" != ambiguous ] && [ "$FS_INSTALL_PLAN_STATUS" != invalid ] || return 1
	# Never accept duplicate provider names or physical identities as a selectable target.
	printf '%s\n' "$FS_INSTALL_TARGETS" | LC_ALL=C awk -F '\t' '
		NF == 0 {next}
		NF != 4 || $1 !~ /^(ada|da|nda|nvd|vtbd)[0-9]+$/ ||
		  $2 !~ /^[A-Za-z0-9][A-Za-z0-9_.: -]+$/ || $2 ~ / $/ || length($2)>128 ||
		  $3 !~ /^[ -~]+$/ || $3 ~ /[|]/ || length($3)>128 ||
		  $4 !~ /^[1-9][0-9]*$/ || length($4)>18 || name[$1]++ || id[$2]++ {exit 1}
	'
}

guided_preflight() {
	case ${FS_INSTALL_STATE:-new} in new) ;; *) return 1 ;; esac
	FS_INSTALL_ERROR=''
	FS_INSTALL_USE_SEED=no
	FS_INSTALL_LAN=''
	fs_adapter_init || { guided_fail 'Could not initialize the private installer workspace.'; return 1; }
	guided_scan || { guided_fail 'Hardware or provisioning media could not be verified.'; return 1; }
	FS_INSTALL_INITIAL_STAMP=$FS_INSTALL_SOURCE_STAMP
	FS_INSTALL_INITIAL_UUID=$FS_INSTALL_MACHINE_UUID
	FS_INSTALL_STATE=select
	FS_INSTALL_CONFIRM=''
}

guided_find_target() {
	printf '%s\n' "$FS_INSTALL_TARGETS" | awk -F '\t' -v key="$1" -v col="$2" '("id:" $col) == ("id:" key) {print; n++} END {exit n!=1}'
}

guided_select() {
	[ "$FS_INSTALL_STATE" = select ] || return 1
	FS_INSTALL_SELECTED=$(guided_find_target "$1" 1) || return 1
	FS_INSTALL_DISK=$1
	FS_INSTALL_ID=$(printf '%s\n' "$FS_INSTALL_SELECTED" | cut -f2)
	FS_INSTALL_CAPACITY=$(printf '%s\n' "$FS_INSTALL_SELECTED" | cut -f4)
	FS_INSTALL_CONFIRM=$(printf '%s\n' "$FS_INSTALL_SELECTED" | awk -F '\t' '{print "INSTALL /dev/" $1 " | " $2 " | " $3 " | " $4}')
	FS_INSTALL_STATE=review
}

guided_back() {
	[ "$FS_INSTALL_STATE" = review ] || return 1
	FS_INSTALL_CONFIRM=''
	FS_INSTALL_SELECTED=''
	FS_INSTALL_USE_SEED=no
	FS_INSTALL_STATE=select
}

guided_cancel() {
	case "$FS_INSTALL_STATE" in select|review) ;; *) return 1 ;; esac
	FS_INSTALL_CONFIRM=''
	FS_INSTALL_USE_SEED=no
	FS_INSTALL_STATE=cancelled
	fs_adapter_cleanup
}

guided_binding() {
	[ "$FS_INSTALL_PLAN_STATUS" = ready ] && [ -n "$FS_INSTALL_MACHINE_UUID" ] &&
		[ "$FS_INSTALL_PLAN_UUID" = "$FS_INSTALL_MACHINE_UUID" ] && [ "$FS_INSTALL_PLAN_ID" = "$FS_INSTALL_ID" ]
}

guided_recheck() {
	guided_scan && [ "$FS_INSTALL_SOURCE_STAMP" = "$FS_INSTALL_INITIAL_STAMP" ] &&
		[ "$FS_INSTALL_MACHINE_UUID" = "$FS_INSTALL_INITIAL_UUID" ] &&
		[ "$(guided_find_target "$FS_INSTALL_DISK" 1)" = "$FS_INSTALL_SELECTED" ] || return 1
	case "$1" in
		plan) guided_binding ;;
		console) [ "$FS_INSTALL_PLAN_STATUS" = absent ] ;;
		*) return 1 ;;
	esac
}

guided_commit() {
	FS_INSTALL_STATE=verifying
	guided_recheck "$1" || { guided_fail 'Target, boot media, machine or provisioning source changed.'; return 1; }
	if [ "$1" = console ] && [ "$FS_INSTALL_USE_SEED" = yes ] && [ "$FS_INSTALL_SEED_STATUS" != protected ]; then
		guided_fail 'Selected Fleet seed is unavailable.'; return 1
	fi
	fs_lan=$FS_INSTALL_LAN
	if [ "$1" = plan ]; then fs_lan=${FS_INSTALL_PLAN_LAN:-}; fi
	if [ -n "$fs_lan" ]; then
		if ! printf '%s\n' "$fs_lan" | grep -Eq '^[a-z][a-z0-9]{0,12}[0-9]$'; then
			guided_fail 'Invalid LAN interface.'; return 1
		fi
		case " $FS_INSTALL_INTERFACES " in *" $fs_lan "*) ;; *) guided_fail 'Selected LAN interface is unavailable.'; return 1 ;; esac
	fi
	fs_adapter_verify_payload || { guided_fail 'Payload checksum or expanded size failed verification.'; return 1; }
	fs_adapter_blank "$FS_INSTALL_DISK" "$FS_INSTALL_CAPACITY" || { guided_fail 'Target contains data or could not be read completely.'; return 1; }
	# Repeat the fast identity checks after the two full reads, immediately before opening the disk for writing.
	guided_recheck "$1" || { guided_fail 'Identity or provisioning data changed during verification.'; return 1; }
	FS_INSTALL_STATE=writing
	fs_adapter_write "$FS_INSTALL_DISK" || { guided_fail 'Disk write failed. Installation is incomplete.'; return 1; }
	FS_INSTALL_STATE=readback
	fs_adapter_readback "$FS_INSTALL_DISK" "$FS_INSTALL_CAPACITY" || { guided_fail 'Disk readback or GPT verification failed.'; return 1; }
	if [ "$FS_INSTALL_FIRMWARE" = BIOS ] || [ "$1" = plan ] || [ "$FS_INSTALL_USE_SEED" = yes ] || [ -n "$FS_INSTALL_LAN" ]; then
		fs_adapter_handoff "$FS_INSTALL_DISK" "$1" || { guided_fail 'Installed boot or provisioning setup failed. Installation is incomplete.'; return 1; }
	fi
	fs_adapter_cleanup || { guided_fail 'Installer workspace cleanup failed.'; return 1; }
	FS_INSTALL_STATE='done'
	FS_INSTALL_CONFIRM=''
}

guided_install() {
	[ "$FS_INSTALL_STATE" = review ] && [ "$FS_INSTALL_PLAN_STATUS" = absent ] &&
		[ -n "$FS_INSTALL_CONFIRM" ] && [ "$1" = "$FS_INSTALL_CONFIRM" ] || return 1
	guided_commit console
}

guided_unattended() {
	[ "$FS_INSTALL_STATE" = select ] && [ "$FS_INSTALL_PLAN_STATUS" = ready ] || return 1
	[ -n "$FS_INSTALL_MACHINE_UUID" ] && [ "$FS_INSTALL_PLAN_UUID" = "$FS_INSTALL_MACHINE_UUID" ] ||
		{ guided_fail 'Installation plan names a different machine.'; return 1; }
	fs_plan_row=$(guided_find_target "$FS_INSTALL_PLAN_ID" 2) ||
		{ guided_fail 'Installation plan does not identify a unique eligible disk.'; return 1; }
	if ! guided_select "$(printf '%s\n' "$fs_plan_row" | cut -f1)" || ! guided_binding; then
		guided_fail 'Installation plan binding failed.'; return 1
	fi
	guided_commit plan
}

