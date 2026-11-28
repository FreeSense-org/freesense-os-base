#!/bin/sh
# FreeSense 2.0 lab kernel build (M2): kernel-toolchain, then buildkernel KERNCONF=FREESENSE (or FREESENSE-DEBUG).
# Usage: build-kernel.sh [KERNCONF] [srcdir] [objdir]
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
conf=${1:-FREESENSE}; src=${2:-/build/src}; obj=${3:-/build/obj}
export MAKEOBJDIRPREFIX="$obj"
jobs=$(sysctl -n hw.ncpu)
cd "$src"
start=$(date +%s)
echo ">>> kernel-toolchain ($(date -u +%H:%M:%S))"
make -j"$jobs" kernel-toolchain
echo ">>> buildkernel KERNCONF=$conf ($(date -u +%H:%M:%S))"
if [ -n "${MODULES_OVERRIDE+set}" ]; then set -- MODULES_OVERRIDE="$MODULES_OVERRIDE"; else set --; fi
make -j"$jobs" buildkernel KERNCONF="$conf" "$@"
end=$(date +%s)
echo ">>> done in $((end - start)) s"
ls -l "$obj$src/amd64.amd64/sys/$conf/kernel" 2>/dev/null || find "$obj" -name kernel -path "*$conf*" | head -3
