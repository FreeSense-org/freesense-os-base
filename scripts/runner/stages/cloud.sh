# Build one provider-neutral cloud filesystem variant from the same sealed
# release inputs used by the installer ISO.
prepare_release_inputs
verify_release_channel

phase cloud-tools
cloud_keys=/root/freesense-src/src/usr/local/share/FreeSense/keys/pkg
test -s "${cloud_keys}/trusted/freesense"
if ! command -v qemu-img >/dev/null; then
  if [ "${ARCHITECTURE}" = amd64 ]; then
    mkdir -p /tmp/cloud-repos /tmp/cloud-cache
    cat >/tmp/cloud-repos/FreeSenseAssembly.conf <<EOF
FreeSenseAssembly: {
  url: "file:///root/system-repo",
  mirror_type: "none",
  enabled: yes,
  signature_type: "fingerprints",
  fingerprints: "${cloud_keys}"
}
EOF
    pkg -o REPOS_DIR=/tmp/cloud-repos -o PKG_CACHEDIR=/tmp/cloud-cache \
      update -f -r FreeSenseAssembly
    pkg -o REPOS_DIR=/tmp/cloud-repos -o PKG_CACHEDIR=/tmp/cloud-cache \
      install -y -r FreeSenseAssembly qemu-tools
  else
    pkg install -y qemu-tools || {
      echo "unable to install host qemu-tools for ARM64 cloud assembly" >&2
      exit 1
    }
  fi
fi
command -v qemu-img >/dev/null

phase cloud-root
run_in_cloud_chroot() (
  set -eu
  cloud_chroot_root=$1
  shift
  cloud_devfs_mounted=
  cleanup_cloud_chroot() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ -n "${cloud_devfs_mounted}" ] && \
      ! umount -f "${cloud_chroot_root}/dev"; then
      echo "unable to unmount cloud image devfs" >&2
      [ "${status}" -ne 0 ] || status=1
    fi
    exit "${status}"
  }
  trap cleanup_cloud_chroot EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  mount -t devfs devfs "${cloud_chroot_root}/dev"
  cloud_devfs_mounted=yes
  if [ -f /usr/local/bin/qemu-aarch64-static ] && [ ! -f "${cloud_chroot_root}/usr/local/bin/qemu-aarch64-static" ]; then
    mkdir -p "${cloud_chroot_root}/usr/local/bin"
    cp /usr/local/bin/qemu-aarch64-static "${cloud_chroot_root}/usr/local/bin/"
  fi
  chroot "${cloud_chroot_root}" "$@"
)

root=/root/cloud-root
rm -rf "${root}"
mkdir -p "${root}"
tar -xpf /root/jail-base.txz -C "${root}"
mkdir -p "${root}/tmp/assembly-repo" "${root}/tmp/assembly-repos" \
  "${root}/tmp/assembly-keys" "${root}/tmp/assembly-cache" \
  "${root}/usr/local/etc/pkg/repos" "${root}/conf" "${root}/boot/efi" \
  "${root}/dev"
cp -a /root/system-repo/. "${root}/tmp/assembly-repo/"
cp -a "${cloud_keys}/." "${root}/tmp/assembly-keys/"
pkg_package=$(find /root/system-repo/All -name 'pkg-[0-9]*.pkg' \
  -type f | sort | tail -1)
