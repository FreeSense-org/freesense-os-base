#!/usr/bin/env bash
set -euo pipefail

declare public_base_url= fingerprint= bundle= system= packages= generation= channel=
while (($#)); do
  case "$1" in
    --public-base-url) public_base_url=$2; shift 2 ;;
    --fingerprint) fingerprint=$2; shift 2 ;;
    --bundle) bundle=$2; shift 2 ;;
    --system) system=$2; shift 2 ;;
    --packages) packages=$2; shift 2 ;;
    --generation) generation=$2; shift 2 ;;
    --channel) channel=$2; shift 2 ;;
    *) echo "unknown cloud smoke argument: $1" >&2; exit 2 ;;
  esac
done
for value in "$fingerprint" "$bundle" "$system" "$packages"; do
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid cloud smoke identity" >&2; exit 2; }
done

work=$(mktemp -d)
cleanup() {
  [[ -z "${qemu_pid:-}" ]] || kill "$qemu_pid" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
base="${public_base_url}/artifacts/cloud/${fingerprint}"
curl --fail --silent --show-error --location --retry 5 \
  "${base}/complete.json" -o "${work}/complete.json"
jq -e --arg fingerprint "$fingerprint" --arg bundle "$bundle" \
  --arg system "$system" --arg packages "$packages" \
  --arg channel "$channel" --argjson generation "$generation" '
  .schema_version == "freesense.cloud-image/v1" and
  .fingerprint == $fingerprint and .bundle_fingerprint == $bundle and
  .generation == $generation and .channel == $channel and
  .inputs.system == $system and .inputs.packages == $packages and
  .disk.scheme == "gpt" and .disk.root_growth == true and
  (.disk.firmware | sort) == ["bios","uefi"] and
  ([.files[].format] | sort) == ["qcow2","raw"]
' "${work}/complete.json" >/dev/null

for format in qcow2 raw; do
  file=$(jq -er --arg format "$format" '.files[] | select(.format == $format) | .file' "${work}/complete.json")
  sha=$(jq -er --arg format "$format" '.files[] | select(.format == $format) | .sha256' "${work}/complete.json")
  curl --fail --silent --show-error --location --retry 5 "${base}/${file}" -o "${work}/${file}"
  printf '%s  %s\n' "$sha" "${work}/${file}" | sha256sum --check --status
  xz -dc "${work}/${file}" >"${work}/disk.${format}"
done

ssh-keygen -q -t ed25519 -N '' -f "${work}/id"
key=$(<"${work}/id.pub")
cat >"${work}/meta-data" <<EOF
instance-id: freesense-smoke-${generation}
local-hostname: freesense-cloud-smoke
EOF
cat >"${work}/user-data" <<EOF
#cloud-config
ssh_authorized_keys:
  - ${key}
freesense:
  management_cidrs:
    - 10.0.2.2/32
EOF
cat >"${work}/network-config" <<'EOF'
version: 2
ethernets:
  wan:
    match:
      macaddress: "52:54:00:12:34:56"
    dhcp4: true
EOF
cloud-localds --network-config="${work}/network-config" \
  "${work}/cidata.iso" "${work}/user-data" "${work}/meta-data"
qemu-img resize "${work}/disk.qcow2" 20G

boot_and_wait() {
  local disk=$1 format=$2 firmware=$3 log=$4
  local firmware_args=()
  if [[ "$firmware" == uefi ]]; then
    ovmf=$(find /usr/share /usr/share/OVMF -type f \( -name 'OVMF_CODE*.fd' -o -name 'OVMF_CODE*.4m.fd' \) 2>/dev/null | head -n1)
    [[ -n "$ovmf" ]]
    firmware_args=(-drive "if=pflash,format=raw,readonly=on,file=${ovmf}")
  fi
  qemu-system-x86_64 -machine accel=kvm -m 4096 -nographic \
    "${firmware_args[@]}" \
    -drive "if=virtio,format=${format},file=${disk},cache=none" \
    -drive "if=virtio,format=raw,readonly=on,file=${work}/cidata.iso" \
    -netdev user,id=wan,hostfwd=tcp:127.0.0.1:10022-:22,hostfwd=tcp:127.0.0.1:10443-:443 \
    -device virtio-net-pci,netdev=wan,mac=52:54:00:12:34:56 \
    -serial "file:${log}" -monitor none &
  qemu_pid=$!
  for _ in {1..120}; do
    if ssh -q -o BatchMode=yes -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o ConnectTimeout=2 \
      -i "${work}/id" -p 10022 admin@127.0.0.1 true; then
      return 0
    fi
    kill -0 "$qemu_pid" 2>/dev/null || { cat "$log" >&2; return 1; }
    sleep 5
  done
  cat "$log" >&2
  return 1
}

boot_and_wait "${work}/disk.qcow2" qcow2 bios "${work}/bios.log"
ssh_args=(-q -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "${work}/id" -p 10022 admin@127.0.0.1)
"${ssh_args[@]}" 'test "$(sysrc -n qemu_guest_agent_enable)" = YES &&
  service qemu_guest_agent status &&
  test -s /etc/ssh/ssh_host_ed25519_key &&
  grep -q freesense-cloud-smoke /conf/config.xml &&
  test "$(df -k / | awk "NR == 2 {print \$2}")" -gt 17000000 &&
  test "$(cloud-init status --wait)" = "status: done"'
if ssh -q -o BatchMode=yes -o PreferredAuthentications=password \
  -o PubkeyAuthentication=no -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 \
  -p 10022 admin@127.0.0.1 true; then
  echo "SSH password authentication was accepted" >&2
  exit 1
fi
if curl --insecure --fail --silent --connect-timeout 3 https://127.0.0.1:10443/ >/dev/null 2>&1; then
  echo "WebUI was exposed automatically on one-NIC WAN" >&2
  exit 1
fi
kill "$qemu_pid"
wait "$qemu_pid" || true
unset qemu_pid

# A clean second boot of the same disk proves instance-ID idempotency.
boot_and_wait "${work}/disk.qcow2" qcow2 bios "${work}/bios-second.log"
"${ssh_args[@]}" 'test "$(grep -c "<instance_id>freesense-smoke-" /conf/config.xml)" = 1 &&
  test "$(grep -c "FreeSense cloud temporary SSH" /conf/config.xml)" = 1'
kill "$qemu_pid"
wait "$qemu_pid" || true
unset qemu_pid

# Exercise two-NIC role assignment on the independently published raw image
# under UEFI. Management is forwarded only through the LAN NIC.
cat >"${work}/meta-data-two" <<EOF
instance-id: freesense-smoke-two-${generation}
local-hostname: freesense-cloud-two
EOF
cat >"${work}/user-data-two" <<EOF
#cloud-config
ssh_authorized_keys:
  - ${key}
freesense:
  interfaces:
    - match: "52:54:00:12:34:56"
      role: wan
    - match: "52:54:00:12:34:57"
      role: lan
EOF
cat >"${work}/network-config-two" <<'EOF'
version: 2
ethernets:
  wan:
    match:
      macaddress: "52:54:00:12:34:56"
    dhcp4: true
    routes:
      - to: 0.0.0.0/0
        via: 10.0.2.2
  lan:
    match:
      macaddress: "52:54:00:12:34:57"
    dhcp4: true
EOF
cloud-localds --network-config="${work}/network-config-two" \
  "${work}/cidata-two.iso" "${work}/user-data-two" "${work}/meta-data-two"
ovmf=$(find /usr/share /usr/share/OVMF -type f \( -name 'OVMF_CODE*.fd' -o -name 'OVMF_CODE*.4m.fd' \) 2>/dev/null | head -n1)
[[ -n "$ovmf" ]]
qemu-system-x86_64 -machine accel=kvm -m 4096 -nographic \
  -drive "if=pflash,format=raw,readonly=on,file=${ovmf}" \
  -drive "if=virtio,format=raw,file=${work}/disk.raw,cache=none" \
  -drive "if=virtio,format=raw,readonly=on,file=${work}/cidata-two.iso" \
  -netdev user,id=wan -device virtio-net-pci,netdev=wan,mac=52:54:00:12:34:56 \
  -netdev user,id=lan,net=10.0.3.0/24,dhcpstart=10.0.3.15,hostfwd=tcp:127.0.0.1:10023-:22 \
  -device virtio-net-pci,netdev=lan,mac=52:54:00:12:34:57 \
  -serial "file:${work}/uefi-two.log" -monitor none &
qemu_pid=$!
for _ in {1..120}; do
  if ssh -q -o BatchMode=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=2 \
    -i "${work}/id" -p 10023 admin@127.0.0.1 \
    'grep -q "<wan>" /conf/config.xml &&
     grep -q "<lan>" /conf/config.xml &&
     ! grep -q "FreeSense cloud temporary SSH" /conf/config.xml'; then
    two_nic_ready=true
    break
  fi
  kill -0 "$qemu_pid" 2>/dev/null || { cat "${work}/uefi-two.log" >&2; exit 1; }
  sleep 5
done
[[ "${two_nic_ready:-false}" == true ]] || {
  cat "${work}/uefi-two.log" >&2
  exit 1
}
kill "$qemu_pid"
wait "$qemu_pid" || true
unset qemu_pid
