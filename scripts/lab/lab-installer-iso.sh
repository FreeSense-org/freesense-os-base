#!/bin/sh
# Lab only: assemble a UEFI installer ISO from an installed FreeSense lab disk and the package repository.
# The system on the disk becomes the read-only live system; its console terminals run `freesense-install menu`
# (InstallerMedia::ttys) and the repository sits on the medium at /media/repo. Replaced by the M9 image pipeline.
# usage: lab-installer-iso.sh INSTALLED_DISK_IMAGE REPOSITORY_DIR OUTPUT_ISO
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -e
disk=$1; repo=$2; out=$3
work=/build/isowork
[ -d "$work" ] && chflags -R 0 "$work" 2>/dev/null; rm -rf "$work"; mkdir -p "$work/src" "$work/root" "$work/esp"
md=$(mdconfig -a -t vnode -f "$disk")
trap 'umount "$work/src" 2>/dev/null; mdconfig -d -u ${md#md} -o force 2>/dev/null' EXIT
fsck_ffs -y /dev/"${md}"p3 >/dev/null 2>&1 || true
mount -o ro /dev/"${md}"p3 "$work/src"
# The live system: everything of the installed root, without machine identity and without the installed configuration.
tar -C "$work/src" -cf - --exclude ./var/log --exclude ./var/tmp --exclude ./tmp --exclude ./cf --exclude ./.sujournal --exclude ./.snap --exclude ./etc/hostid --exclude ./boot/entropy . | tar -C "$work/root" -xpf -
mkdir -p "$work/root/cf" "$work/root/tmp" "$work/root/var/log" "$work/root/var/tmp" "$work/root/media"
cp -R "$repo" "$work/root/media/repo"
# The EFI loader for the El Torito image comes from the installed disk's EFI partition.
mkdir -p "$work/espmnt"; mount -t msdosfs -o ro /dev/"${md}"p1 "$work/espmnt"
mkdir -p "$work/esp/EFI/BOOT"; cp "$work/espmnt/EFI/BOOT/BOOTX64.EFI" "$work/esp/EFI/BOOT/BOOTX64.EFI"; umount "$work/espmnt"
umount "$work/src"
# Boot from the medium: read-only root, memory file systems for what must be written.
printf '%s\n' '/dev/iso9660/FREESENSE / cd9660 ro 0 0' 'tmpfs /tmp tmpfs rw,mode=1777 0 0' 'tmpfs /var tmpfs rw 0 0' 'tmpfs /cf tmpfs rw,mode=700 0 0' > "$work/root/etc/fstab"
{ grep -v -E '^(vfs\.root\.mountfrom|autoboot_delay)=' "$work/root/boot/loader.conf" || true
  printf '%s\n' 'vfs.root.mountfrom="cd9660:iso9660/FREESENSE"' 'autoboot_delay="3"'; } > "$work/loader.new"; cp "$work/loader.new" "$work/root/boot/loader.conf"
# No appliance services on the installer: the installed system's rc.conf names its own host and starts the firewall.
grep -v -E '^(hostname|freesense_|gateway_enable|pf_enable)' "$work/root/etc/rc.conf" > "$work/rc.new" || true
printf '%s\n' 'hostname="freesense-installer"' 'freesense_config_enable="NO"' 'freesense_routing_enable="NO"' 'pf_enable="NO"' >> "$work/rc.new"
cp "$work/rc.new" "$work/root/etc/rc.conf"
php_term=$(grep -c freesense-install "$work/root/etc/ttys" || true)
if [ "$php_term" = 0 ]; then
	# shellcheck disable=SC2016
	/usr/local/bin/php -r 'require "/build/instsrc/usr/local/FreeSense/autoload.php"; $c = chr(92)."FreeSense".chr(92)."Installer".chr(92)."InstallerMedia"; file_put_contents($argv[1], $c::ttys(file_get_contents($argv[1])));' "$work/root/etc/ttys"
fi
# pkg-static must be on the medium for the installer's package step.
[ -x "$work/root/usr/local/sbin/pkg-static" ] || cp /usr/local/sbin/pkg-static "$work/root/usr/local/sbin/pkg-static"
makefs -t msdos -o fat_type=12 -s 4m "$work/efiboot.img" "$work/esp" >/dev/null
cp "$work/efiboot.img" "$work/root/efiboot.img"
rm -f "$out"
(cd "$work/root" && makefs -t cd9660 -o rockridge,label=FREESENSE,publisher=FreeSense -o "bootimage=i386;boot/cdboot" -o no-emul-boot -o "bootimage=efi;efiboot.img" -o no-emul-boot -o platformid=efi "$out" "$work/root")
rm -f "$work/root/efiboot.img"
ls -la "$out"
echo iso-ready
