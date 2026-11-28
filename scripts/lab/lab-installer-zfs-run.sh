#!/bin/sh
# Lab only: scripted bsdinstall with a ZFS root (zroot/ROOT/default boot-environment layout) into /build/zfs.raw. The builder boots BIOS, so the disk has freebsd-boot plus freebsd-zfs. The installer ends with an umount error in this chroot because ZFS mounts are global; export the pool by hand (see ledger).
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
W=/build/img4/root
for m in $W/mnt/boot/efi $W/mnt/dev $W/mnt $W/repo $W/dev /mnt/t1 /mnt/t2; do umount -f "$m" 2>/dev/null; done
zpool list 2>/dev/null | grep -q zroot && zpool export -f zroot 2>/dev/null
for u in $(mdconfig -l); do mdconfig -d -f -u "${u#md}" 2>/dev/null; done
kldload zfs 2>/dev/null
rm -f /build/zfs.raw; truncate -s 6g /build/zfs.raw
md=$(mdconfig -a -t vnode -f /build/zfs.raw); echo "disk $md"
mount -t devfs devfs $W/dev
mkdir -p $W/repo $W/tmp/repos; mount -t nullfs /build/repo2 $W/repo
echo 'FreeSense-base: { url: "file:///repo", signature_type: "none", enabled: yes }' > $W/tmp/repos/FreeSense-base.conf
cp /usr/local/sbin/pkg-static $W/usr/local/sbin/pkg
cat > $W/tmp/install.conf <<X
export ZFSBOOT_DISKS="$md"
export ZFSBOOT_VDEV_TYPE=stripe
export ZFSBOOT_SWAP_SIZE=0
export ZFSBOOT_CONFIRM_LAYOUT=0
export BSDINSTALL_PKG_REPOS_DIR=/tmp/repos
export nonInteractive=YES
export IGNORE_OSVERSION=yes
#!/bin/sh
sysrc hostname=freesense
X
chroot $W /bin/sh -c 'export BSDINSTALL_PKG_REPOS_DIR=/tmp/repos nonInteractive=YES; bsdinstall script /tmp/install.conf' > /build/inst-zfs.log 2>&1
echo "installer rc=$?"
umount $W/repo $W/dev 2>/dev/null
gpart show "$md"
zpool list 2>/dev/null; zpool import 2>/dev/null | head -4
