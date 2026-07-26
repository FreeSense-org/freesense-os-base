# Build a provider-neutral, dual BIOS/UEFI UFS disk from the same sealed
# release inputs used by the installer ISO.
prepare_release_inputs
verify_release_channel

phase cloud-tools
cat >/usr/local/etc/pkg/repos/FreeSense-cloud-build.conf <<EOF
FreeSense-cloud-build: {
  url: "file:///root/system-repo",
  enabled: yes,
  signature_type: "pubkey",
  pubkey: "/root/sign/repo.pub"
}
EOF
pkg update -f -r FreeSense-cloud-build
pkg install -y -r FreeSense-cloud-build qemu-tools
command -v qemu-img >/dev/null

phase cloud-root
root=/root/cloud-root
rm -rf "${root}"
mkdir -p "${root}"
tar -xpf /root/jail-base.txz -C "${root}"
mkdir -p "${root}/usr/local/etc/pkg/repos" "${root}/conf" "${root}/boot/efi"
cat >"${root}/usr/local/etc/pkg/repos/FreeSense.conf" <<EOF
FreeSense: {
  url: "file:///root/system-repo",
  enabled: yes,
  signature_type: "pubkey",
  pubkey: "/usr/local/etc/pkg/repos/FreeSense.pub"
}
EOF
install -m 0444 /root/sign/repo.pub \
  "${root}/usr/local/etc/pkg/repos/FreeSense.pub"
