#!/bin/sh
# Lab only: package the staged System as FreeSense-system and add it to /build/repo2. The real build is the freesense-system-ports port (needs a ports tree).
# pkg -r does not run the post-install script, so the lab image sets freesense_config_enable in rc.conf (see lab-disk-image.sh).
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -e
rm -rf /build/sys; mkdir -p /build/sys/src /build/sys/stage; tar -xf /build/syssrc.tar -C /build/sys/src
cd /build/sys; sh /build/stage-system.sh /build/sys/src /build/sys/stage /build/pkg-plist 2>&1 | tail -3
find stage -type f | wc -l; head -3 /build/pkg-plist; grep -c "^@" /build/pkg-plist
set -e
M=/build/sysmeta; rm -rf $M; mkdir -p $M
# Dependencies follow the System port's RUN_DEPENDS (freesense-system-ports/security/FreeSense-system/Makefile, copied to
# /build/FreeSense-system.Makefile by the caller), with the versions in repo2. FreeSense-platform-abi is not built for the lab.
makefile=/build/FreeSense-system.Makefile
[ -f "$makefile" ] || { echo "lab-system-package: $makefile missing (copy the System port Makefile there)" >&2; exit 1; }
# Each `name>=version:origin` word of a RUN_DEPENDS+= line and its backslash continuations; FreeSense-php86 stands in for USE_PHP.
sysdeps="FreeSense-php86 $(awk '
/^RUN_DEPENDS\+?=/ { on = 1; sub(/^RUN_DEPENDS\+?=/, "") }
on {
	more = substr($0, length($0)) == "\\"
	if (more) $0 = substr($0, 1, length($0) - 1)
	n = split($0, w, /[ \t]+/)
	for (i = 1; i <= n; i++) if (w[i] ~ /:/) { split(w[i], d, /[<>=]/); print d[1] }
	if (!more) on = 0
}' "$makefile" | grep -v '^FreeSense-platform-abi$')"
deps=""
for n in $sysdeps; do
	f=""
	for c in /build/repo2/All/"$n"-[0-9]*.pkg; do [ -f "$c" ] && [ "$(pkg-static query -F "$c" %n)" = "$n" ] && f=$c; done
	[ -n "$f" ] || { echo "lab-system-package: $n (System RUN_DEPENDS) is not in /build/repo2" >&2; exit 1; }
	deps="$deps${deps:+, }$n: {origin: $(pkg-static query -F "$f" %o), version: \"$(pkg-static query -F "$f" %v)\"}"
done
echo "lab-system-package: deps $(printf '%s' "$sysdeps" | wc -w): $(printf '%s' "$sysdeps" | tr '\n' ' ')"
cat > $M/+MANIFEST <<X
name: FreeSense-system
version: "2.0.0"
origin: security/FreeSense-system
comment: FreeSense System files
www: https://freesense.org
maintainer: dev@freesense.org
prefix: /usr/local
arch: FreeBSD:16:amd64
licenselogic: single
licenses: [APACHE20]
desc: FreeSense 2.0 System lab package
deps: {$deps}
scripts: {post-install: "mkdir -p /etc/rc.conf.d && echo freesense_config_enable=YES > /etc/rc.conf.d/freesense_config"}
X
sed 's#^@([a-z]*,[a-z]*,\([0-9]*\))#@(root,wheel,\1)#' /build/pkg-plist > /build/sys.plist
rm -rf /build/syspkg; mkdir -p /build/syspkg
pkg-static create -r /build/sys/stage -m $M -p /build/sys.plist -o /build/syspkg 2>&1 | tail -3
ls -l /build/syspkg
