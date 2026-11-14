#!/bin/sh
# pkgbase_seed.sh REPO_DIR ARCH OUT.txz  - build a jail-seed tarball from an extracted FreeBSD pkgbase repo.
# REPO_DIR holds FreeBSD:16:<arch>/ ; ARCH is amd64|aarch64. Needs FreeBSD pkg, tar, xz.
set -eu
REPO_DIR=$1; ARCH=$2; OUT=$3
ABI="FreeBSD:16:$ARCH"
SNAP=$(readlink "$REPO_DIR/$ABI/latest" 2>/dev/null || ls "$REPO_DIR/$ABI" | grep '^16\.snap' | sort | tail -1)
TS=${SNAP#16.snap}
W=$(mktemp -d /var/tmp/seed.XXXXXX)
mkdir -p "$W/conf" "$W/root"
cat > "$W/conf/seed.conf" <<EOC
seed: { url: "file://$REPO_DIR/$ABI/$SNAP", signature_type: "none", enabled: yes }
EOC
# osversion as recorded in the repository metadata
OSV=$(tar -xOf "$REPO_DIR/$ABI/$SNAP/packagesite.pkg" packagesite.yaml 2>/dev/null | grep -m1 -o '"FreeBSD_version":"[0-9]*"' | grep -o '[0-9][0-9]*' || true)
[ -n "$OSV" ] || { echo "no osversion"; exit 1; }
echo "ABI=$ABI OSVERSION=$OSV snapshot=$SNAP"
P() { env ABI="$ABI" ASSUME_ALWAYS_YES=yes pkg --rootdir "$W/root" -o OSVERSION="$OSV" -o REPOS_DIR="$W/conf" -o IGNORE_OSVERSION=yes -o PKG_CACHEDIR="$W/cache" "$@"; }
P update -f -r seed >/dev/null
P rquery -a -r seed '%n' | sort | grep '^FreeBSD-' | grep -Ev '^FreeBSD-(set-|kernel-|src|tests)|-(dbg|tests|lib32|dbg-lib32)$|-(dbg|dev)-lib32$' > "$W/names"
# FreeBSD-*-dev (headers/static libs) stays: the jail compiles ports.
wc -l < "$W/names"
P install -y -r seed $(cat "$W/names") >"$W/install.log" 2>&1 || { tail -30 "$W/install.log"; exit 1; }
tail -3 "$W/install.log"
R=$W/root
# not part of a seed: package database and caches
rm -rf "$R/var/db/pkg" "$R/var/cache/pkg" "$R/var/db/pkg.meta" 2>/dev/null || true
# schg/uchg flags would block touch and later removal
chflags -R noschg,nouchg,nosappnd,nouappnd "$R" 2>/dev/null || true
# fixed timestamps, sorted member order, numeric owners
find "$R" -exec touch -h -d "$(date -u -j -f %Y%m%d%H%M%S "$TS" +%Y-%m-%dT%H:%M:%SZ)" {} + 2>/dev/null || find "$R" -exec touch -h -t "$(echo $TS | cut -c1-12)" {} +
( cd "$R" && find . ! -name . | sed 's|^\./||' | LC_ALL=C sort > "$W/list" )
( cd "$R" && tar -n --numeric-owner --format=ustar -cf - -T "$W/list" ) | xz -6 -T1 -c > "$OUT"
ls -l "$OUT"; sha256 "$OUT"
cp "$W/names" "$OUT.packages"; cp "$W/list" "$OUT.files"
echo "work=$W"