[ -n "${pkg_package}" ] || {
  echo "cloud assembly package missing: pkg-[0-9]*.pkg" >&2
  exit 1
}
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
run_in_cloud_chroot "${root}" /usr/bin/env \
  PKG_INSTALL_EPOCH="${SOURCE_DATE_EPOCH}" /bin/sh -c '
  set -eu
  pkg add /tmp/pkg-bootstrap.pkg
  pkg -o REPOS_DIR=/tmp/assembly-repos -o PKG_CACHEDIR=/tmp/assembly-cache \
    update -f -r FreeSenseAssembly
  for pkg_name in FreeSense-cloud-init qemu-guest-agent; do
    pkg -o REPOS_DIR=/tmp/assembly-repos -o PKG_CACHEDIR=/tmp/assembly-cache \
      rquery -r FreeSenseAssembly "%n" "${pkg_name}" >/dev/null || {
        echo "required cloud image package is missing: ${pkg_name}" >&2
        exit 1
      }
  done
  pkg -o REPOS_DIR=/tmp/assembly-repos \
    -o PKG_CACHEDIR=/tmp/assembly-cache install -y -r FreeSenseAssembly \
    FreeSense FreeSense-base FreeSense-kernel-FreeSense FreeSense-rc FreeSense-system \
    FreeSense-default-config-serial FreeSense-repoc FreeSense-cloud-init qemu-guest-agent
  # FreeSense-base expands its base payload during installation. Reapply the
  # signed product overlay last so its absolute /etc files win deterministically.
  pkg -o REPOS_DIR=/tmp/assembly-repos \
    -o PKG_CACHEDIR=/tmp/assembly-cache install -f -y -r FreeSenseAssembly \
    FreeSense-system
  for boot_hook in FreeSense-rc FreeSense-rc.shutdown; do
    if ! package_owner=$(pkg which "/etc/${boot_hook}"); then
      echo "cloud image package database does not own /etc/${boot_hook}" >&2
      exit 1
    fi
    case "${package_owner}" in
      *FreeSense-system-*) ;;
      *)
        echo "unexpected cloud boot hook owner: ${package_owner}" >&2
        exit 1
        ;;
    esac
  done
  package_epochs=$(pkg query -a "%t" | sort -u)
  [ "${package_epochs}" = "${PKG_INSTALL_EPOCH}" ] || {
    echo "cloud package install epoch mismatch" >&2
    exit 1
  }
'
if [ ! -s "${root}/boot/kernel/kernel" ] && \
  [ ! -s "${root}/boot/kernel/kernel.gz" ]; then
  echo "cloud image root is missing the FreeSense kernel" >&2
  exit 1
fi
for kernel_module in zfs.ko opensolaris.ko; do
  [ -s "${root}/boot/kernel/${kernel_module}" ] || {
    echo "cloud image root is missing ${kernel_module}" >&2
    exit 1
  }
done
for boot_hook in FreeSense-rc FreeSense-rc.shutdown; do
  [ -s "${root}/etc/${boot_hook}" ] || {
    echo "cloud image root is missing or empty: ${boot_hook}" >&2
    exit 1
  }
done
ln -sf FreeSense-rc "${root}/etc/pfSense-rc"
ln -sf FreeSense-rc.shutdown "${root}/etc/pfSense-rc.shutdown"
rm -rf "${root}/tmp/assembly-repo" "${root}/tmp/assembly-repos" \
  "${root}/tmp/assembly-keys" "${root}/tmp/assembly-cache" \
  "${root}/tmp/pkg-bootstrap.pkg" "${root}/usr/local/bin/qemu-aarch64-static"
config="${root}/cf/conf/config.xml"
test -s "${config}"
xml ed -L \
  -d "/freesense/system/user[name='admin']/bcrypt-hash" \
  -d "/freesense/system/user[name='admin']/password" \
  -s "/freesense/system/user[name='admin']" -t elem -n password -v '*LOCKED*' \
  "${config}"
test "$(xml sel -t -v "/freesense/system/user[name='admin']/password" "${config}")" = '*LOCKED*'
test -z "$(xml sel -t -v "/freesense/system/user[name='admin']/bcrypt-hash" "${config}")"
pw -R "${root}" lock root
cat >"${root}/usr/local/etc/pkg/repos/FreeSense.conf" <<EOF
FreeBSD: { enabled: no }
FreeBSD-kmods: { enabled: no }

FreeSense-system: {
  url: "${PUBLIC_BASE_URL}/artifacts/system/${SYSTEM_ID}/${PACKAGE_ARCH}",
  mirror_type: "none",
  enabled: yes,
  signature_type: "fingerprints",
  fingerprints: "/usr/local/share/FreeSense/keys/pkg"
}
FreeSense-packages: {
  url: "${PUBLIC_BASE_URL}/artifacts/packages/${PACKAGE_TRAIN}/${PACKAGES_ID}/${PACKAGE_ARCH}",
  mirror_type: "none",
  enabled: yes,
  signature_type: "fingerprints",
  fingerprints: "/usr/local/share/FreeSense/keys/pkg"
}
EOF

