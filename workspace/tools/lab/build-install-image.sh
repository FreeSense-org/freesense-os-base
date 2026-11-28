#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.
# Builder side of tools/lab_accept.py --fresh: a fresh ZFS install of /build/v2-pkg/repo (build-packages.sh) onto one md disk,
# plus the repository disk. The installer runs from the tree build-packages.sh unpacked.
# Usage: build-install-image.sh NAME. The caller wrote /build/px/cfg-NAME.json (mode 0600; removed here) and /build/px/lab.pub.
# Touches only /build/px and its own md device.
set -eu
z=/build/px
name=$1
cd "$z"
trap 'rm -f "$z/cfg-run.json" "$z/cfg-$name.json"' EXIT
umount "$z/mnt/boot/efi" "$z/mnt" 2>/dev/null || true
if zpool list -H -o name FreeSense >/dev/null 2>&1; then zpool export -f FreeSense; fi
if [ -f "$z/md.list" ]; then
	while read -r unit; do mdconfig -d -u "${unit#md}" -o force 2>/dev/null || true; done < "$z/md.list"
fi
rm -f "$z/md.list" "$z/$name.raw"
mkdir -p "$z/mnt"
kldload -n zfs

# The repository disk (label fsrepo) the installed system reads its packages from.
rm -rf "$z/repostage" "$z/repo.raw"
mkdir -p "$z/repostage"
cp -Rp /build/v2-pkg/repo/. "$z/repostage/"
cp /usr/local/sbin/pkg-static "$z/repostage/pkg-static"
makefs -t ffs -o version=2,label=fsrepo -s 1300m "$z/repo.raw" "$z/repostage" >/dev/null
rm -rf "$z/repostage"

truncate -s 8g "$z/$name.raw"
disk=$(mdconfig -a -t vnode -f "$z/$name.raw")
echo "$disk" > "$z/md.list"
sed -e "s/@D1@/$disk/" "$z/cfg-$name.json" > "$z/cfg-run.json"
chmod 600 "$z/cfg-run.json"
/usr/local/bin/php /build/v2-pkg/src/freesense/src/usr/local/sbin/freesense-install install --config "$z/cfg-run.json" --root "$z/mnt" --yes > "$z/install-$name.log" 2>&1 ||
	{ tail -3 "$z/install-$name.log" | cut -c1-160; echo "install failed"; exit 1; }

# Lab access: the lab SSH key and sshd. Lab image difference only.
zpool import -f -N -R "$z/mnt" FreeSense
zfs mount FreeSense/ROOT/default
zfs mount -a
mkdir -p "$z/mnt/root/.ssh"
chmod 700 "$z/mnt/root/.ssh"
cp "$z/lab.pub" "$z/mnt/root/.ssh/authorized_keys"
chmod 600 "$z/mnt/root/.ssh/authorized_keys"
mkdir -p "$z/mnt/etc/rc.conf.d"
echo 'sshd_enable="YES"' > "$z/mnt/etc/rc.conf.d/sshd"
# The stock sshd_config refuses root; the lab key is the only way in.
echo 'PermitRootLogin prohibit-password' >> "$z/mnt/etc/ssh/sshd_config"
mkdir -p "$z/mnt/media/repo" "$z/mnt/usr/local/etc/pkg/repos"
printf '/dev/ufs/fsrepo\t/media/repo\tufs\tro,failok\t0\t0\n' >> "$z/mnt/etc/fstab"
printf 'FreeSense: {\n\turl: "file:///media/repo",\n\tsignature_type: "none",\n\tenabled: yes\n}\n' > "$z/mnt/usr/local/etc/pkg/repos/FreeSense.conf"
sync
zfs umount -a 2>/dev/null || true
zpool export FreeSense || zpool export -f FreeSense
mdconfig -d -u "${disk#md}" -o force
rm -f "$z/md.list"
echo IMAGE-READY
