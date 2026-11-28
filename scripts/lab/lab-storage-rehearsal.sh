#!/bin/sh
# Lab only: run freesense-install storage on file-backed md disks on the builder: prepare, boot files, finalize, readback, teardown.
# Needs /build/instsrc.tar (tar of freesense/src) and boot files in /build/img4/root/boot. Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# usage: rehearse.sh NAME NDISKS firmware fs layout swap
name=$1; n=$2; fw=$3; fs=$4; layout=$5; swap=$6
rm -rf /build/instsrc; mkdir -p /build/instsrc; tar -xf /build/instsrc.tar -C /build/instsrc
INST="/usr/local/bin/php /build/instsrc/usr/local/sbin/freesense-install"
ROOT=/mnt/fsroot; BOOT=/build/img4/root/boot
mkdir -p $ROOT
disks=""; mds=""
for i in $(seq 1 "$n"); do
  f=/build/rh-$name-$i.raw; rm -f "$f"; truncate -s 9g "$f"
  md=$(mdconfig -a -t vnode -f "$f"); mds="$mds $md"; disks="$disks,$md"
done
disks=${disks#,}
echo "=== $name: disks $disks fw=$fw fs=$fs layout=$layout swap=$swap"
args="--disks $disks --firmware $fw --fs $fs --layout $layout --swap $swap --pool rhpool --root $ROOT"
$INST storage "$args" --phase prepare --yes > /build/rh-"$name".out 2>&1; echo "prepare rc=$?"
mkdir -p $ROOT/boot; cp $BOOT/loader.efi $BOOT/pmbr $BOOT/gptboot $BOOT/gptzfsboot $ROOT/boot/ 2>&1 | head -2
$INST storage "$args" --phase finalize --yes >> /build/rh-"$name".out 2>&1; echo "finalize rc=$?"
first=${mds%% *}; first=$(echo "$mds" | cut -d' ' -f1)
gpart show -l "$first" | sed 's/^/  /'
if [ "$fs" = zfs ]; then zpool status rhpool | sed -n '1,12p' | sed 's/^/  /'; zfs list -r -o name,mountpoint rhpool | sed 's/^/  /'; zpool get bootfs rhpool | tail -1 | sed 's/^/  /'; fi
if [ "$fw" = uefi ]; then mkdir -p /mnt/e; mount -t msdosfs /dev/gpt/efiboot0 /mnt/e && find /mnt/e -type f | sed 's/^/  esp: /'; umount /mnt/e; fi
$INST storage "$args" --phase teardown --yes >> /build/rh-"$name".out 2>&1; echo "teardown rc=$?"
for m in $mds; do mdconfig -d -f -u "${m#md}" 2>/dev/null; done
tail -3 /build/rh-"$name".out
rm -f /build/rh-"$name"-*.raw
