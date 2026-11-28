#!/bin/sh
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# Claim a single physical terminal before opening the attended wizard.
# Called only at rc.local on the boot medium, before gettys are running.

guided_terminal() {
	terminal_tmp=$(mktemp -d /tmp/freesense-terminal.XXXXXX) || return 1
	chmod 700 "$terminal_tmp" || return 1
	terminal_pids=''
	for terminal_dev in /dev/ttyv0 /dev/ttyu0; do
		[ -c "$terminal_dev" ] || continue
		(
			# FreeBSD UART call-in device must ignore carrier for a three-wire console.
			if [ "$terminal_dev" = /dev/ttyu0 ]; then
				stty -f /dev/ttyu0.init 115200 clocal 2>/dev/null || exit 1
			fi
			exec 3<>"$terminal_dev" || exit 1
			stty sane <&3 || exit 1
			if [ "$terminal_dev" = /dev/ttyu0 ]; then stty 115200 clocal <&3 || exit 1; fi
			printf '\r\nFreeSense installer  /  Press Enter to use this console.\r\n' >&3
			IFS= read -r _ <&3 || exit 1
			# A same-filesystem hard link chooses the first reader atomically.
			terminal_name=${terminal_dev##*/}
			printf '%s\n' "$terminal_dev" >"$terminal_tmp/$terminal_name" &&
				ln "$terminal_tmp/$terminal_name" "$terminal_tmp/chosen" 2>/dev/null
		) &
		terminal_pids="$terminal_pids $!"
	done
	while [ ! -f "$terminal_tmp/chosen" ]; do
		terminal_alive=no
		for terminal_pid in $terminal_pids; do
			if kill -0 "$terminal_pid" 2>/dev/null; then terminal_alive=yes; fi
		done
		[ "$terminal_alive" = yes ] || break
		sleep 1
	done
	for terminal_pid in $terminal_pids; do kill "$terminal_pid" 2>/dev/null || :; done
	for terminal_pid in $terminal_pids; do wait "$terminal_pid" 2>/dev/null || :; done
	terminal_choice=''
	IFS= read -r terminal_choice <"$terminal_tmp/chosen" || :
	rm -f "$terminal_tmp/ttyv0" "$terminal_tmp/ttyu0" "$terminal_tmp/chosen"
	rmdir "$terminal_tmp" || return 1
	case "$terminal_choice" in
		/dev/ttyv0) TERM=xterm ;;
		/dev/ttyu0) TERM=vt100 ;;
		*) printf 'No installer console could be opened.\n' >&2; return 1 ;;
	esac
	export TERM
	exec 0<>"$terminal_choice" 1>&0 2>&0
}
