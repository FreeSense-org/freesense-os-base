#!/bin/sh
# FreeSense 2.0 lab preflight (M0): is this FreeBSD host ready to build the FREESENSE kernel from the pinned tree?
# Usage: preflight.sh [srcdir] [objdir]   (defaults /build/src /build/obj)
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
src=${1:-/build/src}; obj=${2:-/build/obj}
fail=0
need() { if "$@" >/dev/null 2>&1; then echo "ok    $*"; else echo "FAIL  $*"; fail=1; fi; }
[ "$(uname -s)" = FreeBSD ] || { echo "FAIL  host is not FreeBSD"; exit 1; }
need test -d "$src/sys/amd64/conf"
need test -f "$src/sys/conf/FREESENSE-COMMON"
need test -f "$src/sys/amd64/conf/FREESENSE"
need test -f "$src/sys/amd64/conf/FREESENSE-DEBUG"
need test -f "$src/sys/amd64/conf/FREESENSE-HW"
need test -x /usr/bin/clang
need test -x /usr/bin/ld.lld
need test -x /usr/bin/make
mkdir -p "$obj"
avail=$(df -k "$obj" | awk 'NR==2{print int($4/1024)}')
if [ "$avail" -ge 12000 ]; then echo "ok    ${avail} MiB free in $obj"; else echo "FAIL  only ${avail} MiB free in $obj (need 12000)"; fail=1; fi
cpus=$(sysctl -n hw.ncpu); echo "info  ${cpus} CPUs, $(( $(sysctl -n hw.physmem) / 1048576 )) MiB RAM"
# No inherited vendor names in the kernel configuration.
if grep -rIl -i 'pfsense\|netgate' "$src/sys/conf/FREESENSE-COMMON" "$src"/sys/*/conf/FREESENSE* 2>/dev/null; then echo "FAIL  inherited names in kernel config"; fail=1; else echo "ok    kernel config has no inherited vendor names"; fi
exit $fail
