#!/bin/sh
# FreeSense 2.0 lab base packages (M5): turn the built world and kernel into FreeSense-* pkgbase packages.
# Run after build-world.sh and build-kernel.sh, with the same SRCCONF and KERNCONF.
# Usage: build-packages.sh [KERNCONF] [srcdir] [objdir] [repodir]
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
conf=${1:-FREESENSE}; src=${2:-/build/src}; obj=${3:-/build/obj}; repo=${4:-/build/repo}
export MAKEOBJDIRPREFIX="$obj"
jobs=${JOBS:-$(sysctl -n hw.ncpu)}
cd "$src"
start=$(date +%s)
echo ">>> packages ($(date -u +%H:%M:%S))"
make -j"$jobs" packages \
	SRCCONF="$src/release/conf/FreeSense_build_src.conf" \
	__MAKE_CONF="$src/release/conf/FreeSense_make.conf" \
	KERNCONF="$conf" PKG_NAME_PREFIX=FreeSense REPODIR="$repo"
end=$(date +%s)
echo ">>> packages done in $((end - start)) s"
find "$repo" -name '*.pkg' | wc -l
du -sh "$repo" | cut -f1
