# Assemble one board-specific ARM64 UFS appliance from the sealed repository
# closure. The image is structurally verified here; physical verification is a
# separate, monotonic metadata promotion keyed by this artifact fingerprint.
prepare_release_inputs
verify_release_channel

[ "${ARCHITECTURE}" = arm64 ] && [ "${PACKAGE_ARCH}" = aarch64 ] || {
  echo "appliances require the canonical ARM64 target" >&2; exit 1;
}
case "${IMAGE_PROFILE}" in arm64-rpi4b|arm64-rpi5-d0) : ;; *)
  echo "unsupported appliance profile" >&2; exit 1;;
esac
[ "${APPLIANCE_FILESYSTEM}" = ufs ] && [ "${APPLIANCE_FORMAT}" = img ] && \
  [ "${APPLIANCE_COMPRESSION}" = xz ] && [ "${PARTITION_SCHEME}" = mbr ] || {
  echo "invalid appliance storage policy" >&2; exit 1;
}

phase appliance-root
root=/root/appliance-root
rm -rf "${root}"
mkdir -p "${root}/tmp/assembly-repo" "${root}/tmp/assembly-repos" \
  "${root}/tmp/assembly-keys" "${root}/tmp/assembly-cache" \
  "${root}/usr/local/etc/pkg/repos" "${root}/dev" "${root}/conf"
tar -xpf /root/jail-base.txz -C "${root}"
keys=/root/freesense-src/src/usr/local/share/FreeSense/keys/pkg
cp -a /root/system-repo/. "${root}/tmp/assembly-repo/"
cp -a "${keys}/." "${root}/tmp/assembly-keys/"
pkg_package=$(find /root/system-repo/All -name 'pkg-[0-9]*.pkg' -type f | sort | tail -1)
[ -n "${pkg_package}" ] || { echo "appliance pkg bootstrap is missing" >&2; exit 1; }
tar -xpf "${pkg_package}" -C "${root}" --exclude '+*'
cp "${pkg_package}" "${root}/tmp/pkg-bootstrap.pkg"
cat >"${root}/tmp/assembly-repos/FreeSenseAssembly.conf" <<'EOF'
FreeSenseAssembly: {
  url: "file:///tmp/assembly-repo",
  mirror_type: "none",
  enabled: yes,
  signature_type: "fingerprints",
  fingerprints: "/tmp/assembly-keys"
}
EOF
mount -t devfs devfs "${root}/dev"
if [ -x /usr/local/bin/qemu-aarch64-static ]; then
  mkdir -p "${root}/usr/local/bin"
  cp /usr/local/bin/qemu-aarch64-static "${root}/usr/local/bin/"
fi
chroot "${root}" /usr/bin/env PKG_INSTALL_EPOCH="${SOURCE_DATE_EPOCH}" /bin/sh -c '
  set -eu
  pkg add /tmp/pkg-bootstrap.pkg
  pkg -o REPOS_DIR=/tmp/assembly-repos -o PKG_CACHEDIR=/tmp/assembly-cache \
    install -y -r FreeSenseAssembly FreeSense FreeSense-base \
    FreeSense-kernel-FreeSense FreeSense-rc FreeSense-system \
    FreeSense-default-config-serial FreeSense-repoc
  pkg -o REPOS_DIR=/tmp/assembly-repos -o PKG_CACHEDIR=/tmp/assembly-cache \
    install -f -y -r FreeSenseAssembly FreeSense-system
  ! pkg info -e FreeSense-cloud-init
  ! pkg info -e qemu-guest-agent
'
umount "${root}/dev"
rm -rf "${root}/tmp/assembly-repo" "${root}/tmp/assembly-repos" \
  "${root}/tmp/assembly-keys" "${root}/tmp/assembly-cache" \
  "${root}/tmp/pkg-bootstrap.pkg" "${root}/usr/local/bin/qemu-aarch64-static" \
  "${root}/var/lib/cloud" "${root}/var/db/cloud-init"
