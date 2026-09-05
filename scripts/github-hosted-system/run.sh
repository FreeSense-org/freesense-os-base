#!/usr/bin/env bash
set -euo pipefail
: "${RUNNER_TEMP:?}"
input=$1
output=$2
mkdir -p "$output"
work=$(mktemp -d "${RUNNER_TEMP}/github-system.XXXXXX")
vm_pid=""
cleanup() {
  status=$?
  trap - EXIT
  if [[ -n $vm_pid ]] && kill -0 "$vm_pid" 2>/dev/null; then kill "$vm_pid"; wait "$vm_pid" || true; fi
  cp "$work/serial.log" "$output/system-serial.log" 2>/dev/null || true
  cp "$work/qemu.log" "$output/system-qemu.log" 2>/dev/null || true
  rm -rf -- "$work"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
[[ -r /dev/kvm && -w /dev/kvm ]]
(( $(nproc) >= 4 ))
df -h "$work" | tee "$output/disk-before.txt"
image_sha=$(jq -er .artifact_image_sha256 "$input/channel.json")
curl --fail --location --retry 5 --output "$work/base.qcow2" \
  "https://pkg.freesense.org/v1/inputs/sha256/${image_sha}"
printf '%s  %s\n' "$image_sha" "$work/base.qcow2" | sha256sum --check
qemu-img create -f qcow2 -F qcow2 -b "$work/base.qcow2" "$work/worker.qcow2"
qemu-img resize "$work/worker.qcow2" 64G
truncate -s 8G "$work/output.img"
mkfs.vfat -F 32 -n FSOUTPUT "$work/output.img"
worker_b64=$(base64 -w 0 "$input/worker.sh")
cat >"$work/user-data" <<EOF
#!/bin/sh
set -eu
exec </dev/ttyu0 >/dev/ttyu0 2>&1
echo FREESENSE_SYSTEM_EXPERIMENT_BEGIN
printf '%s' '${worker_b64}' | /usr/bin/base64 -d >/root/worker.sh
chmod 700 /root/worker.sh
status=0
/bin/sh /root/worker.sh || status=\$?
mkdir -p /mnt/output
mount -t msdosfs /dev/vtbd1 /mnt/output || status=90
if [ \$status -eq 0 ]; then cp /root/experiment-output/* /mnt/output/ || status=91; fi
printf '%s\n' "\$status" >/mnt/output/status 2>/dev/null || true
sync
umount /mnt/output 2>/dev/null || true
echo "FREESENSE_SYSTEM_EXPERIMENT_END status=\$status"
shutdown -p now
exit "\$status"
EOF
printf 'instance-id: github-system-%s\nlocal-hostname: github-system\n' "${GITHUB_RUN_ID:-local}" >"$work/meta-data"
cloud-localds "$work/seed.img" "$work/user-data" "$work/meta-data"
cp /usr/share/OVMF/OVMF_VARS_4M.fd "$work/vars.fd"
touch "$work/serial.log"
qemu-system-x86_64 -name github-system-experiment -machine q35,accel=kvm -cpu host \
  -smp 4 -m 10240 -display none -monitor none -serial "file:$work/serial.log" \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive "if=pflash,format=raw,file=$work/vars.fd" \
  -drive "if=virtio,format=qcow2,cache=none,discard=unmap,file=$work/worker.qcow2" \
  -drive "if=virtio,format=raw,cache=none,file=$work/output.img" \
  -drive "if=ide,media=cdrom,format=raw,readonly=on,file=$work/seed.img" \
  -device virtio-net-pci,netdev=net0 -netdev user,id=net0,ipv6=off \
  -no-reboot >"$work/qemu.log" 2>&1 &
vm_pid=$!
start=$(date +%s)
next_report=$start
while kill -0 "$vm_pid" 2>/dev/null; do
  now=$(date +%s)
  (( now - start < 19800 )) || { echo 'System core experiment exceeded 5.5 hours' >&2; exit 1; }
  if (( now >= next_report )); then
    phase=$(tr '\r' '\n' <"$work/serial.log" | grep -E '^(FreeSense phase|FREESENSE_)' | tail -n 1 || true)
    printf 'System core heartbeat: elapsed=%ss overlay=%s phase=%s\n' "$((now - start))" \
      "$(du -h "$work/worker.qcow2" | cut -f1)" "${phase:-booting}"
    next_report=$((now + 180))
  fi
  sleep 10
done
wait "$vm_pid"
vm_pid=""
mcopy -i "$work/output.img" '::/*' "$output/"
[[ $(tr -d '\r\n' <"$output/status") == 0 ]] || { echo 'System core worker failed' >&2; exit 1; }
tr '\r' '\n' <"$work/serial.log" >"$output/system.log"
df -h "$work" | tee "$output/disk-after.txt"
du -h "$work/worker.qcow2" | tee "$output/overlay-size.txt"
