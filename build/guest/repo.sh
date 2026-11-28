#!/bin/sh
# Stage repo: collect what the installer's package list needs from everything that was built or fetched, and nothing
# else, as the (unsigned) repository of the installer medium; then check that it alone satisfies the list.
# Usage: repo.sh PKG_STATIC REPO MEDIA SCRATCH ABI PACKAGE...
#   REPO has the packages in All/. MEDIA is empty and receives All/ and its catalogue.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
pkg=$1 repo=$2 media=$3 scratch=$4 abi=$5
shift 5
export ASSUME_ALWAYS_YES=yes

# into NAME REPOSITORY ARGS...: pkg working in an empty root with only that repository.
into() {
	dir=$scratch/$1 url=$2
	shift 2
	mkdir -p "$dir/repos" "$dir/root"
	printf 'FreeSense: { url: "file://%s", signature_type: "none", enabled: yes }\n' "$url" > "$dir/repos/FreeSense.conf"
	"$pkg" --rootdir "$dir/root" --repo-conf-dir "$dir/repos" -o IGNORE_OSVERSION=yes -o ABI="$abi" "$@"
}

echo "build/repo: cataloguing every package"
"$pkg" repo -q "$repo"
echo "build/repo: resolving the installer's list: $*"
into resolve "$repo" update -f
into resolve "$repo" install -F -y -r FreeSense "$@"
mkdir -p "$media/All"
find "$scratch/resolve/root/var/cache/pkg" -type f -name '*.pkg' | while read -r file; do
	cp "$file" "$media/All/$("$pkg" query -F "$file" '%n-%v').pkg"
done
"$pkg" repo -q "$media"

echo "build/repo: checking that the medium alone satisfies the list"
into check "$media" update -f
# A dry run exits non-zero when there is something to do; the listing is the proof.
into check "$media" install -n -r FreeSense "$@" > "$scratch/check.log" 2>&1 || true
if ! grep -q 'to be INSTALLED' "$scratch/check.log" || grep -i 'missing\|no packages available' "$scratch/check.log"; then
	tail -30 "$scratch/check.log"
	echo "ERROR: the installer's package list does not resolve from the medium" >&2
	exit 1
fi
echo "build/repo: $(find "$media/All" -name '*.pkg' | wc -l | tr -d ' ') packages, $(du -sh "$media" | cut -f1)"
echo REPOSITORY-READY
