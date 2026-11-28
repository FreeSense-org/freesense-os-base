#!/bin/sh
# Stage iso: install the installer's package list into an empty root exactly as the installer does, make that root the
# live system, put the repository on it, and write a hybrid ISO (UEFI and BIOS, from CD or written to a USB stick).
# Usage: iso.sh MEDIA_REPO OUT_ISO WORK PKG_STATIC ABI LABEL PUBLIC_KEY PACKAGE...
#   PUBLIC_KEY verifies the signed repository, here and on the medium; "-" for an unsigned test repository.
# The hybrid layout follows release/amd64/mkisoimages.sh of FreeBSD.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
set -eu
media=$1 out=$2 work=$3 pkg=$4 abi=$5 label=$6 key=$7
shift 7
export ASSUME_ALWAYS_YES=yes
root=$work/root
# Where the installer looks for the key of its medium (Installer\PackageInstaller::MEDIA_KEY). pkg opens a pubkey path on
# the running system, never inside --rootdir: here the file given, on the medium this path.
keypath=/usr/share/keys/freesense/pkg.pub
mkdir -p "$root/dev" "$root/tmp" "$work/repos" "$work/esp/EFI/BOOT"
chmod 1777 "$root/tmp"
verify='signature_type: "none"'
if [ "$key" != - ]; then
	mkdir -p "$root${keypath%/*}"
	install -m 0644 "$key" "$root$keypath"
	verify="signature_type: \"pubkey\", pubkey: \"$key\""
fi
printf 'FreeSense: { url: "file://%s", %s, enabled: yes }\n' "$media" "$verify" > "$work/repos/FreeSense.conf"
mount -t devfs devfs "$root/dev"
trap 'umount "$root/dev" 2>/dev/null || true' EXIT

echo "build/iso: installing the live system"
into() { "$pkg" --rootdir "$root" --repo-conf-dir "$work/repos" -o IGNORE_OSVERSION=yes -o ABI="$abi" "$@"; }
into update -f
# pkg update succeeds even when the signature is wrong: the packages must be visible.
visible=$(into rquery -r FreeSense '%n' | wc -l | tr -d ' ')
[ "$visible" -gt 0 ] || { echo "ERROR: no package of the repository is visible; its signature does not match the key" >&2; exit 1; }
echo "build/iso: $visible packages in the repository"
into install -y -r FreeSense "$@"

for file in boot/loader.efi boot/cdboot boot/pmbr boot/isoboot boot/kernel/kernel usr/local/bin/php usr/local/sbin/pkg-static \
	usr/local/sbin/freesense-install usr/local/FreeSense/autoload.php etc/ttys etc/master.passwd; do
	[ -f "$root/$file" ] || { echo "ERROR: the installed packages have no /$file" >&2; exit 1; }
done
[ -f "$root/etc/spwd.db" ] || pwd_mkdb -p -d "$root/etc" "$root/etc/master.passwd"
[ -f "$root/etc/login.conf.db" ] || cap_mkdb "$root/etc/login.conf"
[ -f "$root/boot/kernel/linker.hints" ] || kldxref "$root/boot/kernel"

echo "build/iso: checking the installer on the live system's own PHP"
# The extensions the System's port asks for must load in this root.
# shellcheck disable=SC2016 # PHP, not shell
chroot "$root" /usr/local/bin/php -r 'foreach (["ctype", "dom", "filter", "iconv", "intl", "ldap", "openssl", "pcntl", "posix", "sodium", "sqlite3", "zlib"] as $e) { if (!extension_loaded($e)) { fwrite(STDERR, "ERROR: PHP extension $e does not load\n"); exit(1); } } echo "build/iso: PHP ", PHP_VERSION, " with every extension\n";'
status=0
chroot "$root" /usr/local/bin/php /usr/local/sbin/freesense-install > "$work/usage.log" 2>&1 || status=$?
{ [ "$status" -eq 64 ] && grep -q '^Usage: freesense-install' "$work/usage.log"; } ||
	{ cat "$work/usage.log" >&2; echo "ERROR: freesense-install does not start (status $status)" >&2; exit 1; }

echo "build/iso: making it the medium"
# shellcheck disable=SC2016 # PHP, not shell
chroot "$root" /usr/local/bin/php -r 'require "/usr/local/FreeSense/autoload.php"; foreach (\FreeSense\Installer\InstallerMedia::files($argv[1], (string)file_get_contents("/etc/ttys")) as $path => $content) { if (file_put_contents($path, $content) !== strlen($content)) { fwrite(STDERR, "ERROR: cannot write $path\n"); exit(1); } }' "$label"
umount "$root/dev"
chflags -R noschg "$root/var/cache/pkg" "$root/tmp"
rm -rf "$root/var/cache/pkg" "$root/var/db/pkg/repos" "$root/tmp"
mkdir -p "$root/var/cache/pkg" "$root/tmp" "$root/cf" "$root/media/repo" "$root/mnt"
chmod 1777 "$root/tmp"
rm -f "$root/etc/hostid" "$root/boot/entropy"
cp -R "$media/." "$root/media/repo/"

echo "build/iso: writing $(basename "$out")"
cp "$root/boot/loader.efi" "$work/esp/EFI/BOOT/BOOTX64.EFI"
makefs -t msdos -o fat_type=12 -o sectors_per_cluster=1 -o volume_label=EFISYS -s 4096k "$work/efiboot.img" "$work/esp"
makefs -D -N "$root/etc" -t cd9660 -o "bootimage=i386;$root/boot/cdboot" -o no-emul-boot \
	-o "bootimage=i386;$work/efiboot.img" -o no-emul-boot -o platformid=efi \
	-o rockridge -o label="$label" -o publisher="The FreeSense Project" "$out" "$root"

# A partition table in the unused first 32 KiB makes the same file boot from a USB stick: isoboot for BIOS, and an EFI
# system partition that points at the boot image inside the ISO.
esp=
for entry in $(etdump --format shell "$out"); do
	eval "$entry"
	# shellcheck disable=SC2154 # set by etdump
	if [ "$et_platform" = efi ]; then
		esp="efi::$((et_sectors * 512)):$((et_lba * 2048))"
		break
	fi
done
[ -n "$esp" ] || { echo "ERROR: no EFI boot image in the ISO" >&2; exit 1; }
mkimg -s gpt --capacity "$(stat -f %z "$out")" -b "$root/boot/pmbr" -p freebsd-boot:="$root/boot/isoboot" -p "$esp" \
	-o "$work/hybrid.img"
dd if="$work/hybrid.img" of="$out" bs=32k count=1 conv=notrunc status=none
echo ISO-READY
