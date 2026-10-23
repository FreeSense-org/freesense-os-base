#!/bin/sh
#
# kernel-contrib.sh — fetch the third-party sources that FreeSense kernel
# modules build from into a freebsd-src checkout (called by apply.sh).
#
# Usage: kernel-contrib.sh <freebsd-src-dir>
#
# config/kernel-contrib.json pins every source:
#   intel-ipsec-mb  git commit  -> sys/contrib/intel-ipsec-mb
#   nasm            sha256      -> sys/contrib/intel-ipsec-mb/.nasm/nasm (built here)
#
# Local or offline builds can point at existing copies instead of fetching:
#   IPSECMB_SRC=<intel-ipsec-mb checkout at the pinned commit>
#   NASM_TARBALL=<nasm release tarball>  (still checked against the sha256)
# KERNEL_CONTRIB_SKIP=1 skips everything (the ipsecmb module then cannot build).
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cfg="${here}/config/kernel-contrib.json"
src="${1:?usage: kernel-contrib.sh <freebsd-src-dir>}"

if [ "${KERNEL_CONTRIB_SKIP:-0}" = "1" ]; then
	echo ">>> kernel-contrib: skipped (KERNEL_CONTRIB_SKIP=1)"
	exit 0
fi
# The FreeSense builder exports the kernel's module list (tools/builder_defaults.sh).
# Kernels that do not build ipsecmb (Stable 1.1, arm64) need none of this.
if [ -n "${MODULES_OVERRIDE+set}" ]; then
	case " ${MODULES_OVERRIDE} " in
	*" ipsecmb "*) ;;
	*)
		echo ">>> kernel-contrib: skipped (ipsecmb is not in MODULES_OVERRIDE)"
		exit 0
		;;
	esac
fi

# value <section> <key>: a string value from one section of the config.
value() {
	sed -n "/\"$1\": *{/,/}/s/^ *\"$2\": *\"\([^\"]*\)\".*/\1/p" "${cfg}" | head -n 1
}

sha256_of() {
	if command -v sha256 >/dev/null 2>&1; then
		sha256 -q "$1"
	else
		sha256sum "$1" | awk '{print $1}'
	fi
}

download() {
	if command -v fetch >/dev/null 2>&1; then
		fetch -q -o "$2" "$1"
	else
		curl -fsSL -o "$2" "$1"
	fi
}

