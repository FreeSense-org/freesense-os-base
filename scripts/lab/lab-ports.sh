#!/bin/sh
# Lab only: build the System's third-party ports as packages for /build/repo2 from the pinned ports tree (freesense-os-base
# config freebsd_ports.commit, extracted to /build/ports), with the Development 1.2 option sets (/build/ports-make.conf, see
# lab-ports-make.conf). Build dependencies come from the FreeBSD package catalogue; run dependencies the ports do not
# change are fetched from that catalogue into repo2 so the appliance resolves everything from repo2 alone.
# usage: lab-ports.sh ORIGIN...   (for example net/kea security/openvpn)
#   ORIGIN may name an overlay port under /build/ports-fs (net/kea without kea-shell and python, sysutils/ssh_tunnel_shell).
#   JOBS sets MAKE_JOBS_NUMBER (default 4). Network access goes through HTTP(S)_PROXY when the builder is host-only.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
export PORTSDIR=/build/ports DISTDIR=/build/distfiles WRKDIRPREFIX=/build/pwork PACKAGES=/build/pkgout BATCH=yes \
	__MAKE_CONF=/build/ports-make.conf REPOS_DIR="${REPOS_DIR:-/build/pkgrepos}" ASSUME_ALWAYS_YES=yes
repo=/build/repo2
overlay=/build/ports-fs
jobs=${JOBS:-4}
mkdir -p "$DISTDIR" "$WRKDIRPREFIX" "$PACKAGES" "$overlay"
here=$(dirname "$0")
[ -f "$here/lab-ports-make.conf" ] && cp "$here/lab-ports-make.conf" "$__MAKE_CONF"

# Kea without kea-shell: the stock port makes python a run dependency only for that one script.
overlay_kea() {
	d=$overlay/net/kea
	[ -f "$d/.fs-overlay" ] && return 0
	rm -rf "$d"; mkdir -p "$overlay/net"; cp -R "$PORTSDIR/net/kea" "$d"
	sed -i '' -e 's/\([[:space:]]\)python\([[:space:]]\)/\1python:build\2/' "$d/Makefile"
	grep -q 'python:build' "$d/Makefile"
	# Kea 3.2 uses Boost headers only: none of its 47 ELF files links a libboost_* (checked with ldd, 2026-11-27), so the
	# 195 MiB boost-libs package is a build dependency, not a run dependency.
	awk '/^LIB_DEPENDS=.*libboost_date_time\.so:devel\/boost-libs/ { print "BUILD_DEPENDS+=\tboost-libs>0:devel/boost-libs"; print "LIB_DEPENDS=\t\\"; next } { print }' \
		"$d/Makefile" > "$d/Makefile.fs" && mv "$d/Makefile.fs" "$d/Makefile"
	grep -q 'BUILD_DEPENDS+=	boost-libs' "$d/Makefile"
	# Append to the end of the post-install recipe (the first blank line after it), so the man page exists before it is removed.
	awk '/^post-install:/ { p = 1 } p && /^$/ {
		print "\t${RM} ${STAGEDIR}${PREFIX}/sbin/kea-shell ${STAGEDIR}${PREFIX}/share/man/man8/kea-shell.8"
		print "\t${RM} -r ${STAGEDIR}${PYTHON_SITELIBDIR}/kea"; p = 0 } { print }' "$d/Makefile" > "$d/Makefile.fs"
	mv "$d/Makefile.fs" "$d/Makefile"
	grep -q 'sbin/kea-shell' "$d/Makefile"
	sed -i '' -e '/%%PYTHON_SITELIBDIR%%\/kea\//d' -e '/^sbin\/kea-shell$/d' -e '/kea-shell\.8\.gz$/d' "$d/pkg-plist"
	touch "$d/.fs-overlay"
}

# OpenLDAP client without Cyrus SASL (8 MB), like the earlier FreeSense-openldap26-client; PHP ldap, pam_ldap and nss_ldap link it.
overlay_openldap() {
	d=$overlay/databases
	[ -f "$d/openldap26-client/.fs-overlay" ] && return 0
	rm -rf "$d/openldap26-client" "$d/openldap26-server"; mkdir -p "$d"
	cp -R "$PORTSDIR/databases/openldap26-client" "$PORTSDIR/databases/openldap26-server" "$d/"
	sed -i '' -e '/^LIB_DEPENDS+=.*libsasl2\.so/d' -e 's/--enable-spasswd/--disable-spasswd/' -e 's/--with-cyrus-sasl/--without-cyrus-sasl/' \
		"$d/openldap26-server/Makefile"
	# A plain "! grep" would not stop the script under set -e.
	if grep -q 'libsasl2' "$d/openldap26-server/Makefile"; then echo "libsasl2 is still referenced"; exit 1; fi
	# Without SASL the libldap version script still names the SASL helpers (lld refuses undefined versioned symbols).
	awk '!done && /^MAKE_ENV\+=/ { print; print "LDFLAGS+=\t-Wl,--undefined-version"; done = 1; next } { print }' \
		"$d/openldap26-server/Makefile" > "$d/Makefile.fs" && mv "$d/Makefile.fs" "$d/openldap26-server/Makefile"
	grep -q 'undefined-version' "$d/openldap26-server/Makefile"
	touch "$d/openldap26-client/.fs-overlay"
}

portdir() {
	if [ -d "$overlay/$1" ]; then echo "$overlay/$1"; else echo "$PORTSDIR/$1"; fi
}

for origin in "$@"; do
	[ "$origin" = net/kea ] && overlay_kea
	[ "$origin" = databases/openldap26-client ] && overlay_openldap
	d=$(portdir "$origin")
	test -f "$d/Makefile"
	name=$(make -C "$d" -V PKGNAME)
	log=/build/port-$(echo "$origin" | tr / _).log
	echo ">>> $origin ($name) $(date -u +%H:%M:%S)"
	{
		make -C "$d" install-missing-packages
		make -C "$d" clean
		make -C "$d" MAKE_JOBS_NUMBER="$jobs" package
	} > "$log" 2>&1 || { echo "FAILED $origin (see $log)"; tail -15 "$log"; continue; }
	cp "$PACKAGES/All/$name.pkg" "$repo/All/"
	make -C "$d" clean > /dev/null 2>&1 || true
	# Later ports (pam_ldap, nss_ldap, PHP) must link this build, not the catalogue's SASL variant.
	[ "$origin" = databases/openldap26-client ] && { pkg-static delete -fy openldap26-client > /dev/null 2>&1 || true; pkg-static add "$repo/All/$name.pkg" > /dev/null; }
	# Run dependencies not built here: fetch them (with their own dependencies) from the FreeBSD catalogue.
	deps=$(pkg-static query -F "$repo/All/$name.pkg" '%dn')
	for dep in $deps; do
		ls "$repo/All/$dep"-[0-9]*.pkg > /dev/null 2>&1 && continue
		rm -rf /build/repo2-fetch; pkg-static fetch -y -d -o /build/repo2-fetch "$dep" > /dev/null
		find /build/repo2-fetch -name '*.pkg' | while read -r f; do
			n=$(pkg-static query -F "$f" '%n')
			ls "$repo/All/$n"-[0-9]*.pkg > /dev/null 2>&1 || cp "$f" "$repo/All/$(pkg-static query -F "$f" '%n-%v').pkg"
		done
	done
	echo "    $name.pkg $(du -k "$repo/All/$name.pkg" | cut -f1) KB, flat size $(pkg-static query -F "$repo/All/$name.pkg" '%sh'), deps: $(printf '%s' "$deps" | tr '\n' ' ')"
done
pkg-static repo "$repo" 2>&1 | tail -1
