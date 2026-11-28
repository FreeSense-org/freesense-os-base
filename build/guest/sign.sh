#!/bin/sh
# Sign a package repository and prove the signature, on FreeBSD (build/sign-repo).
# Usage: sign.sh REPOSITORY KEYFILE PUBLICKEY
# Works on a copy of the catalogue and writes it back only after both checks passed.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
repo=$1 key=$2 public=$3
export ASSUME_ALWAYS_YES=yes IGNORE_OSVERSION=yes
work=$(mktemp -d "${TMPDIR:-/tmp}/freesense-sign-check.XXXXXX")
trap 'rm -rf "$work"' EXIT

# pkg of the repository itself, so signer and installer agree on the format.
set -- "$repo"/All/pkg-[0-9]*.pkg
[ $# -eq 1 ] && [ -f "$1" ] || { echo "ERROR: the repository has no pkg package" >&2; exit 1; }
tar -xf "$1" -C "$work" -s ',.*/,,' /usr/local/sbin/pkg-static
pkg=$work/pkg-static
count=$(find "$repo/All" -name '*.pkg' | wc -l | tr -d ' ')

cp -R "$repo" "$work/repo"
"$pkg" repo -q "$work/repo" "eddsa:$key" > /dev/null 2>&1 || { echo "ERROR: pkg repo could not sign" >&2; exit 1; }

# visible NAME PUBLICKEY: how many packages pkg shows when it verifies with that key. pkg update itself exits 0 either way.
visible() {
	mkdir -p "$work/$1/repos" "$work/$1/db"
	printf 'FreeSense: { url: "file://%s", signature_type: "pubkey", pubkey: "%s", enabled: yes }\n' "$work/repo" "$2" \
		> "$work/$1/repos/FreeSense.conf"
	"$pkg" -o PKG_DBDIR="$work/$1/db" --repo-conf-dir "$work/$1/repos" update -f > "$work/$1/log" 2>&1 || true
	{ "$pkg" -o PKG_DBDIR="$work/$1/db" --repo-conf-dir "$work/$1/repos" rquery -r FreeSense '%n' 2>/dev/null || true; } |
		wc -l | tr -d ' '
}
good=$(visible good "$public")
[ "$good" = "$count" ] ||
	{ tail -5 "$work/good/log" >&2; echo "ERROR: $good of $count packages are visible with the public key" >&2; exit 1; }
"$pkg" key --create -t eddsa "$work/other.key" > "$work/other.pub" 2>/dev/null
other=$(visible other "$work/other.pub")
[ "$other" = 0 ] || { echo "ERROR: $other packages are visible with a wrong key" >&2; exit 1; }

find "$work/repo" -maxdepth 1 -type f -exec cp {} "$repo/" \;
echo "sign: $count packages visible with the public key, none with another key"
