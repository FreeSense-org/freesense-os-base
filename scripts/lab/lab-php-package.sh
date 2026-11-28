#!/bin/sh
# Lab only: package the builder PHP 8.6 CLI (from lab-php-openssl.sh) as FreeSense-php86 in /build/repo2 and check that it installs
# with its dependencies (pcre2; openldap26-client without SASL and libsodium from lab-ports.sh) and nginx-lite and fcgiwrap into a
# throwaway root. Replaced by System metaport packages (M6).
# usage: lab-php-package.sh [PHP_PREFIX]   (default /opt/refactor12/php86-openssl)
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
prefix=${1:-/opt/refactor12/php86-openssl}
stage=/build/phpstage
ldap_version=$(pkg-static query -F /build/repo2/All/openldap26-client-*.pkg '%v')
sodium_version=$(pkg-static query -F /build/repo2/All/libsodium-*.pkg '%v')
xml_version=$(pkg-static query -F /build/repo2/All/libxml2-*.pkg '%v')
install -d "$stage/usr/local/bin"
install -m 0755 "$prefix/bin/php" "$stage/usr/local/bin/php"
# pcre2, libxml2, the OpenLDAP client and libsodium are dependencies (libxml2 no longer travels inside: the libxml2 package in repo2,
# pulled by mobile-broadband-provider-info, would conflict with it).
printf '%s\n' bin/php > /build/php2.plist
mkdir -p /build/phpmeta
cat > /build/phpmeta/+MANIFEST <<EOF
name: FreeSense-php86
version: "8.6.0_1"
origin: lang/FreeSense-php86
comment: "PHP 8.6 CLI for the FreeSense System"
www: https://freesense.org
maintainer: dev@freesense.org
prefix: /usr/local
arch: FreeBSD:16:amd64
licenselogic: single
licenses: [PHP301]
desc: "PHP 8.6.0 command line interpreter built for FreeSense with sqlite3, posix, pcntl, sockets, session, fileinfo, dom, tokenizer, openssl, ldap, iconv, zlib and sodium."
deps: {pcre2: {origin: devel/pcre2, version: "10.49"}, libxml2: {origin: textproc/libxml2, version: "$xml_version"}, openldap26-client: {origin: databases/openldap26-client, version: "$ldap_version"}, libsodium: {origin: security/libsodium, version: "$sodium_version"}}
EOF
rm -f /build/phppkg/*; mkdir -p /build/phppkg
pkg-static create -r "$stage" -m /build/phpmeta -p /build/php2.plist -o /build/phppkg 2>&1 | tail -2
cp /build/phppkg/*.pkg /build/repo2/All/; pkg-static repo /build/repo2 2>&1 | tail -1
ls -l /build/repo2/All/FreeSense-php86-*.pkg /build/repo2/All/openldap26-client-*.pkg /build/repo2/All/libsodium-*.pkg

W=$(mktemp -d /tmp/fs-phppkg.XXXXXX); mkdir -p "$W/repos" "$W/db" "$W/root"
trap 'chflags -R noschg "$W"; rm -rf "$W"' EXIT
printf 'FreeSense: { url: "file:///build/repo2", signature_type: "none", enabled: yes }\n' > "$W/repos/fs.conf"
export ASSUME_ALWAYS_YES=yes IGNORE_OSVERSION=yes
P="pkg-static -o REPOS_DIR=$W/repos -o PKG_DBDIR=$W/db -o ABI=FreeBSD:16:amd64 -r $W/root"
$P update -f 2>&1 | tail -1
$P install -y FreeSense-php86 nginx-lite fcgiwrap 2>&1 | tail -2
$P info | grep -i "php\|nginx\|pcre\|fcgiwrap\|ldap\|sodium"
ls "$W/root/usr/local/bin/php" "$W/root/usr/local/sbin/nginx" "$W/root/usr/local/lib/libxml2.so.16" "$W/root/usr/local/lib/libldap.so.2" "$W/root/usr/local/lib/libsodium.so"
"$W/root/usr/local/bin/php" -m | tr '
' ' '; echo
