#!/bin/sh
# Build the local FreeSense 1.2 preview payload on the native lab builder (disposable FreeBSD guest).
# Starts from the pristine upstream snapshot, adds PHP 8.6.0, the offline package set and the reviewed System.
# Inputs under /var/tmp/refactor12-preview: tree/ (freesense/src, freesense/LICENSE and the System port), pkgs/ (the offline
# package files), plus /var/tmp/refactor12-pristine.raw.gz, /opt/refactor12/php86-final and the PHP source. Run as root.
# Output: payload.raw (seal it with xz) and packages.sha256, source-files.sha256 for the provenance record.
set -eu
umask 022
work=/var/tmp/refactor12-preview
tree=$work/tree
root=$work/root
php_bin=/opt/refactor12/php86-final/bin/php
php_source=/var/tmp/refactor12-src/php-8.6.0
cd "$work"
mkdir -p "$root" "$work/efi"
echo "[1] expand the pristine image (sparse)"
test ! -e payload.raw
gunzip -c /var/tmp/refactor12-pristine.raw.gz | dd of=payload.raw bs=1m conv=sparse 2>/dev/null
test "$(stat -f %z payload.raw)" -eq 6477709312
dev=$(mdconfig -a -t vnode -f payload.raw)
echo "$dev" > md.unit
cleanup() {
    code=$?
    mount -p | awk -v d="$work/efi" '$2==d{f=1} END{exit !f}' && umount "$work/efi"
    mount -p | awk -v d="$root" '$2==d{f=1} END{exit !f}' && umount "$root"
    mdconfig -d -u "$dev" || true
    exit $code
}
trap cleanup EXIT
gpart show "$dev" | awk '$3==2 && $4=="efi"{e=1} $3==5 && $4=="freebsd-ufs"{u=1} END{exit !(e && u)}'
mount -t ufs "/dev/${dev}p5" "$root"
mount -t msdosfs -o ro "/dev/${dev}p2" "$work/efi"
test -f "$work/efi/EFI/BOOT/bootx64.efi"
test ! -e "$root/root/.ssh"
test ! -e "$root/home/freebsd"
test ! -e "$root/cf/conf/freesense/config.xml"
test -z "$(find "$root/etc/ssh" -type f -name 'ssh_host_*' -print)"

echo "[2] base settings"
pw -R "$root" lock root
awk -F: '$3==0 && $2!~/^[*!]/ {exit 1}' "$root/etc/master.passwd"
sysrc -f "$root/etc/rc.conf" hostname=freesense-local sshd_enable=NO nuageinit_enable=NO \
    firstboot_pkg_upgrade_enable=NO network_interfaces=lo0 ifconfig_DEFAULT=NOAUTO \
    ipv6_network_interfaces=none ntpd_enable=NO ntpdate_enable=NO \
    local_unbound_enable=NO inetd_enable=NO sendmail_enable=NONE \
    dumpdev=NO syslogd_flags=-ss freesense_config_enable=YES rc_startmsgs=YES defaultroute_delay=0 >/dev/null
sysrc -f "$root/etc/rc.conf" freesense_routing_enable=YES gateway_enable=NO pf_enable=NO >/dev/null
# The WebUI reads the product version from /etc/version.
echo 2.0.0-DEVELOPMENT >"$root/etc/version"
sysrc -f "$root/boot/loader.conf" autoboot_delay=3 beastie_disable=NO loader_logo=none console=efi,comconsole \
    comconsole_speed=115200 boot_multicons=YES boot_mute=YES init_rc=/usr/local/FreeSense/boot/rc >/dev/null
# FreeBSD accepts uid 0 logins (root and the administrator account) only on terminals marked secure: the physical consoles.
awk 'BEGIN {OFS="\t"}
     $1=="ttyv0" || $1=="ttyu0" {print $1,"\"/usr/local/sbin/freesense-console\"","vt100","on secure"; next}
     $1=="console" {print $1,"none","unknown","off secure"; next}
     $1~/^tty[uv][0-9a-f]+$/ {print $1,"none","unknown","off"; next}
     {print}' "$root/etc/ttys" >"$work/ttys"
install -m 0644 "$work/ttys" "$root/etc/ttys"

