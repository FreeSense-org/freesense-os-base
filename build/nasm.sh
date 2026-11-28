#!/bin/sh
# Build the pinned NASM for sys/modules/ipsecmb from the tarball the source stage verified and placed in the tree.
# Run on the FreeBSD build host before buildkernel. Usage: nasm.sh <freebsd-src-dir>
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
src=${1:?usage: nasm.sh <freebsd-src-dir>}
dest=$(CDPATH='' cd -- "${src}/sys/contrib/intel-ipsec-mb/.nasm" && pwd)
set -- "${dest}"/nasm-*.tar.xz
[ $# -eq 1 ] && [ -f "$1" ] || { echo "ERROR: expected one nasm tarball in ${dest}" >&2; exit 1; }
tarball=$1
cc=${CC:-cc}
work=$(mktemp -d "${TMPDIR:-/tmp}/freesense-nasm.XXXXXX")
trap 'rm -rf "${work}"' EXIT
tar -xf "${tarball}" -C "${work}"
cd "${work}"/nasm-*/
# A host tool: keep the kernel build's flags out.
env -u CFLAGS -u CPPFLAGS -u LDFLAGS -u MAKEFLAGS -u MAKEOBJDIRPREFIX CC="${cc}" ./configure --quiet >/dev/null 2>&1

# nasm's Makefile needs GNU make, which the build host need not have. The tarball carries every generated source,
# so compile the objects its Makefile lists.
# shellcheck disable=SC2016,SC2020 # literal $(O); tr maps both blanks to newlines
objects() {
	awk '/\\$/ { sub(/\\$/, ""); line = line $0 " "; next } { line = line $0; print line; line = "" }' Makefile |
	    awk -F'[[:space:]]*=[[:space:]]*' -v want="$1" '$1 == want { print $2 }' |
	    tr ' \t' '\n\n' | sed -n 's/\$(O)$/c/p'
}
library=$( (objects LIBOBJ_NW; objects WARNOBJ) | sort -u)
program=$(objects NASM)
[ -n "${library}" ] && [ -n "${program}" ] || { echo "ERROR: no nasm sources found in its Makefile" >&2; exit 1; }
flags="-O2 -DHAVE_CONFIG_H -I. -Iinclude -Ix86 -Iasm -Idisasm -Ioutput"
archive=""
for file in ${library}; do
	# shellcheck disable=SC2086
	"${cc}" ${flags} -c -o "${file%.c}.o" "${file}"
	archive="${archive} ${file%.c}.o"
done
# As in its Makefile the library goes into an archive, so symbols the program defines itself win.
# shellcheck disable=SC2086
ar rcs libnasm.a ${archive}
# shellcheck disable=SC2086
"${cc}" ${flags} -o nasm ${program} libnasm.a
cp nasm "${dest}/nasm"
"${dest}/nasm" -v
