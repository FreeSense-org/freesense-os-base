#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: smoke-iso.sh --public-base-url URL --fingerprint SHA256 --system SHA256 --generation NUMBER --channel CHANNEL --package-train TRAIN --channel-payload SHA256" >&2
  exit 2
}

public_base_url=""
fingerprint=""
system=""
generation=""
channel=""
package_train=""
channel_payload=""
while (($#)); do
  case "$1" in
    --public-base-url) public_base_url=${2:-}; shift 2 ;;
    --fingerprint) fingerprint=${2:-}; shift 2 ;;
    --system) system=${2:-}; shift 2 ;;
    --generation) generation=${2:-}; shift 2 ;;
    --channel) channel=${2:-}; shift 2 ;;
    --package-train) package_train=${2:-}; shift 2 ;;
    --channel-payload) channel_payload=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done

[[ $public_base_url == "https://pkg.freesense.org/v1" ]] || usage
[[ $fingerprint =~ ^[0-9a-f]{64}$ ]] || usage
[[ $system =~ ^[0-9a-f]{64}$ ]] || usage
[[ $generation =~ ^[1-9][0-9]*$ ]] || usage
[[ $channel == devel || $channel == stable ]] || usage
[[ $package_train =~ ^[0-9]+[.][0-9]+$ ]] || usage
[[ $channel_payload =~ ^[0-9a-f]{64}$ ]] || usage
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

for tool in curl jq qemu-system-x86_64 setsid sha256sum stat timeout; do
  command -v "$tool" >/dev/null || { echo "missing ISO smoke dependency: $tool" >&2; exit 1; }
done
[[ -r /dev/kvm && -w /dev/kvm ]] || { echo "/dev/kvm is not available for the ISO smoke" >&2; exit 1; }

run_dir=""
smoke_pid=""

stop_smoke_group() {
  local group=$1
  [[ $group =~ ^[0-9]+$ ]] || return 1
  kill -TERM -- "-${group}" 2>/dev/null || kill -TERM "$group" 2>/dev/null || true
  sleep 1
  kill -KILL -- "-${group}" 2>/dev/null || true
  kill -KILL "$group" 2>/dev/null || true
  wait "$group" 2>/dev/null || true
  ! kill -0 -- "-${group}" 2>/dev/null && ! kill -0 "$group" 2>/dev/null
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n $smoke_pid ]] && ! stop_smoke_group "$smoke_pid"; then
    echo "ISO smoke process group ${smoke_pid} could not be stopped; preserving ${run_dir}" >&2
    exit 1
  fi
  [[ -z $run_dir ]] || rm -rf -- "$run_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_dir=$(mktemp -d "${RUNNER_TEMP}/freesense-iso-smoke.XXXXXX")
marker=${run_dir}/complete.json
iso=${run_dir}/installer.iso
serial_log=${run_dir}/serial.log
qemu_log=${run_dir}/qemu.log
artifact_url=${public_base_url}/artifacts/iso/${fingerprint}

curl --fail --location --silent --show-error --proto '=https' \
  --user-agent 'FreeSense-build/1' --retry 12 --retry-all-errors \
  --retry-delay 5 --connect-timeout 15 --max-time 45 \
  --output "$marker" "${artifact_url}/complete.json"

jq -e --arg fingerprint "$fingerprint" --arg system "$system" \
  --arg channel "$channel" --arg train "$package_train" \
  --arg payload "$channel_payload" --argjson generation "$generation" '
  .schema_version == "freesense.iso/v1" and
  .fingerprint == $fingerprint and
  .system == $system and
  .generation == $generation and
  .inputs.channel == $channel and
  .inputs.package_train == $train and
  .inputs.channel_payload == $payload and
  (.sha256 | type == "string" and test("^[0-9a-f]{64}$")) and
  (.size | type == "number" and . > 0 and floor == .) and
  (.file | type == "string" and test("^FreeSense-[0-9]+[.][0-9]+[.][0-9]+(-g[0-9]+)?-amd64[.]iso$"))
' "$marker" >/dev/null || { echo "published ISO completion marker is invalid" >&2; exit 1; }

filename=$(jq -r .file "$marker")
expected_sha=$(jq -r .sha256 "$marker")
expected_size=$(jq -r .size "$marker")
curl --fail --location --silent --show-error --proto '=https' \
  --user-agent 'FreeSense-build/1' --retry 12 --retry-all-errors \
  --retry-delay 5 --connect-timeout 15 --max-time 1800 \
  --output "$iso" "${artifact_url}/${filename}"
[[ $(stat -c %s "$iso") == "$expected_size" ]] || { echo "published ISO size mismatch" >&2; exit 1; }
[[ $(sha256sum "$iso" | awk '{print $1}') == "$expected_sha" ]] || {
  echo "published ISO SHA-256 mismatch" >&2
  exit 1
}

readiness=FREESENSE_INSTALLER_READY_V1
: >"$serial_log"
setsid timeout --signal=TERM --kill-after=15s 300s \
  qemu-system-x86_64 -name freesense-iso-smoke \
  -machine q35,accel=kvm -cpu host -smp 2 -m 4096 \
  -boot order=d,strict=on -cdrom "$iso" -nic none -display none \
  -monitor none -serial "file:${serial_log}" -no-reboot >"$qemu_log" 2>&1 &
smoke_pid=$!

ready=""
for _ in {1..60}; do
  if grep -aFq "$readiness" "$serial_log"; then
    ready=yes
    break
  fi
  kill -0 "$smoke_pid" 2>/dev/null || break
  sleep 5
done

if [[ -z $ready ]]; then
  set +e
  wait "$smoke_pid"
  qemu_status=$?
  set -e
  smoke_pid=""
  grep -aFq "$readiness" "$serial_log" && ready=yes
fi

if [[ -n $smoke_pid ]]; then
  stop_smoke_group "$smoke_pid" || { echo "ISO smoke process cleanup failed" >&2; exit 1; }
  smoke_pid=""
fi

if [[ -z $ready ]]; then
  echo "ISO did not reach the FreeSense installer within 300 seconds (QEMU status $qemu_status)" >&2
  echo "serial output: $(stat -c %s "$serial_log") bytes" >&2
  tail -n 80 "$serial_log" >&2 || true
  echo "QEMU diagnostics:" >&2
  tail -n 40 "$qemu_log" >&2 || true
  exit 1
fi

echo "ISO boot smoke passed: ${filename} (${expected_sha}) reached the FreeSense Installer"
