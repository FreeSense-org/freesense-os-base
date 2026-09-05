#!/usr/bin/env bash
set -euo pipefail
output=$1
filename=$(jq -er .file "$output/assembled.json")
[[ $filename =~ ^FreeSense-[0-9]+\.[0-9]+\.[0-9]+-g[0-9]+-amd64\.iso$ ]]
expected=$(jq -er .sha256 "$output/assembled.json")
[[ $expected =~ ^[0-9a-f]{64}$ ]]
[[ $(stat -c %s "$output/$filename") == "$(jq -er .size "$output/assembled.json")" ]]
printf '%s  %s\n' "$expected" "$output/$filename" | sha256sum --check
# Match the production amd64 installer readiness check, using the local artifact.
qemu-system-x86_64 -machine q35,accel=kvm -cpu host -smp 2 -m 4096 \
  -boot order=d,strict=on -cdrom "$output/$filename" -nic none \
  -display none -monitor none -serial "file:$output/smoke-serial.log" \
  -no-reboot >"$output/smoke-qemu.log" 2>&1 &
vm_pid=$!
cleanup() { kill "$vm_pid" 2>/dev/null || true; wait "$vm_pid" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for attempt in {1..60}; do
  if grep -aqF FREESENSE_INSTALLER_READY_V1 "$output/smoke-serial.log"; then
    echo 'GitHub-hosted ISO reached the FreeSense installer.'
    exit 0
  fi
  kill -0 "$vm_pid" 2>/dev/null || break
  sleep 5
done
echo 'ISO did not reach the installer readiness marker within five minutes.' >&2
exit 1