cat >"${root}/usr/local/etc/pkg/repos/FreeSense.conf" <<EOF
FreeBSD: { enabled: no }
FreeBSD-kmods: { enabled: no }
FreeSense-system: { url: "${PUBLIC_BASE_URL}/artifacts/system/${SYSTEM_ID}/${PACKAGE_ARCH}", mirror_type: "none", enabled: yes, signature_type: "fingerprints", fingerprints: "/usr/local/share/FreeSense/keys/pkg" }
FreeSense-packages: { url: "${PUBLIC_BASE_URL}/artifacts/packages/${PACKAGE_TRAIN}/${PACKAGES_ID}/${PACKAGE_ARCH}", mirror_type: "none", enabled: yes, signature_type: "fingerprints", fingerprints: "/usr/local/share/FreeSense/keys/pkg" }
EOF
install -m 0444 /tmp/channel-payload.json "${root}/usr/local/etc/freesense-channel.json"
install -m 0444 /tmp/channel-signature.bin "${root}/usr/local/etc/freesense-channel.sig"
printf '%s\n' "${PRODUCT_VERSION}" >"${root}/etc/version"
cat >>"${root}/etc/rc.conf" <<'EOF'
growfs_enable="YES"
growfs_swap_size="0"
EOF
cat >"${root}/boot.config" <<'EOF'
-S115200 -Dh
EOF
cat >>"${root}/boot/loader.conf" <<'EOF'
autoboot_delay="3"
boot_multicons="YES"
boot_serial="YES"
comconsole_speed="115200"
EOF
touch "${root}/root/force_growfs" "${root}/firstboot"
mkdir -p "${root}/usr/local/share/FreeSense"
printf '%s\n' "${FINGERPRINT}" >"${root}/usr/local/share/FreeSense/appliance-${IMAGE_PROFILE}.complete"

phase appliance-disk
raw=/root/FreeSense-appliance.img
truncate -s 8G "${raw}"
md=$(mdconfig -a -t vnode -f "${raw}")
cleanup_appliance() {
  umount /mnt/appliance-boot 2>/dev/null || true
  umount /mnt/appliance-root 2>/dev/null || true
  mdconfig -d -u "${md#md}" 2>/dev/null || true
}
trap cleanup_appliance EXIT INT TERM
gpart create -s mbr "${md}"
mkdir -p /mnt/appliance-boot /mnt/appliance-root
if [ "${IMAGE_PROFILE}" = arm64-rpi4b ]; then
  fetch -o /root/RPI.conf "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/arm64/RPI.conf"
  grep -q 'EMBEDDEDPORTS="sysutils/u-boot-rpi-arm64 sysutils/rpi-firmware"' /root/RPI.conf
  grep -q 'FAT_TYPE="16"' /root/RPI.conf
  grep -q 'PART_SCHEME="MBR"' /root/RPI.conf
  gpart add -a 1m -s 50m -t fat16 "${md}"
  newfs_msdos -F 16 -L FREESENSE "/dev/${md}s1"
else
  gpart add -a 1m -s 260m -t fat32lba "${md}"
  newfs_msdos -F 32 -c 1 -L FREESENSE "/dev/${md}s1"
fi
gpart add -a 1m -t freebsd "${md}"
gpart create -s bsd "${md}s2"
gpart add -a 1m -t freebsd-ufs "${md}s2"
newfs -U -L FreeSense "/dev/${md}s2a"
mount_msdosfs "/dev/${md}s1" /mnt/appliance-boot
mount "/dev/${md}s2a" /mnt/appliance-root
cat >"${root}/etc/fstab" <<'EOF'
/dev/ufs/FreeSense / ufs rw,noatime 1 1
/dev/msdosfs/FREESENSE /boot/efi msdosfs rw 2 2
EOF
tar -C "${root}" -cpf - . | tar -C /mnt/appliance-root -xpf -
mkdir -p /mnt/appliance-root/boot/efi