mkdir -p "${root}/root/system-repo"
cp -a /root/system-repo/. "${root}/root/system-repo/"
pkg -r "${root}" update -f
pkg -r "${root}" install -y FreeSense FreeSense-default-config-serial \
  FreeSense-cloud-init qemu-guest-agent
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
rm -rf "${root}/root/system-repo"
cat >"${root}/usr/local/etc/pkg/repos/FreeSense.conf" <<EOF
FreeSense-system: {
  url: "${PUBLIC_BASE_URL}/artifacts/system/${SYSTEM_ID}/amd64",
  enabled: yes,
  signature_type: "pubkey",
  pubkey: "/usr/local/etc/pkg/repos/FreeSense.pub"
}
FreeSense-packages: {
  url: "${PUBLIC_BASE_URL}/artifacts/packages/${PACKAGE_TRAIN}/${PACKAGES_ID}/amd64",
  enabled: yes,
  signature_type: "pubkey",
  pubkey: "/usr/local/etc/pkg/repos/FreeSense.pub"
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
qemu_guest_agent_enable="YES"
sshd_enable="YES"
EOF
cat >"${root}/usr/local/etc/rc.d/freesense_growroot" <<'EOF'
#!/bin/sh
# PROVIDE: freesense_growroot
# REQUIRE: root
# BEFORE: NETWORKING
# KEYWORD: firstboot

. /etc/rc.subr
name=freesense_growroot
rcvar=freesense_growroot_enable
start_cmd=freesense_growroot_start

freesense_growroot_start()
{
	for disk in $(sysctl -n kern.disks); do
		index=$(gpart show -lp "${disk}" 2>/dev/null |
			awk '$4 == "freesense-root" { print $3; exit }')
		[ -n "${index}" ] || continue
		gpart recover "${disk}" >/dev/null 2>&1 || true
		gpart resize -i "${index}" "${disk}"
		growfs -y /
		return 0
	done
	echo "FreeSense cloud root partition was not found" >&2
	return 1
}

load_rc_config "${name}"
: "${freesense_growroot_enable:=YES}"
run_rc_command "$1"
EOF
chmod 0555 "${root}/usr/local/etc/rc.d/freesense_growroot"
touch "${root}/firstboot"
cat >"${root}/etc/fstab" <<'EOF'
/dev/gpt/freesense-root / ufs rw,noatime 1 1
/dev/gpt/freesense-efi /boot/efi msdosfs rw 2 2
EOF

# Publication images never carry build identity.
rm -rf "${root}/var/lib/cloud" "${root}/var/db/cloud-init" \
  "${root}/var/db/dhclient.leases"* "${root}/var/db/dhclient/"* \
  "${root}/var/log/"*
rm -f "${root}/etc/hostid" "${root}/etc/machine-id" \
  "${root}/var/db/hostid" "${root}/var/db/entropy" \
  "${root}/etc/ssh/ssh_host_"* \
  "${root}/var/db/freesense-cloud-init/instance.json"
: >"${root}/var/log/messages"

phase cloud-disk
raw=/root/FreeSense.raw
qcow=/root/FreeSense.qcow2
truncate -s 16G "${raw}"
md=$(mdconfig -a -t vnode -f "${raw}")
cleanup_cloud_disk() {
  umount /mnt/cloud-efi 2>/dev/null || true
  umount /mnt/cloud-root 2>/dev/null || true
  mdconfig -d -u "${md#md}" 2>/dev/null || true
}
trap cleanup_cloud_disk EXIT INT TERM
gpart create -s gpt "${md}"
gpart add -a 4k -s 512k -t freebsd-boot -l freesense-boot "${md}"
gpart add -a 1m -s 200m -t efi -l freesense-efi "${md}"
gpart add -a 1m -t freebsd-ufs -l freesense-root "${md}"
gpart bootcode -b "${root}/boot/pmbr" -p "${root}/boot/gptboot" -i 1 "${md}"
newfs -U -L freesense-root "/dev/gpt/freesense-root"
newfs_msdos -F 32 -L FREESENSE_EFI "/dev/gpt/freesense-efi"
mkdir -p /mnt/cloud-root /mnt/cloud-efi
mount "/dev/gpt/freesense-root" /mnt/cloud-root
mount -t msdosfs "/dev/gpt/freesense-efi" /mnt/cloud-efi
(cd "${root}" && tar -cf - .) | (cd /mnt/cloud-root && tar -xpf -)
mkdir -p /mnt/cloud-efi/EFI/BOOT
install -m 0444 "${root}/boot/loader.efi" /mnt/cloud-efi/EFI/BOOT/BOOTX64.EFI
sync
cleanup_cloud_disk
trap - EXIT INT TERM

phase cloud-convert
qemu-img convert -f raw -O qcow2 -o compat=1.1,lazy_refcounts=on \
  "${raw}" "${qcow}"
raw_name="FreeSense-${release_version}-amd64-ufs.raw.xz"
qcow_name="FreeSense-${release_version}-amd64-ufs.qcow2.xz"
if [ "${CHANNEL}" != stable ]; then
  raw_name="FreeSense-${release_version}-g${GENERATION}-amd64-ufs.raw.xz"
  qcow_name="FreeSense-${release_version}-g${GENERATION}-amd64-ufs.qcow2.xz"
fi
xz -T0 -9 -c "${raw}" >/root/"${raw_name}"
xz -T0 -9 -c "${qcow}" >/root/"${qcow_name}"
raw_sha=$(sha256 -q /root/"${raw_name}")
qcow_sha=$(sha256 -q /root/"${qcow_name}")
raw_size=$(stat -f %z /root/"${raw_name}")
qcow_size=$(stat -f %z /root/"${qcow_name}")
virtual_size=$((16 * 1024 * 1024 * 1024))

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
  --arg raw_file "${raw_name}" --arg raw_sha "${raw_sha}" \
  --arg qcow_file "${qcow_name}" --arg qcow_sha "${qcow_sha}" \
  --argjson generation "${GENERATION}" --argjson virtual_size "${virtual_size}" \
  --argjson raw_size "${raw_size}" --argjson qcow_size "${qcow_size}" \
  '{schema_version:"freesense.cloud-image/v1",fingerprint:$fingerprint,
    bundle_fingerprint:$bundle,generation:$generation,channel:$channel,
    release_version:$release,architecture:"amd64",filesystem:"ufs",
    disk:{scheme:"gpt",firmware:["bios","uefi"],virtual_size:$virtual_size,
      root_growth:true},
    inputs:{platform:$platform,system:$system,packages:$packages,
      package_train:$package_train,channel_payload:$channel_payload,source:$source,
      system_ports:$system_ports,freebsd:$freebsd,ports:$ports,
      worker_tools:$worker_tools},
    files:[
      {kind:"cloud",format:"qcow2",compression:"xz",file:$qcow_file,
       sha256:$qcow_sha,size:$qcow_size,virtual_size:$virtual_size},
      {kind:"cloud",format:"raw",compression:"xz",file:$raw_file,
       sha256:$raw_sha,size:$raw_size,virtual_size:$virtual_size}
    ]}' >/tmp/complete.json
upload_immutable /tmp/complete.json "${RESULT}/complete.json"
phase cloud-complete
