#!/bin/sh
# Lab only: build the System PHP 8.6 CLI with the openssl, ldap, iconv (libc), zlib (base libz) and sodium (libsodium package)
# extensions added to the extension set of lab-php-package.sh. intl is not built (owner undecided; IDN names stay ASCII-only).
# usage: lab-php-openssl.sh PHP_SOURCE_TARBALL PREFIX [LDAP_DIR]   (the source is the php:8.6 image's /usr/src/php.tar.xz)
#   LDAP_DIR holds include/ldap.h and lib/libldap.so (default /usr/local: the SASL-free openldap26-client from lab-ports.sh,
#   installed on the builder); libsodium must be installed on the builder (lab-ports.sh security/libsodium).
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
tarball=$1; prefix=$2; ldap=${3:-/usr/local}
test -f "$ldap/include/ldap.h"
test -f /usr/local/include/sodium.h
work=/build/php-openssl-src
rm -rf "$work"; mkdir -p "$work"
tar -C "$work" -xf "$tarball" --strip-components=1
cd "$work"
# LIBICONV_PLUG: a GNU libiconv header in /usr/local/include (builder only) must not rename iconv_open; the appliance uses libc iconv.
CFLAGS='-g -O2 -DLIBICONV_PLUG' OPENSSL_CFLAGS='' OPENSSL_LIBS='-lssl -lcrypto' \
ZLIB_CFLAGS='' ZLIB_LIBS='-lz' SODIUM_CFLAGS=-I/usr/local/include SODIUM_LIBS='-L/usr/local/lib -lsodium' \
SQLITE_CFLAGS=-I/opt/fs-sqlite/usr/include/private/sqlite3 SQLITE_LIBS='-L/opt/fs-sqlite/usr/lib -lprivatesqlite3' \
./configure --prefix="$prefix" --disable-all --enable-cli --disable-cgi --disable-phpdbg --disable-opcache-jit --with-libxml --enable-dom \
	--enable-filter --enable-ctype --enable-tokenizer --with-external-pcre --with-sqlite3 --enable-posix --enable-pcntl --enable-sockets \
	--enable-session --enable-fileinfo --with-openssl --with-ldap="$ldap" --with-iconv --with-zlib --with-sodium > /build/php-openssl-configure.log 2>&1
make -j8 > /build/php-openssl-make.log 2>&1
make install > /build/php-openssl-install.log 2>&1
"$prefix/bin/php" -m | tr '\n' ' '
echo
# shellcheck disable=SC2016
"$prefix/bin/php" -r 'echo strlen(openssl_encrypt("x", "aes-256-gcm", str_repeat("k", 32), OPENSSL_RAW_DATA, str_repeat("i", 12), $tag)), " openssl-ok", PHP_EOL;'
"$prefix/bin/php" -r 'echo LDAP_OPT_X_TLS_REQUIRE_CERT > 0 && ldap_connect("ldap://127.0.0.1:389") instanceof LDAP\Connection ? "ldap-ok" : "ldap-missing", PHP_EOL;'
# shellcheck disable=SC2016
"$prefix/bin/php" -r '$k = sodium_crypto_box_keypair(); echo iconv("UTF-8", "ASCII//TRANSLIT", "x") === "x" && gzdecode(gzencode("z")) === "z" && strlen(sodium_crypto_box_publickey($k)) === 32 ? "iconv-zlib-sodium-ok" : "missing", PHP_EOL;'