release_version=${PRODUCT_VERSION%%-*}
printf '%s\n' "${PRODUCT_VERSION}" >"${root}/etc/version"
install -m 0444 /tmp/channel-payload.json \
  "${root}/usr/local/etc/freesense-channel.json"
install -m 0444 /tmp/channel-signature.bin \
  "${root}/usr/local/etc/freesense-channel.sig"
cat >>"${root}/etc/rc.conf" <<'EOF'
cloudinit_enable="YES"
growfs_enable="YES"
growfs_swap_size="0"
qemu_guest_agent_enable="YES"
sshd_enable="YES"
EOF
# Cloud images must expose the loader and kernel on COM1 from the first boot.
# -Dh keeps serial primary while retaining the firmware-native video console
# (vidconsole under BIOS, efi under UEFI), matching the boot-tested ISO setup.
cat >"${root}/boot.config" <<'EOF'
-S115200 -Dh
EOF
cat >>"${root}/boot/loader.conf" <<'EOF'
autoboot_delay="3"
kern.cam.boot_delay=10000
boot_multicons="YES"
boot_serial="YES"
comconsole_speed="115200"
EOF
if [ "${CLOUD_FILESYSTEM}" = zfs ]; then
  printf '%s\n' 'zfs_enable="YES"' >>"${root}/etc/rc.conf"
  cat >>"${root}/boot/loader.conf" <<'EOF'
zfs_load="YES"
kern.geom.label.disk_ident.enable="0"
kern.geom.label.gptid.enable="0"
EOF
fi
# The native FreeSense boot path consumes this one-shot marker, invokes the
# pinned FreeBSD growfs service before normal configuration, and removes it.
touch "${root}/root/force_growfs"
touch "${root}/firstboot"

# Publication images never carry build identity.
rm -rf "${root}/var/lib/cloud" "${root}/var/db/cloud-init" \
  "${root}/var/db/entropy" \
  "${root}/var/db/dhclient.leases"* "${root}/var/db/dhclient/"* \
  "${root}/var/log/"*
rm -f "${root}/etc/hostid" "${root}/etc/machine-id" \
  "${root}/var/db/hostid" \
  "${root}/etc/ssh/ssh_host_"* \
  "${root}/var/db/freesense-cloud-init/instance.json"
: >"${root}/var/log/messages"

phase cloud-disk
raw=/root/FreeSense.raw
qcow=/root/FreeSense.qcow2
truncate -s "${CLOUD_VIRTUAL_SIZE_GIB}G" "${raw}"
md=$(mdconfig -a -t vnode -f "${raw}")
cloud_pool=
cleanup_cloud_disk() {
  umount /mnt/cloud-efi 2>/dev/null || true
  if [ -n "${cloud_pool}" ]; then
    zpool export "${cloud_pool}" 2>/dev/null || true
  fi
  umount /mnt/cloud-root 2>/dev/null || true
  mdconfig -d -u "${md#md}" 2>/dev/null || true
}
trap cleanup_cloud_disk EXIT INT TERM
gpart create -s gpt "${md}"
mkdir -p /mnt/cloud-root /mnt/cloud-efi

if [ "${CLOUD_FILESYSTEM}" = ufs ]; then
  if [ "${ARCHITECTURE}" = amd64 ]; then
    gpart add -a 4k -s 512k -t freebsd-boot -l freesense-boot "${md}"
  fi
  gpart add -a 1m -s 200m -t efi -l freesense-efi "${md}"
  gpart add -a 1m -t freebsd-ufs -l freesense-root "${md}"
  if [ "${ARCHITECTURE}" = amd64 ]; then
    gpart bootcode -b "${root}/boot/pmbr" -p "${root}/boot/gptboot" -i 1 "${md}"
  fi
  newfs -U -L freesense-root "/dev/gpt/freesense-root"
  mount "/dev/gpt/freesense-root" /mnt/cloud-root
  cat >"${root}/etc/fstab" <<'EOF'
