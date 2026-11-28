#!/bin/sh
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# Presentation only. The storage backend rechecks every write condition.

wizard_screen() {
	[ -t 0 ] && [ -t 2 ] && [ -x /usr/bin/bsddialog ] &&
		[ -n "${TERM:-}" ] && [ "$TERM" != dumb ] || return 1
	wizard_rows=$(tput lines 2>/dev/null) && wizard_cols=$(tput cols 2>/dev/null) &&
		[ "$wizard_rows" -ge 24 ] && [ "$wizard_cols" -ge 78 ]
}

wizard_dialog() {
	/usr/bin/bsddialog --theme flat --ascii-lines --clear-screen --esc-exit-code 1 \
		--backtitle "FreeSense  /  System installation" --title " $wizard_title " "$@"
}

# Choice tags are also valid input on a plain serial terminal.
wizard_menu() {
	wizard_title=$1
	wizard_body=$2
	shift 2
	if [ "$wizard_display" = dialog ]; then
		wizard_dialog --output-fd 3 --ok-label Select --cancel-label Back \
			--menu "$wizard_body" 0 72 0 "$@" 3>&1 1>&2
		return $?
	fi
	printf '\nFreeSense  /  %s\n%s\n\n' "$wizard_title" "$wizard_body" >&2
	while [ "$#" -gt 0 ]; do
		printf '  %-12s  %s\n' "$1" "$2" >&2
		shift 2
	done
	printf '\nChoice (or back) > ' >&2
	IFS= read -r wizard_value || return 2
	case "$wizard_value" in back) return 1 ;; '') return 3 ;; esac
	printf '%s\n' "$wizard_value"
}

wizard_confirmation() {
	wizard_title='4 of 4  /  Confirm installation'
	wizard_body=$(printf '%s\n\n%s\n\n%s' \
		"Type this exact line to install on the selected blank disk." \
		"$FS_INSTALL_CONFIRM" \
		"Use Back to change your choices.")
	if [ "$wizard_display" = dialog ]; then
		wizard_dialog --output-fd 3 --ok-label Install --cancel-label Back \
			--inputbox "$wizard_body" 15 72 '' 3>&1 1>&2
		return $?
	fi
	printf '\nFreeSense  /  %s\n%s\n\nConfirm (or back) > ' "$wizard_title" "$wizard_body" >&2
	IFS= read -r wizard_value || return 2
	[ "$wizard_value" != back ] || return 1
	printf '%s\n' "$wizard_value"
}

wizard_disks() {
	# Backend rows are already filtered and their text is safe for terminal display.
	set --
	while IFS="$(printf '\t')" read -r wizard_dev wizard_id wizard_model wizard_bytes; do
		[ -n "$wizard_dev" ] || continue
		wizard_gib=$(awk -v bytes="$wizard_bytes" 'BEGIN {printf "%.1f GiB", bytes/1073741824}')
		set -- "$@" "$wizard_dev" "$wizard_model / $wizard_gib / $wizard_id"
	done <<TARGETS
$FS_INSTALL_TARGETS
TARGETS
	wizard_menu '2 of 4  /  Choose storage' \
		'Select a disk. You will review its identity and confirm before writing.' "$@"
}

wizard_preflight() {
	wizard_summary='The installer has identified these disks for selection.'
	if [ -z "$FS_INSTALL_TARGETS" ]; then
		wizard_summary='No eligible installation disk was found.'
	fi
	if [ -n "${FS_INSTALL_EXCLUSIONS:-}" ]; then
		wizard_summary=$(printf '%s\n\nExcluded from installation\n%s' \
			"$wizard_summary" "$FS_INSTALL_EXCLUSIONS")
	fi
	if [ -z "$FS_INSTALL_TARGETS" ]; then
		wizard_menu '1 of 4  /  Storage checks' "$wizard_summary" exit 'Finish without installing'
	else
		wizard_menu '1 of 4  /  Storage checks' "$wizard_summary" next 'Choose a disk' exit 'Finish without installing'
	fi
}

wizard_provisioning() {
	case "$FS_INSTALL_SEED_STATUS" in
		protected)
			wizard_menu '3 of 4  /  Provisioning' \
				"A Fleet Factory seed is available ($FS_INSTALL_FLEET_MODE / $FS_INSTALL_TRANSPORT). Save it privately for enrollment, or continue standalone." \
				standalone 'Install without a Fleet seed' fleet 'Save the Fleet seed'
			;;
		none)
			wizard_menu '3 of 4  /  Provisioning' \
				'No Fleet Factory seed was found. Network settings can be assigned from the installed console.' \
				standalone 'Set up at the appliance'
			;;
		*) return 2 ;;
	esac
}

