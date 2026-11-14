#!/bin/sh
# Runs inside the pinned FreeBSD worker VM during pin-target.yml (job jail_seed).
# The workflow prepends REPO_SHA256 and PKG_ARCH (amd64|aarch64), and serves
# pkgbase_seed.sh and repo.tar (the FreeBSD pkgbase repository tarball) at 10.0.2.2:8765.
# The finished seed is streamed back to the host on 10.0.2.2:8766 (run-vm.sh --receive-file).
set -eu
export HOME=/root
phase() { printf 'FreeSense phase: %s\n' "$1"; }
phase pkgbase-fetch
fetch -o /root/pkgbase_seed.sh http://10.0.2.2:8765/pkgbase_seed.sh
fetch -o /root/repo.tar http://10.0.2.2:8765/repo.tar
[ "$(sha256 -q /root/repo.tar)" = "${REPO_SHA256}" ]
phase pkgbase-unpack
mkdir /root/repo
tar -xf /root/repo.tar -C /root/repo
rm -f /root/repo.tar
phase pkgbase-seed
sh /root/pkgbase_seed.sh /root/repo "${PKG_ARCH}" /root/base.txz
phase pkgbase-send
SEED_SHA256=$(sha256 -q /root/base.txz)
nc -N 10.0.2.2 8766 </root/base.txz
printf 'PKGBASE_SEED_SHA256=%s\n' "${SEED_SHA256}"
