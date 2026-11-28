#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.
# Rehearse the port's exact file installer in a temporary directory.
set -eu
mode=$1
php_bin=${FREESENSE_TEST_PHP:-php}
"$php_bin" php-check.php preflight
install_root=$(mktemp -d)
trap 'rm -rf "$install_root"' EXIT
if [ -f stage-system.sh ]; then
	sh stage-system.sh "$PWD/component/src" "$install_root" pkg-plist
	cli="$install_root/usr/local/sbin/freesense-config"
	if [ -x /usr/local/bin/php ] || [ "$mode" = appliance ]; then
		set -- "$cli"
		printf 'Checking installed CLI shebang on PHP '
		/usr/local/bin/php -r 'echo PHP_VERSION, PHP_EOL;'
	else
		set -- "$php_bin" "$cli"
		echo 'Checking installed CLI through the portable PHP path'
	fi
	FREESENSE_CONFIG_DIR="$install_root/fixture-conf" "$@" init
	FREESENSE_CONFIG_DIR="$install_root/fixture-conf" "$@" backup > "$install_root/backup.xml"
	FREESENSE_CONFIG_DIR="$install_root/fixture-conf" "$@" restore < "$install_root/backup.xml"
fi
"$php_bin" php-check.php portable "$install_root"
if [ "$mode" = appliance ] && [ ! -f native-portable-only ]; then
	"$php_bin" php-check.php appliance "$install_root"
fi
