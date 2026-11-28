#!/bin/sh
# Stage deps: build the third-party ports that need our own options, from the pinned ports tree.
# Usage: ports.sh PORTSDIR MAKE_CONF WORK OUT DISTDIR ORIGIN...
# Build dependencies are installed from REPOS_DIR (the lock's repository) into the throwaway host. Distfiles are checked
# by the ports framework against the pinned tree's distinfo.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
ports=$1 conf=$2 work=$3 out=$4 distfiles=$5
shift 5
mkdir -p "$work/obj" "$work/packages" "$work/portdb" "$distfiles"
port_make() {
	make -C "$ports/$origin" PORTSDIR="$ports" __MAKE_CONF="$conf" BATCH=yes DISTDIR="$distfiles" WRKDIRPREFIX="$work/obj" \
		PACKAGES="$work/packages" PORT_DBDIR="$work/portdb" "$@"
}
for origin in "$@"; do
	name=$(port_make -V PKGNAME)
	echo "build/ports: $origin -> $name ($(date -u +%H:%M:%S))"
	port_make install-missing-packages
	port_make package
	cp "$work/packages/All/$name.pkg" "$out/"
done
echo "build/ports: done"
