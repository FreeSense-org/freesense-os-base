#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.
# Builder side of tools/lab_accept.py: build the FreeSense packages with the real ports from the tree the caller copied to
# /build/v2-pkg/src.tar, and write the lab package repository /build/v2-pkg/repo.
# usage: build-packages.sh PACKAGE...   (the names the installer asks for)
# Uses the ports tree /build/ports, the third-party packages in /build/v2-pkg/stock (tools/lab/fetch_stock.py) and our
# base packages in /build/repo2 (read only).
set -eu
work=/build/v2-pkg

rm -rf "$work/src" "$work/out"
mkdir -p "$work/src" "$work/out"
tar -xf "$work/src.tar" -C "$work/src"
sh "$work/src/freesense-system-ports/build-packages.sh" -p /build/ports -d "$work/stock" -o "$work/out" -w "$work/work" \
	> "$work/build.log" 2>&1 || { tail -30 "$work/build.log"; echo "package build failed"; exit 1; }
grep '^build-packages:' "$work/build.log"
sh "$work/src/tools/lab/make-repo.sh" /build/repo2/All "$work/stock" "$work/out" "$work/repo" "$@"
echo PACKAGES-READY