phase appliance-verify-root
partition_table=$(gpart show -p "${md}") || {
  echo "appliance partition table cannot be read" >&2; exit 1;
}
printf '%s\n' "${partition_table}"
printf '%s\n' "${partition_table}" | grep -Eq '[[:space:]]MBR([[:space:]]|$)' || {
  echo "appliance disk does not use MBR" >&2; exit 1;
}
printf '%s\n' "${partition_table}" | grep -Eq '[[:space:]]freebsd([[:space:]]|$)' || {
  echo "appliance disk is missing its FreeBSD partition" >&2; exit 1;
}
root_type=$(fstyp "/dev/${md}s2a") || {
  echo "appliance root filesystem cannot be identified" >&2; exit 1;
}
[ "${root_type}" = ufs ] || {
  echo "appliance root filesystem is not UFS: ${root_type}" >&2; exit 1;
}
test -s "/mnt/appliance-root/boot/kernel/kernel" -o \
  -s "/mnt/appliance-root/boot/kernel/kernel.gz" || {
  echo "appliance root is missing the ARM64 kernel" >&2; exit 1;
}
test -s "/mnt/appliance-root/usr/local/share/FreeSense/appliance-${IMAGE_PROFILE}.complete" || {
  echo "appliance root is missing its board completion marker" >&2; exit 1;
}
grep -q '/dev/ufs/FreeSense' /mnt/appliance-root/etc/fstab || {
  echo "appliance fstab is missing its UFS root label" >&2; exit 1;
}
grep -q '/dev/msdosfs/FREESENSE' /mnt/appliance-root/etc/fstab || {
  echo "appliance fstab is missing its FAT boot label" >&2; exit 1;
}
grep -q 'growfs_enable="YES"' /mnt/appliance-root/etc/rc.conf || {
  echo "appliance root-growth service is not enabled" >&2; exit 1;
}
test ! -e /mnt/appliance-root/var/lib/cloud || {
  echo "appliance root contains cloud-init state" >&2; exit 1;
}
test ! -e /mnt/appliance-root/usr/local/bin/qemu-aarch64-static || {
  echo "appliance root contains its build emulator" >&2; exit 1;
}
for required_package in FreeSense-base FreeSense-kernel-FreeSense \
  FreeSense-system FreeSense-default-config-serial FreeSense-repoc; do
  pkg -r /mnt/appliance-root info -e "${required_package}" >/dev/null || {
    echo "appliance package database is missing ${required_package}" >&2
    pkg -r /mnt/appliance-root info >&2 || true
    exit 1
  }
done

phase appliance-boot-inputs
mkdir -p /mnt/appliance-boot/EFI/BOOT
if [ "${IMAGE_PROFILE}" = arm64-rpi4b ]; then
  clone_exact https://github.com/freebsd/freebsd-ports.git /root/freebsd-ports "${PORTS_SHA}"
  env BATCH=yes WRKDIRPREFIX=/root/ports-work make -C /root/freebsd-ports/sysutils/u-boot-rpi-arm64 install clean
  env BATCH=yes WRKDIRPREFIX=/root/ports-work make -C /root/freebsd-ports/sysutils/rpi-firmware install clean
  cp /usr/local/share/u-boot/u-boot-rpi-arm64/u-boot.bin /mnt/appliance-boot/
  for boot_file in armstub8.bin armstub8-gic.bin bootcode.bin fixup.dat fixup4.dat \
    start.elf start4.elf bcm2711-rpi-4-b.dtb LICENCE.broadcom; do
    cp "/usr/local/share/rpi-firmware/${boot_file}" /mnt/appliance-boot/
  done
  cp /usr/local/share/rpi-firmware/config_arm64.txt /mnt/appliance-boot/config.txt
