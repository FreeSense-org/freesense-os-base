#!/bin/sh
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# The caller owns media mounts, binding checks, target readback and unmounts.

guided_inspect_file() (
	umask 077
	[ -f "$2" ] && [ ! -L "$2" ] && [ -d "$3" ] && [ ! -L "$3" ] || return 1
	exec /usr/libexec/flua "${FREESENSE_PROVISION_LIB:-/freesense}/provision.lua" "$1" "$2" "$3"
)

guided_parse_plan() { guided_inspect_file plan "$1" "$2"; }
guided_preview_seed() { guided_inspect_file seed "$1" "$2"; }

guided_seed_summary() (
	[ -f "$1/fleet_mode" ] && [ -f "$1/transport" ] || return 1
	kind=$(cat "$1/fleet_mode") && method=$(cat "$1/transport") || return 1
	case "$kind" in attended|pin|unattended) ;; *) return 1 ;; esac
	case "$method" in tunnel|tailscale|direct) ;; *) return 1 ;; esac
	printf 'Fleet seed: %s, %s. First boot can stage it; online enrollment is unavailable.\n' "$kind" "$method"
)

guided_apply_plan() (
	set -eu
	umask 077
	work=$1
	root=$2
	include=$3
	case "$include" in include|skip) ;; *) return 1 ;; esac
	case "$root" in /|'') return 1 ;; /*) ;; *) return 1 ;; esac
	[ ! -L "$root" ] && [ -x "$root/usr/local/sbin/freesense-config" ] || return 1
	if [ ! -f "$work/lan" ] && [ ! -f "$work/admin_password_hash" ] && { [ "$include" = skip ] || [ ! -f "$work/fleet_seed" ]; }; then return 0; fi
	if [ -e "$work/lan" ]; then
		[ -f "$work/lan" ] && [ ! -L "$work/lan" ] || return 1
		device=$(cat "$work/lan") || return 1
		[ "${#device}" -ge 1 ] && [ "${#device}" -le 15 ] || return 1
		case "$device" in -*|*[!a-zA-Z0-9_.-]*) return 1 ;; esac
		[ "$(wc -c < "$work/lan" | tr -d ' ')" = "${#device}" ] || return 1
	fi
	if [ -e "$work/admin_password_hash" ]; then
		[ -f "$work/admin_password_hash" ] && [ ! -L "$work/admin_password_hash" ] || return 1
		[ "$(wc -c < "$work/admin_password_hash" | tr -d ' ')" = 60 ] || return 1
		# shellcheck disable=SC2016 # the bcrypt prefix is a literal pattern, not an expansion
		case "$(cat "$work/admin_password_hash")" in '$2'[aby]'$'[12][0-9]'$'*) ;; *) return 1 ;; esac
	fi
	if [ "$include" = include ] && [ -e "$work/fleet_seed" ]; then
		[ -f "$work/fleet_seed" ] && [ ! -L "$work/fleet_seed" ] || return 1
		guided_seed_summary "$work" >/dev/null || return 1
	fi
	for dir in "$root/cf" "$root/cf/conf" "$root/cf/conf/freesense"; do
		[ ! -L "$dir" ] || return 1
		[ -d "$dir" ] || mkdir -m 0700 "$dir"
	done
	conf=$root/cf/conf/freesense
	for path in "$conf/config.xml" "$conf/provision-pending" "$conf/provision-applying" "$conf/provision-complete" "$root/cf/conf/fleet/seed.json"; do
		[ ! -e "$path" ] && [ ! -L "$path" ] || return 1
	done
	# Single writer after the installer's blank-disk and full readback checks.
	tmp=$conf/.installer-handoff
	mkdir -m 0700 "$tmp"
	trap 'rm -f "$tmp/lan" "$tmp/fleet_seed" "$tmp/admin_password_hash"; rmdir "$tmp" 2>/dev/null || :' EXIT
	if [ -f "$work/lan" ]; then install -m 0600 "$work/lan" "$tmp/lan"; fi
	if [ -f "$work/admin_password_hash" ]; then install -m 0600 "$work/admin_password_hash" "$tmp/admin_password_hash"; fi
	if [ "$include" = include ] && [ -f "$work/fleet_seed" ]; then
		install -m 0600 "$work/fleet_seed" "$tmp/fleet_seed"
	fi
	mv "$tmp" "$conf/provision-pending"
	trap - EXIT
	sync
)