/dev/gpt/freesense-root / ufs rw,noatime 1 1
/dev/gpt/freesense-efi /boot/efi msdosfs rw 2 2
EOF
else
  kldload zfs 2>/dev/null || kldstat -q -m zfs
  gpart add -a 4k -s 260m -t efi -l freesense-efi "${md}"
  if [ "${ARCHITECTURE}" = amd64 ]; then
    gpart add -a 4k -s 512k -t freebsd-boot -l freesense-boot "${md}"
  fi
  gpart add -a 1m -t freebsd-zfs -l freesense-zfs "${md}"
  if [ "${ARCHITECTURE}" = amd64 ]; then
    gpart bootcode -b "${root}/boot/pmbr" -p "${root}/boot/gptzfsboot" -i 2 "${md}"
  fi
  zpool create -o altroot=/mnt/cloud-root -o ashift=12 -o autoexpand=on \
    -O compression=on -O atime=off -m none -f FreeSense \
    /dev/gpt/freesense-zfs
  cloud_pool=FreeSense
  zfs create -o mountpoint=none FreeSense/ROOT
  zfs create -o mountpoint=/ FreeSense/ROOT/default
  zfs create -o mountpoint=/cf -o setuid=off -o exec=off FreeSense/ROOT/default/cf
  zfs create -o mountpoint=/tmp -o exec=on -o setuid=off FreeSense/tmp
  zfs create -o mountpoint=/home FreeSense/home
  zfs create -o mountpoint=/var FreeSense/var
  zfs create -o mountpoint=/var/cache -o setuid=off -o exec=off \
    -o compression=off FreeSense/var/cache
  zfs create -o mountpoint=/var/db -o setuid=off -o exec=off FreeSense/var/db
  zfs create -o mountpoint=/var/empty FreeSense/var/empty
  zfs create -o mountpoint=/var/log -o setuid=off -o exec=off FreeSense/var/log
  zfs create -o mountpoint=/var/tmp -o setuid=off FreeSense/var/tmp
  zfs create -o mountpoint=/var/cache/pkg -o setuid=off -o exec=off \
    FreeSense/ROOT/default/var_cache_pkg
  zfs create -o mountpoint=/var/db/pkg -o setuid=off -o exec=off \
    FreeSense/ROOT/default/var_db_pkg
  chmod 1777 /mnt/cloud-root/tmp /mnt/cloud-root/var/tmp
  cat >"${root}/etc/fstab" <<'EOF'
/dev/gpt/freesense-efi /boot/efi msdosfs rw 2 2
EOF
fi

newfs_msdos -F 32 -c 1 -L FREESENSE "/dev/gpt/freesense-efi"
mount -t msdosfs "/dev/gpt/freesense-efi" /mnt/cloud-efi
(cd "${root}" && tar -cf - .) | (cd /mnt/cloud-root && tar -xpf -)
mkdir -p /mnt/cloud-efi/EFI/BOOT
efi_boot=BOOTX64.EFI
[ "${ARCHITECTURE}" = arm64 ] && efi_boot=BOOTAA64.EFI
install -m 0444 "${root}/boot/loader.efi" "/mnt/cloud-efi/EFI/BOOT/${efi_boot}"
if [ "${CLOUD_FILESYSTEM}" = zfs ]; then
  zpool set bootfs=FreeSense/ROOT/default FreeSense
  mkdir -p /mnt/cloud-root/boot/zfs
  zpool set cachefile=/mnt/cloud-root/boot/zfs/zpool.cache FreeSense
  zfs set canmount=noauto FreeSense/ROOT/default
fi
sync
cleanup_cloud_disk
trap - EXIT INT TERM

phase cloud-convert
qemu-img convert -f raw -O qcow2 -o compat=1.1,lazy_refcounts=on \
  "${raw}" "${qcow}"
