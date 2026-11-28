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
build() {
	make -j"$jobs" "$@" SRCCONF="$src/release/conf/FreeSense_build_src.conf" __MAKE_CONF="$src/release/conf/FreeSense_make.conf" \
		KERNCONF="$kernconf"
}
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