echo "[3] PHP 8.6.0 runtime"
install -d -m 0755 "$root/usr/local/bin" "$root/usr/local/lib"
test "$("$php_bin" -r 'echo PHP_VERSION;')" = 8.6.0
install -m 0555 "$php_bin" "$root/usr/local/bin/php"
ldd "$php_bin" >"$work/php-ldd.txt"
awk '$3 ~ /^\/usr\/local\/lib\// { print $3 }' "$work/php-ldd.txt" | sort -u >"$work/php-libs.txt"
while IFS= read -r lib; do
    install -m 0555 "$lib" "$root$lib"
    package=$(pkg which -q "$(realpath "$lib")")
    test -n "$package"
    install -d -m 0755 "$root/usr/local/share/licenses/$package"
    for license in /usr/local/share/licenses/"$package"/*; do
        install -m 0444 "$license" "$root/usr/local/share/licenses/$package/"
    done
done <"$work/php-libs.txt"
pc_license_dir=$(pkg query '%n-%v' pcre2)
install -d -m 0755 "$root/usr/local/share/licenses/$pc_license_dir"
install -m 0444 /usr/local/share/doc/pcre2/COPYING "$root/usr/local/share/licenses/$pc_license_dir/COPYING"
php_licenses="$root/usr/local/share/licenses/php-8.6.0"
for license in LICENSE Zend/asm/LICENSE ext/date/lib/LICENSE.rst ext/lexbor/LICENSE \
    ext/standard/libavifinfo/LICENSE ext/uri/uriparser/COPYING.BSD-3-Clause; do
    install -d -m 0755 "$php_licenses/$(dirname "$license")"
    install -m 0444 "$php_source/$license" "$php_licenses/$license"
done
chroot "$root" /sbin/ldconfig /lib /usr/lib /usr/local/lib
chroot "$root" /usr/local/bin/php -r 'exit(PHP_VERSION === "8.6.0" && !array_diff(["dom","filter","ctype","tokenizer"],get_loaded_extensions()) ? 0 : 1);'

echo "[4] offline service packages"
: > "$work/packages.sha256"
for name in expat libffi indexinfo gettext-runtime readline mpdecimal liblz4 zstd icu python312 boost-libs log4cplus kea \
    pcre2 fcgi-devkit fcgiwrap nginx-lite dpinger radvd; do
    file=
    for candidate in "$work"/pkgs/"$name"*.pkg; do
        file=$candidate
        break
    done
    test -f "$file"
    echo "$(sha256 -q "$file")  $(basename "$file")" >> "$work/packages.sha256"
    pkg -r "$root" add -M "$file" >"$work/pkg-$name.log" 2>&1 || { cat "$work/pkg-$name.log"; exit 1; }
done
for bin in usr/local/sbin/kea-dhcp4 usr/local/sbin/dpinger usr/local/sbin/radvd usr/local/sbin/nginx usr/local/bin/fcgiwrap usr/local/sbin/fcgiwrap; do
    test -x "$root/$bin" && echo "present: $bin"
done
chroot "$root" /sbin/ldconfig /lib /usr/lib /usr/local/lib

echo "[5] FreeSense System"
port="$tree/freesense-system-ports/security/FreeSense-system"
sh "$port/files/stage-system.sh" "$tree/freesense/src" "$root" "$port/pkg-plist"
install -d -m 0755 "$root/usr/local/share/licenses/FreeSense-local"
install -m 0444 "$tree/freesense/LICENSE" "$root/usr/local/share/licenses/FreeSense-local/Apache-2.0"
test -x "$root/usr/local/sbin/freesense-console"
test -x "$root/usr/local/FreeSense/boot/rc"
test ! -e "$root/cf/conf/freesense/config.xml"
test -z "$(find "$root" -type f \( -name authorized_keys -o -name 'ssh_host_*_key' \) -print)"
(cd "$tree/freesense/src" && find . -type f -exec sha256 -r {} + | sort) >"$work/source-files.sha256"
miss=0
while read -r digest path; do
    if [ -f "$root/$path" ]; then
        [ "$(sha256 -q "$root/$path")" = "$digest" ] || { echo "DIFFERS: $path"; miss=1; }
    else
        echo "NOT INSTALLED: $path"; miss=$((miss+1))
    fi
done <"$work/source-files.sha256"
echo "source files not byte-identical in the image: $miss"
umount "$work/efi"
umount "$root"
fsck_ufs -fn "/dev/${dev}p5"
mdconfig -d -u "$dev"
trap - EXIT
echo "[6] seal"
sha256 payload.raw
stat -f %z payload.raw
echo done-build