else
  archive=/root/RPI5_D0.zip
  fetch -o "${archive}" "$(printf '%s' "${BOOT_INPUTS}" | jq -er .url)"
  test "$(sha256 -q "${archive}")" = "$(printf '%s' "${BOOT_INPUTS}" | jq -er .archive_sha256)"
  upload_immutable "${archive}" "R2:${R2_BUCKET}/${PREFIX}/inputs/sha256/$(sha256 -q "${archive}")"
  mkdir -p /root/rpi5-uefi
  unzip -q "${archive}" -d /root/rpi5-uefi
  cp /root/rpi5-uefi/RPI_EFI.fd /root/rpi5-uefi/config.txt \
    /root/rpi5-uefi/bcm2712-d-rpi-5-b.dtb \
    /root/rpi5-uefi/bcm2712-rpi-5-b.dtb \
    /root/rpi5-uefi/bcm2712d0-rpi-5-b.dtb /mnt/appliance-boot/
fi
cp "${root}/boot/loader.efi" /mnt/appliance-boot/EFI/BOOT/BOOTAA64.EFI
sync

phase appliance-verify-boot
test -s /mnt/appliance-boot/EFI/BOOT/BOOTAA64.EFI || {
  echo "${IMAGE_PROFILE} appliance is missing BOOTAA64.EFI" >&2; exit 1;
}
if [ "${IMAGE_PROFILE}" = arm64-rpi4b ]; then
  test -s /mnt/appliance-boot/u-boot.bin || {
    echo "Pi 4 appliance is missing U-Boot" >&2; exit 1;
  }
  test -s /mnt/appliance-boot/bcm2711-rpi-4-b.dtb || {
    echo "Pi 4 appliance is missing its board DTB" >&2; exit 1;
  }
else
  test -s /mnt/appliance-boot/RPI_EFI.fd || {
    echo "Pi 5 appliance is missing RPI_EFI.fd" >&2; exit 1;
  }
  test -s /mnt/appliance-boot/bcm2712d0-rpi-5-b.dtb || {
    echo "Pi 5 appliance is missing its D0 board DTB" >&2; exit 1;
  }
fi
umount /mnt/appliance-boot
umount /mnt/appliance-root
mdconfig -d -u "${md#md}"
trap - EXIT INT TERM

release_version=${PRODUCT_VERSION%%-*}
name="FreeSense-${release_version}-g${GENERATION}-${IMAGE_PROFILE}.img.xz"
xz -T0 -9 -c "${raw}" >"/root/${name}"
image_sha=$(sha256 -q "/root/${name}")
image_size=$(stat -f %z "/root/${name}")
upload_immutable "/root/${name}" "${RESULT}/${name}"
jq -n --arg fingerprint "${FINGERPRINT}" --arg bundle "${BUNDLE_ID}" \
  --arg system "${SYSTEM_ID}" --arg packages "${PACKAGES_ID}" \
  --arg platform "${IMAGE_PROFILE}" --arg platform_id "${PLATFORM_ID}" --arg file "${name}" --arg sha256 "${image_sha}" \
  --argjson size "${image_size}" --argjson generation "${GENERATION}" \
  --argjson boot_inputs "${BOOT_INPUTS}" --argjson target_models "${TARGET_MODELS}" \
  --arg channel "${CHANNEL}" --arg package_train "${PACKAGE_TRAIN}" \
  '{schema_version:"freesense.appliance/v1",fingerprint:$fingerprint,bundle_fingerprint:$bundle,generation:$generation,channel:$channel,architecture:"arm64",package_arch:"aarch64",platform:$platform,filesystem:"ufs",format:"img",compression:"xz",partition_scheme:"mbr",firmware:(if $platform == "arm64-rpi4b" then ["raspberry-pi-firmware","u-boot"] else ["raspberry-pi","uefi"] end),capabilities:{appliance:true,cloud_init:false,root_growth:true,serial_console:true,hdmi_console:true},target_models:$target_models,boot_inputs:$boot_inputs,hardware_verification:"unverified",file:$file,sha256:$sha256,size:$size,inputs:{platform:$platform_id,system:$system,packages:$packages,package_train:$package_train}}' \
  >/root/assembled.json
upload_immutable /root/assembled.json "${RESULT}/assembled.json"
phase appliance-assembled