wizard_network() {
	set -- later 'Assign from the appliance console'
	for wizard_nic in $FS_INSTALL_INTERFACES; do
		set -- "$@" "$wizard_nic" 'Save as the LAN interface'
	done
	wizard_menu '3 of 4  /  LAN assignment' 'Choose the LAN port for the installed appliance.' "$@"
}

guided_wizard() {
	[ "$FS_INSTALL_STATE" = select ] && [ "$FS_INSTALL_PLAN_STATUS" = absent ] || return 1
	wizard_display=text
	if wizard_screen; then wizard_display=dialog; fi
	wizard_stage=welcome
	FS_INSTALL_USE_SEED=no
	while :; do
		wizard_rc=0
		case "$wizard_stage" in
			welcome)
				wizard_choice=$(wizard_menu 'Welcome' \
					'Install FreeSense on a blank disk. The wizard checks storage and walks you through standalone or Fleet setup.' \
					start 'Review this appliance' exit 'Finish without installing') || wizard_rc=$?
				;;
			checks) wizard_choice=$(wizard_preflight) || wizard_rc=$? ;;
			storage) wizard_choice=$(wizard_disks) || wizard_rc=$? ;;
			provision) wizard_choice=$(wizard_provisioning) || wizard_rc=$? ;;
			network) wizard_choice=$(wizard_network) || wizard_rc=$? ;;
			review)
				wizard_setup='Standalone appliance'
				if [ "$FS_INSTALL_USE_SEED" = yes ]; then wizard_setup='Fleet seed saved for enrollment'; fi
				wizard_choice=$(wizard_menu '4 of 4  /  Review installation' \
					"Setup     $wizard_setup
Target    $FS_INSTALL_CONFIRM
LAN       ${FS_INSTALL_LAN:-Assign later}

The installer will check the entire disk for existing data, write FreeSense, and verify the written image." \
					confirm 'Enter disk confirmation' back 'Change provisioning') || wizard_rc=$?
				;;
			confirm) wizard_choice=$(wizard_confirmation) || wizard_rc=$? ;;
			*) return 1 ;;
		esac
		case "$wizard_rc" in
			1|5) wizard_choice=back ;; # Cancel / Escape from bsddialog
			3) continue ;;            # Blank input on a line terminal
			0) ;;
			*) guided_cancel; return 1 ;;
		esac
		case "$wizard_stage/$wizard_choice" in
			welcome/start) wizard_stage=checks ;;
			welcome/back|welcome/exit|checks/exit) guided_cancel; return 1 ;;
			checks/back) wizard_stage=welcome ;;
			checks/next) [ -n "$FS_INSTALL_TARGETS" ] && wizard_stage=storage ;;
			storage/back) wizard_stage=checks ;;
			storage/*)
				if guided_select "$wizard_choice"; then wizard_stage=provision
				else printf '\nSelect a listed storage device.\n' >&2; fi
				;;
			provision/back) guided_back || return 1; FS_INSTALL_USE_SEED=no; wizard_stage=storage ;;
			provision/standalone) FS_INSTALL_USE_SEED=no; wizard_stage=network ;;
			provision/fleet)
				[ "$FS_INSTALL_SEED_STATUS" = protected ] || return 1
				FS_INSTALL_USE_SEED=yes; wizard_stage=network
				;;
			network/back) wizard_stage=provision ;;
			network/later) FS_INSTALL_LAN=''; wizard_stage=review ;;
			network/*)
				for wizard_nic in $FS_INSTALL_INTERFACES; do
					if [ "$wizard_choice" = "$wizard_nic" ]; then
						FS_INSTALL_LAN=$wizard_nic; wizard_stage=review; break
					fi
				done
				;;
			review/back) wizard_stage=network ;;
			review/confirm) wizard_stage=confirm ;;
			confirm/back) wizard_stage=review ;;
			confirm/*)
				if [ "$wizard_choice" != "$FS_INSTALL_CONFIRM" ]; then
					wizard_title='Confirmation did not match'
					if [ "$wizard_display" = dialog ]; then
						wizard_dialog --msgbox 'Review the disk identity and try again.' 8 60 >&2 || return 1
					else printf '\nConfirmation did not match. Review the disk identity.\n' >&2
					fi
					wizard_stage=review
					continue
				fi
				printf '\nFreeSense  /  Installing\n' >&2
				guided_install "$wizard_choice" || return 1
				[ "$FS_INSTALL_STATE" = 'done' ] || return 1
				printf '\nInstallation verified. Remove the installer after power-off.\n' >&2
				return 0
				;;
			*) printf '\nChoose one of the listed options.\n' >&2 ;;
		esac
	done
}
