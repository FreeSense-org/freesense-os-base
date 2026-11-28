#!/bin/sh
# FreeSense 2.0 lab world build (M3): buildworld with the FreeSense release configuration.
# Usage: build-world.sh [srcdir] [objdir]
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
src=${1:-/build/src}; obj=${2:-/build/obj}
export MAKEOBJDIRPREFIX="$obj"
jobs=${JOBS:-$(sysctl -n hw.ncpu)}   # heavy C++ files need about 2 GB each: set JOBS lower on small-RAM hosts
cd "$src"
start=$(date +%s)
echo ">>> buildworld ($(date -u +%H:%M:%S)) -j$jobs"
make -j"$jobs" buildworld \
	SRCCONF="$src/release/conf/FreeSense_build_src.conf" \
	__MAKE_CONF="$src/release/conf/FreeSense_make.conf"
end=$(date +%s)
echo ">>> buildworld done in $((end - start)) s"
du -sh "$obj$src/amd64.amd64" | cut -f1
