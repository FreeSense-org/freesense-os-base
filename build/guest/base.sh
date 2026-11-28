#!/bin/sh
# Stage base: buildworld, the pinned NASM, buildkernel and the FreeSense-* base packages from the patched source.
# Usage: base.sh SRC OBJ REPODIR JOBS KERNCONF NASM_SH
# On a kept OBJ, finished world and kernel steps are not repeated, so a failed run continues where it stopped.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
src=$1 obj=$2 repo=$3 jobs=$4 kernconf=$5 nasm=$6
export MAKEOBJDIRPREFIX="$obj"
mkdir -p "$obj" "$repo"
cd "$src"
tree=$(cat "$src/.freesense-tree")
say() { echo "build/base: $* ($(date -u +%H:%M:%S))"; }
conf() {
	make "$@" SRCCONF="$src/release/conf/FreeSense_build_src.conf" __MAKE_CONF="$src/release/conf/FreeSense_make.conf" \
		KERNCONF="$kernconf"
}
# The build host is the same revision as the source, so make takes its compiler and linker (WITH_SYSTEM_COMPILER and
# WITH_SYSTEM_LINKER). It still builds all of libllvm for llvm-ar, nm and objcopy unless every one of these binutils
# is given as a full path.
tools=
# system WHAT: make's own decision; only Makefile.inc1 knows it. The build is native: target is the host.
system() {
	conf -f Makefile.inc1 TARGET="$(uname -m)" TARGET_ARCH="$(uname -p)" "test-system-$1" -V "$2" 2>/dev/null
}
if [ "$(system compiler USING_SYSTEM_COMPILER)" = yes ] && [ "$(system linker USING_SYSTEM_LINKER)" = yes ]; then
	tools="XAR=/usr/bin/ar XNM=/usr/bin/nm XOBJCOPY=/usr/bin/objcopy XSIZE=/usr/bin/size XSTRINGS=/usr/bin/strings XSTRIPBIN=/usr/bin/strip"
fi
build() {
	# shellcheck disable=SC2086 # a list of assignments
	conf -j"$jobs" "$@" $tools
}
if [ -n "$tools" ]; then
	say "compiler, linker and binutils of the build host: no bootstrap toolchain"
else
	say "WARNING: the host compiler or linker differs from the source: building a bootstrap toolchain (slow)"
fi
done_for_tree() { [ -f "$obj/.$1-done" ] && [ "$(cat "$obj/.$1-done")" = "$tree" ]; }

if done_for_tree world; then
	say "buildworld kept from the earlier run"
else
	say "buildworld -j$jobs"
	build buildworld
	echo "$tree" > "$obj/.world-done"
fi
say "nasm for the ipsecmb module"
sh "$nasm" "$src"
if done_for_tree kernel; then
	say "buildkernel kept from the earlier run"
else
	say "buildkernel $kernconf"
	build buildkernel
	echo "$tree" > "$obj/.kernel-done"
fi
module=$(find "$obj" -name ipsecmb.ko | head -1)
[ -n "$module" ] || { echo "ERROR: the kernel build made no ipsecmb.ko" >&2; exit 1; }
say "base packages"
build packages PKG_NAME_PREFIX=FreeSense REPODIR="$repo"
say "done, obj uses $(du -sh "$obj" | cut -f1)"