raw_name="FreeSense-${release_version}-${ARCHITECTURE}-${CLOUD_FILESYSTEM}.raw.xz"
qcow_name="FreeSense-${release_version}-${ARCHITECTURE}-${CLOUD_FILESYSTEM}.qcow2.xz"
if [ "${CHANNEL}" != stable ]; then
  raw_name="FreeSense-${release_version}-g${GENERATION}-${ARCHITECTURE}-${CLOUD_FILESYSTEM}.raw.xz"
  qcow_name="FreeSense-${release_version}-g${GENERATION}-${ARCHITECTURE}-${CLOUD_FILESYSTEM}.qcow2.xz"
fi
xz -T0 -9 -c "${raw}" >/root/"${raw_name}"
xz -T0 -9 -c "${qcow}" >/root/"${qcow_name}"
raw_sha=$(sha256 -q /root/"${raw_name}")
qcow_sha=$(sha256 -q /root/"${qcow_name}")
raw_size=$(stat -f %z /root/"${raw_name}")
qcow_size=$(stat -f %z /root/"${qcow_name}")
virtual_size=$((CLOUD_VIRTUAL_SIZE_GIB * 1024 * 1024 * 1024))

phase cloud-publish
upload_immutable /root/"${raw_name}" "${RESULT}/${raw_name}"
upload_immutable /root/"${qcow_name}" "${RESULT}/${qcow_name}"
jq -n \
  --arg fingerprint "${FINGERPRINT}" --arg bundle "${BUNDLE_ID}" \
  --arg channel "${CHANNEL}" --arg release "${release_version}" \
  --arg system "${SYSTEM_ID}" --arg packages "${PACKAGES_ID}" \
  --arg channel_payload "${CHANNEL_PAYLOAD_SHA256}" \
  --arg package_train "${PACKAGE_TRAIN}" --arg platform "${PLATFORM_ID}" \
  --arg source "${SOURCE_SHA}" --arg system_ports "${SYSTEM_SHA}" \
  --arg freebsd "${FREEBSD_SHA}" --arg ports "${PORTS_SHA}" \
  --arg worker_tools "${WORKER_TOOLS_SHA256}" \
  --arg filesystem "${CLOUD_FILESYSTEM}" \
  --arg architecture "${ARCHITECTURE}" --arg package_arch "${PACKAGE_ARCH}" \
  --arg image_profile "${IMAGE_PROFILE}" --arg firmware "${FIRMWARE}" \
  --argjson capabilities "${IMAGE_CAPABILITIES}" \
  --arg raw_file "${raw_name}" --arg raw_sha "${raw_sha}" \
  --arg qcow_file "${qcow_name}" --arg qcow_sha "${qcow_sha}" \
  --argjson generation "${GENERATION}" --argjson virtual_size "${virtual_size}" \
  --argjson raw_size "${raw_size}" --argjson qcow_size "${qcow_size}" \
  '{schema_version:"freesense.cloud-image/v1",fingerprint:$fingerprint,
    bundle_fingerprint:$bundle,generation:$generation,channel:$channel,
    release_version:$release,architecture:$architecture,package_arch:$package_arch,
    platform:$image_profile,firmware:($firmware|split(",")),capabilities:$capabilities,
    filesystem:$filesystem,
    disk:({scheme:"gpt",firmware:($firmware|split(",")),virtual_size:$virtual_size,
      root_growth:true} +
      (if $filesystem == "zfs" then
        {pool:{name:"FreeSense",topology:"stripe",
          root_dataset:"FreeSense/ROOT/default",boot_environments:true}}
       else {} end)),
    inputs:{platform:$platform,system:$system,packages:$packages,
      package_train:$package_train,channel_payload:$channel_payload,source:$source,
      system_ports:$system_ports,freebsd:$freebsd,ports:$ports,
      worker_tools:$worker_tools},
    files:[
      {kind:"cloud",format:"qcow2",compression:"xz",file:$qcow_file,
       sha256:$qcow_sha,size:$qcow_size,virtual_size:$virtual_size},
      {kind:"cloud",format:"raw",compression:"xz",file:$raw_file,
       sha256:$raw_sha,size:$raw_size,virtual_size:$virtual_size}
    ]}' >/tmp/assembled.json
upload_immutable /tmp/assembled.json "${RESULT}/assembled.json"
phase cloud-complete
