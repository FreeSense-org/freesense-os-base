#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.
# Builder side of tools/lab_accept.py: assemble the lab package repository and check that the installer's package list
# resolves in an empty root.
# usage: make-repo.sh BASE_DIR STOCK_DIR BUILT_DIR REPO_DIR PACKAGE...
#   BASE_DIR   holds our base packages FreeSense-*-16.snap*.pkg (other files there are not used)
#   STOCK_DIR  holds the third-party packages (tools/lab/fetch_stock.py)
#   BUILT_DIR  holds the packages of freesense-system-ports/build-packages.sh
#   REPO_DIR   is removed and written again
#   PACKAGE    the names the installer asks for
set -eu
base=$1
stock=$2
built=$3
repo=$4
shift 4
case $repo in
	/build/v2-pkg/?*) ;;
	*) echo "REPO_DIR must be below /build/v2-pkg" >&2; exit 2 ;;
esac
rm -rf "$repo"
mkdir -p "$repo/All"
for file in "$base"/FreeSense-*-16.snap*.pkg "$stock"/*.pkg "$built"/*.pkg; do
	[ -f "$file" ] || { echo "no packages at $file" >&2; exit 1; }
	ln "$file" "$repo/All/" 2>/dev/null || cp "$file" "$repo/All/"
done
pkg-static repo "$repo" | tail -1

check=$(mktemp -d /tmp/fs-closure.XXXXXX)
trap 'rm -rf "$check"' EXIT
mkdir -p "$check/repos" "$check/root"
printf 'FreeSense: { url: "file://%s", signature_type: "none", enabled: yes }\n' "$repo" > "$check/repos/FreeSense.conf"
pkg() {
	pkg-static --rootdir "$check/root" --repo-conf-dir "$check/repos" -o IGNORE_OSVERSION=yes -o ABI=FreeBSD:16:amd64 "$@"
}
pkg update -f > "$check/log" 2>&1 || { tail -20 "$check/log"; exit 1; }
# A dry run exits non-zero when there is something to do; the listing of what would be installed is the proof.
pkg install -n -r FreeSense "$@" > "$check/log" 2>&1 || true
grep -q 'package(s) will be affected\|packages to be INSTALLED' "$check/log" ||
	{ tail -20 "$check/log"; echo "the installer's package list does not resolve"; exit 1; }
if grep -i 'missing\|no packages available' "$check/log"; then
	echo "the installer's package list does not resolve"
	exit 1
fi
echo REPOSITORY-READY
