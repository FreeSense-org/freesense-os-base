#!/bin/sh
# Lab only: build the OpenLDAP 2.6 client (liblber, libldap and the ldap* tools; no slapd, no Cyrus SASL, TLS through the base
# OpenSSL) for the PHP ldap extension, stage it under STAGE for lab-php-openssl.sh and package it as FreeSense-openldap26-client
# in /build/repo2. Replaced by the System port's USE_PHP ldap (net/openldap26-client) in M6.
# usage: lab-openldap-client.sh OPENLDAP_TARBALL [STAGE]
#   OPENLDAP_TARBALL is openldap-2.6.N.tgz from https://www.openldap.org/software/download/OpenLDAP/openldap-release/,
#   checked against its published .sha3-512 before it is copied to the builder (the builder has no network).
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
tarball=$1; stage=${2:-/opt/fs-openldap}
version=$(basename "$tarball" .tgz); version=${version#openldap-}
case $version in 2.6.[0-9]*) ;; *) echo "unexpected tarball name: $tarball" >&2; exit 1 ;; esac
work=/build/openldap-src; meta=/build/openldapmeta; out=/build/openldappkg
rm -rf "$work" "$stage" "$meta" "$out"; mkdir -p "$work" "$meta" "$out"
tar -C "$work" -xf "$tarball" --strip-components=1
cd "$work"
# lld refuses version-script entries for the SASL symbols left out without SASL; the FreeBSD port passes the same flag.
LDFLAGS=-Wl,--undefined-version ./configure --prefix=/usr/local --sysconfdir=/usr/local/etc --localstatedir=/var --mandir=/usr/local/share/man \
	--disable-slapd --without-cyrus-sasl --with-tls=openssl --without-fetch --disable-static --enable-shared \
	> /build/openldap-configure.log 2>&1
make depend > /build/openldap-make.log 2>&1
make -j8 >> /build/openldap-make.log 2>&1
make DESTDIR="$stage" install > /build/openldap-install.log 2>&1
# The libtool archives name /usr/local/lib/liblber.la, which is not there when PHP links against the stage.
rm -f "$stage"/usr/local/lib/*.la
install -d "$stage/usr/local/share/licenses/FreeSense-openldap26-client"
install -m 0444 LICENSE COPYRIGHT "$stage/usr/local/share/licenses/FreeSense-openldap26-client/"

# The runtime package: libraries, tools and the license. Headers, man pages and libtool files stay in the stage for the PHP build.
cd "$stage/usr/local"
{ find lib -maxdepth 1 \( -name 'liblber*.so*' -o -name 'libldap*.so*' \); find bin -name 'ldap*'
  find share/licenses/FreeSense-openldap26-client -type f; } | sort > /build/openldap.plist
cat > "$meta/+MANIFEST" <<EOF
name: FreeSense-openldap26-client
version: "$version"
origin: net/FreeSense-openldap26-client
comment: "OpenLDAP $version client libraries for the FreeSense System"
www: https://www.openldap.org
maintainer: dev@freesense.org
prefix: /usr/local
arch: FreeBSD:16:amd64
licenselogic: single
licenses: [OPENLDAP]
desc: "OpenLDAP $version liblber, libldap and client tools (TLS through the base OpenSSL, no SASL), used by the PHP ldap extension."
EOF
pkg-static create -r "$stage" -m "$meta" -p /build/openldap.plist -o "$out" 2>&1 | tail -2
rm -f /build/repo2/All/FreeSense-openldap26-client-*.pkg
cp "$out"/*.pkg /build/repo2/All/
ls -l /build/repo2/All/FreeSense-openldap26-client-*.pkg
