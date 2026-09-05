#!/usr/bin/env bash
# Disposable GitHub-hosted FreeBSD assembly VM. No production credentials.
set -euo pipefail
: "${RUNNER_TEMP:?}"
input=$1
output=$2
mkdir -p "$output"
work=$(mktemp -d "${RUNNER_TEMP}/github-iso.XXXXXX")
vm_pid=""
cleanup() {
  status=$?
  trap - EXIT
  if [[ -n $vm_pid ]] && kill -0 "$vm_pid" 2>/dev/null; then
    kill "$vm_pid"
    wait "$vm_pid" || true
  fi
  cp "$work/serial.log" "$output/assembly-serial.log" 2>/dev/null || true
  cp "$work/qemu.log" "$output/assembly-qemu.log" 2>/dev/null || true
  rm -rf -- "$work"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
[[ -r /dev/kvm && -w /dev/kvm ]]
(( $(nproc) >= 4 ))
(( $(awk '/MemAvailable:/ {print $2}' /proc/meminfo) >= 12 * 1024 * 1024 ))
df -h "$work" | tee "$output/disk-before.txt"
# This is a measured experiment: fail explicitly rather than overcommit host RAM.
# Disk is sparse and actual peak use is recorded by the worker at each phase.
image_sha=$(jq -er .artifact_image_sha256 "$input/channel.json")
[[ $image_sha =~ ^[0-9a-f]{64}$ ]]
curl --fail --location --retry 5 --output "$work/base.qcow2" \
  "https://pkg.freesense.org/v1/inputs/sha256/${image_sha}"
printf '%s  %s\n' "$image_sha" "$work/base.qcow2" | sha256sum --check
qemu-img create -f qcow2 -F qcow2 -b "$work/base.qcow2" "$work/worker.qcow2"
qemu-img resize "$work/worker.qcow2" 64G
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
public_key=$(cat "$work/key.pub")
cat >"$work/user-data" <<EOF
#!/bin/sh
set -eu
mkdir -p /root/.ssh
chmod 700 /root/.ssh
printf '%s\n' '${public_key}' >/root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
sed -i '' -E 's/^[#[:space:]]*PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
printf '\nPermitRootLogin prohibit-password\nPasswordAuthentication no\n' >>/etc/ssh/sshd_config
sysrc sshd_enable=YES
service sshd restart
EOF
printf 'instance-id: github-iso-%s\nlocal-hostname: github-iso\n' "${GITHUB_RUN_ID:-local}" >"$work/meta-data"
cloud-localds "$work/seed.img" "$work/user-data" "$work/meta-data"
cp /usr/share/OVMF/OVMF_VARS_4M.fd "$work/vars.fd"
qemu-system-x86_64 -name github-iso-experiment -machine q35,accel=kvm -cpu host \
  -smp 4 -m 10240 -display none -monitor none -serial "file:$work/serial.log" \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive "if=pflash,format=raw,file=$work/vars.fd" \
  -drive "if=virtio,format=qcow2,cache=none,discard=unmap,file=$work/worker.qcow2" \
  -drive "if=ide,media=cdrom,format=raw,readonly=on,file=$work/seed.img" \
  -device virtio-net-pci,netdev=net0 \
  -netdev user,id=net0,ipv6=off,hostfwd=tcp:127.0.0.1:2222-:22 \
  -no-reboot >"$work/qemu.log" 2>&1 &
vm_pid=$!
ssh_args=(-i "$work/key" -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$work/known_hosts"
  -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
ready=false
for attempt in {1..180}; do
  kill -0 "$vm_pid"
  if ssh "${ssh_args[@]}" -p 2222 root@127.0.0.1 true 2>/dev/null; then ready=true; break; fi
  if (( attempt % 12 == 0 )); then
    serial_bytes=$(stat -c %s "$work/serial.log" 2>/dev/null || echo 0)
    serial_phase=$(tr '\r' '\n' <"$work/serial.log" 2>/dev/null | tail -n 1 || true)
    printf 'Waiting for FreeBSD SSH: elapsed=%ss serial_bytes=%s last_line=%s\n' \
      "$((attempt * 5))" "$serial_bytes" "$serial_phase"
  fi
  sleep 5
done
[[ $ready == true ]] || { echo 'FreeBSD SSH startup timed out after 15 minutes' >&2; exit 1; }
scp "${ssh_args[@]}" -P 2222 "$input/worker.sh" root@127.0.0.1:/root/worker.sh
timeout --signal=TERM --kill-after=30s 18000 \
  ssh "${ssh_args[@]}" -p 2222 root@127.0.0.1 'sh /root/worker.sh' \
  2>&1 | tee "$output/assembly.log"
scp "${ssh_args[@]}" -P 2222 'root@127.0.0.1:/root/experiment-output/*' "$output/"
df -h "$work" | tee "$output/disk-after.txt"
du -h "$work/worker.qcow2" | tee "$output/overlay-size.txt"
ssh "${ssh_args[@]}" -p 2222 root@127.0.0.1 'shutdown -p now' || true
