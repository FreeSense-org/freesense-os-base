#!/bin/sh
# Lab only: turn /build/img4/root (pkg-installed FreeSense base) into a bootable UEFI disk image.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -e
W=/build/img4; R=$W/root; cd $W
echo '/dev/gpt/rootfs / ufs rw 1 1' > $R/etc/fstab
printf 'hostname="freesense"\nsendmail_enable="NO"\nsshd_enable="YES"\nifconfig_em0="DHCP"\nfreesense_routing_enable="YES"\ngateway_enable="NO"\npf_enable="NO"\n' > $R/etc/rc.conf
echo 'freesense_config_enable="YES"' >> $R/etc/rc.conf
echo 2.0.0-DEVELOPMENT > $R/etc/version
printf 'console="efi,comconsole"\nboot_serial="YES"\nautoboot_delay="3"\nboot_mute="YES"\n' > $R/boot/loader.conf
echo 'PermitRootLogin prohibit-password' >> $R/etc/ssh/sshd_config
mkdir -p $R/root/.ssh; cp /root/.ssh/authorized_keys $R/root/.ssh/; chmod 700 $R/root/.ssh; chmod 600 $R/root/.ssh/authorized_keys
mkdir -p esp/EFI/BOOT; cp $R/boot/loader.efi esp/EFI/BOOT/BOOTX64.EFI
makefs -t msdos -o fat_type=16 -s 64m esp.img esp >/dev/null
makefs -t ffs -o version=2,label=rootfs -s 2000m root.img $R >/dev/null
mkimg -s gpt -p efi:=esp.img -p freebsd-ufs/rootfs:=root.img -o freesense.raw; ls -l freesense.raw
# Same console entries as build-payload.sh: the physical consoles run the FreeSense console.
awk 'BEGIN {OFS="\t"}
     $1=="ttyv0" || $1=="ttyu0" {print $1,"\"/usr/local/sbin/freesense-console\"","vt100","on secure"; next}
     $1=="console" {print $1,"none","unknown","off secure"; next}
     $1~/^tty[uv][0-9a-f]+$/ {print $1,"none","unknown","off"; next}
     {print}' $R/etc/ttys > /build/ttys.new && install -m 0644 /build/ttys.new $R/etc/ttys
cd /build/img4; rm -f root.img freesense.raw
makefs -t ffs -o version=2,label=rootfs -s 2000m root.img root >/dev/null
mkimg -s gpt -p efi:=esp.img -p freebsd-ufs/rootfs:=root.img -o freesense.raw