lib_url=$(value intel-ipsec-mb url)
lib_commit=$(value intel-ipsec-mb commit)
lib_dest="${src}/$(value intel-ipsec-mb destination)"
nasm_url=$(value nasm url)
nasm_mirror=$(value nasm mirror)
nasm_sha=$(value nasm sha256)
nasm_dest="${src}/$(value nasm destination)"
case "${lib_commit}" in
[0-9a-f]*) [ ${#lib_commit} -eq 40 ] || { echo ">>> ERROR: bad intel-ipsec-mb commit in ${cfg}" >&2; exit 1; } ;;
*) echo ">>> ERROR: bad intel-ipsec-mb commit in ${cfg}" >&2; exit 1 ;;
esac
[ ${#nasm_sha} -eq 64 ] || { echo ">>> ERROR: bad nasm sha256 in ${cfg}" >&2; exit 1; }

# --- intel-ipsec-mb at the pinned commit ---------------------------------
echo ">>> kernel-contrib: intel-ipsec-mb ${lib_commit}"
rm -rf "${lib_dest}"
mkdir -p "$(dirname "${lib_dest}")"
if [ -n "${IPSECMB_SRC:-}" ]; then
	cp -R "${IPSECMB_SRC}" "${lib_dest}"
	rm -rf "${lib_dest}/.git"
else
	git init -q "${lib_dest}"
	git -C "${lib_dest}" fetch -q --depth 1 "${lib_url}" "${lib_commit}"
	git -C "${lib_dest}" checkout -q FETCH_HEAD
	head=$(git -C "${lib_dest}" rev-parse HEAD)
	if [ "${head}" != "${lib_commit}" ]; then
		echo ">>> ERROR: intel-ipsec-mb is at ${head}, expected ${lib_commit}" >&2
		exit 1
	fi
	rm -rf "${lib_dest}/.git"
fi
[ -f "${lib_dest}/lib/intel-ipsec-mb.h" ] || {
	echo ">>> ERROR: ${lib_dest} is not an intel-ipsec-mb tree" >&2
	exit 1
}

# --- nasm, built from its pinned release tarball --------------------------
echo ">>> kernel-contrib: nasm ${nasm_sha}"
work=$(mktemp -d "${TMPDIR:-/tmp}/kernel-contrib-nasm.XXXXXX")
trap 'rm -rf "${work}"' EXIT
if [ -n "${NASM_TARBALL:-}" ]; then
	cp "${NASM_TARBALL}" "${work}/nasm.tar.xz"
else
	# The FreeBSD port's two sites; the sha256 below decides, not the site.
	for u in "${nasm_url}" "${nasm_mirror}"; do
		[ -n "${u}" ] || continue
		download "${u}" "${work}/nasm.tar.xz" && break
		echo ">>> kernel-contrib: ${u} failed, trying the next site" >&2
		rm -f "${work}/nasm.tar.xz"
	done
	[ -s "${work}/nasm.tar.xz" ] || { echo ">>> ERROR: could not download nasm" >&2; exit 1; }
fi
got=$(sha256_of "${work}/nasm.tar.xz")
if [ "${got}" != "${nasm_sha}" ]; then
	echo ">>> ERROR: nasm tarball sha256 ${got}, expected ${nasm_sha}" >&2
	exit 1
fi
tar -xf "${work}/nasm.tar.xz" -C "${work}"
(
	cd "${work}"/nasm-*/
	cc="${KERNEL_CONTRIB_CC:-cc}"
	# The host compiler builds a host tool; keep the kernel build's flags out.
	env -u CFLAGS -u CPPFLAGS -u LDFLAGS -u MAKEFLAGS -u MAKEOBJDIRPREFIX \
	    CC="${cc}" ./configure --quiet >/dev/null 2>&1
	# nasm's Makefile needs GNU make, which the build worker does not have.
	# The release tarball carries every generated source, so compile the
	# objects its Makefile lists for the nasm program directly.
	# Like its Makefile: the library objects go into libnasm.a, so a symbol
	# the program defines itself (error_file) wins over the library copy.
	objlist() {
		awk '
			/\\$/ { sub(/\\$/, ""); line = line $0 " "; next }
			{ line = line $0; print line; line = "" }
		' Makefile | awk -F'[[:space:]]*=[[:space:]]*' -v want="$1" '$1 == want { print $2 }' |
		    tr ' \t' '\n\n' | sed -n 's/\$(O)$/c/p'
	}
	lib_srcs=$( (objlist LIBOBJ_NW; objlist WARNOBJ) | sort -u)
	prog_src=$(objlist NASM)
	[ -n "${lib_srcs}" ] && [ -n "${prog_src}" ] || {
		echo ">>> ERROR: no nasm sources found in its Makefile" >&2
		exit 1
	}
	flags="-O2 -DHAVE_CONFIG_H -I. -Iinclude -Ix86 -Iasm -Idisasm -Ioutput"
	for s in ${lib_srcs}; do
		# shellcheck disable=SC2086
		"${cc}" ${flags} -c -o "${s%.c}.o" "${s}"
	done
	# shellcheck disable=SC2046
	ar rcs libnasm.a $(for s in ${lib_srcs}; do echo "${s%.c}.o"; done)
	# shellcheck disable=SC2086
	"${cc}" ${flags} -o nasm ${prog_src} libnasm.a
)
rm -rf "${nasm_dest}"
mkdir -p "${nasm_dest}"
cp "${work}"/nasm-*/nasm "${nasm_dest}/nasm"
"${nasm_dest}/nasm" -v
echo ">>> kernel-contrib: done"
