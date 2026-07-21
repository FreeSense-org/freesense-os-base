#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: run-vm.sh --image-sha256 SHA256 --script FILE [--timeout SECONDS]" >&2
  exit 2
}

image_sha=""
script_path=""
timeout_seconds=19800
while (($#)); do
  case "$1" in
    --image-sha256) image_sha=${2:-}; shift 2 ;;
    --script) script_path=${2:-}; shift 2 ;;
    --timeout) timeout_seconds=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ $image_sha =~ ^[0-9a-f]{64}$ ]] || usage
[[ -f $script_path ]] || usage
: "${FSBUILD:?FSBUILD must point to the fsbuild executable}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

for tool in qemu-system-x86_64 qemu-img cloud-localds curl sha256sum base64 awk; do
  command -v "$tool" >/dev/null || { echo "missing host dependency: $tool" >&2; exit 1; }
done
[[ -r /dev/kvm && -w /dev/kvm ]] || { echo "/dev/kvm is not available to the runner" >&2; exit 1; }
(( $(nproc) >= 16 )) || { echo "the Ryzen runner exposes fewer than 16 CPU threads" >&2; exit 1; }

cache_dir=${HOME}/.cache/freesense-build/images
base_image=${cache_dir}/${image_sha}.qcow2
mkdir -p "$cache_dir"
chmod 700 "$cache_dir"

verify_image() {
  [[ -f $base_image ]] && [[ $(sha256sum "$base_image" | awk '{print $1}') == "$image_sha" ]]
}

if ! verify_image; then
  rm -f "$base_image"
  download=${base_image}.download.$$
  rm -f "$download"
  echo "Downloading pinned FreeBSD worker image $image_sha into the runner cache"
  image_url=$($FSBUILD blob url --sha256 "$image_sha" --validity 30m)
  curl --fail --location --silent --show-error --retry 4 --output "$download" "$image_url"
  unset image_url
  actual=$(sha256sum "$download" | awk '{print $1}')
  [[ $actual == "$image_sha" ]] || { rm -f "$download"; echo "worker image checksum mismatch" >&2; exit 1; }
  chmod 600 "$download"
  mv "$download" "$base_image"
fi
verify_image || { echo "cached worker image verification failed" >&2; exit 1; }
qemu-img check -q "$base_image"

run_dir=$(mktemp -d "${RUNNER_TEMP}/freesense-ryzen.XXXXXX")
overlay=${run_dir}/worker.qcow2
seed=${run_dir}/seed.img
serial=${run_dir}/serial.log
pidfile=${run_dir}/qemu.pid
vars=${run_dir}/OVMF_VARS.fd
nonce=$(printf '%s-%s-%s' "${GITHUB_RUN_ID:-local}" "${GITHUB_RUN_ATTEMPT:-1}" "$RANDOM" | sha256sum | awk '{print substr($1,1,24)}')
begin_marker=FREESENSE_RYZEN_JOB_BEGIN_${nonce}
ok_marker=FREESENSE_RYZEN_JOB_OK_${nonce}
fail_marker=FREESENSE_RYZEN_JOB_FAILED_${nonce}

cleanup() {
  if [[ -f $pidfile ]]; then
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      for _ in {1..20}; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -rf "$run_dir"
}
trap cleanup EXIT INT TERM

code=""
vars_template=""
for firmware in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd /usr/share/edk2/ovmf/OVMF_CODE.fd; do
  [[ -f $firmware ]] && { code=$firmware; break; }
done
for firmware in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd /usr/share/edk2/ovmf/OVMF_VARS.fd; do
  [[ -f $firmware ]] && { vars_template=$firmware; break; }
done
[[ -n $code && -n $vars_template ]] || { echo "OVMF firmware was not found" >&2; exit 1; }
cp "$vars_template" "$vars"

qemu-img create -q -f qcow2 -F qcow2 -b "$base_image" "$overlay"
qemu-img resize -q "$overlay" 160G

payload_b64=$(base64 -w 0 "$script_path")
wrapper=$(cat <<EOF
#!/bin/sh
set -u
serial=/dev/ttyu0
[ -c "\$serial" ] || serial=/dev/console
exec <"\$serial" >"\$serial" 2>&1
echo "$begin_marker"
network_ready=false
for attempt in \$(jot 60 1); do
  if /usr/bin/fetch -qo /dev/null https://github.com/robots.txt; then
    network_ready=true
    break
  fi
  echo "FreeSense stage waiting for guest network (\${attempt}/60)"
  sleep 5
done
if [ "\$network_ready" != true ]; then
  echo "$fail_marker status=network-timeout"
  shutdown -p now
  exit 1
fi
status=0
/bin/sh /root/freesense-stage.sh || status=\$?
if [ "\$status" -eq 0 ]; then
  echo "$ok_marker"
else
  echo "$fail_marker status=\$status"
fi
sync
shutdown -p now
exit "\$status"
EOF
)
wrapper_b64=$(printf '%s' "$wrapper" | base64 -w 0)
cat >"${run_dir}/user-data" <<EOF
#!/bin/sh
# FreeBSD's BASIC-CLOUDINIT image uses nuageinit. Its cidata handler executes
# user-data only when the first line is a shebang.
set -eu
printf '%s' '${payload_b64}' | /usr/bin/base64 -d >/root/freesense-stage.sh
printf '%s' '${wrapper_b64}' | /usr/bin/base64 -d >/root/freesense-run.sh
chmod 0700 /root/freesense-stage.sh /root/freesense-run.sh
exec /bin/sh /root/freesense-run.sh
EOF
cat >"${run_dir}/meta-data" <<EOF
instance-id: freesense-${nonce}
local-hostname: freesense-worker
EOF
unset payload_b64 wrapper_b64 wrapper
cloud-localds "$seed" "${run_dir}/user-data" "${run_dir}/meta-data" >/dev/null
rm -f "${run_dir}/user-data" "${run_dir}/meta-data"

touch "$serial"
qemu-system-x86_64 \
  -name freesense-${nonce} \
  -machine q35,accel=kvm \
  -cpu host \
  -smp 16 \
  -m 32768 \
  -drive if=pflash,format=raw,readonly=on,file="$code" \
  -drive if=pflash,format=raw,file="$vars" \
  -drive if=virtio,format=qcow2,cache=none,discard=unmap,file="$overlay" \
  -drive if=ide,media=cdrom,format=raw,readonly=on,file="$seed" \
  -device virtio-net-pci,netdev=net0 \
  -netdev user,id=net0 \
  -display none \
  -serial file:"$serial" \
  -no-reboot \
  -daemonize \
  -pidfile "$pidfile"

pid=$(cat "$pidfile")
start=$(date +%s)
next_report=$start
seen_begin=false
while true; do
  now=$(date +%s)
  if grep -Fq "$ok_marker" "$serial"; then
    echo "FreeBSD stage completed successfully"
    break
  fi
  if grep -Fq "$fail_marker" "$serial"; then
    echo "FreeBSD stage reported failure" >&2
    tail -n 200 "$serial" >&2
    exit 1
  fi
  if grep -Fq "$begin_marker" "$serial"; then seen_begin=true; fi
  if [[ $seen_begin == false ]] && (( now - start >= 300 )); then
    echo "FreeBSD booted without executing nuageinit user-data within 300s" >&2
    tail -n 200 "$serial" >&2
    exit 1
  fi
  if (( now - start >= timeout_seconds )); then
    echo "FreeBSD stage exceeded ${timeout_seconds}s" >&2
    tail -n 200 "$serial" >&2
    exit 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "FreeBSD VM stopped before its success marker" >&2
    tail -n 200 "$serial" >&2
    exit 1
  fi
  if (( now >= next_report )); then
    cpu=$(ps -p "$pid" -o %cpu= | xargs)
    rss_kib=$(ps -p "$pid" -o rss= | xargs)
    avail_kib=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
    load=$(awk '{print $1, $2, $3}' /proc/loadavg)
    phase=$(grep -E '^(FreeSense|==>|---|FREESENSE_)' "$serial" | tail -n 1 || true)
    printf 'Ryzen heartbeat: guest_started=%s qemu_cpu=%s%% qemu_rss=%sMiB host_available=%sMiB load=%s phase=%s\n' \
      "$seen_begin" "${cpu:-0}" "$(( ${rss_kib:-0} / 1024 ))" "$(( ${avail_kib:-0} / 1024 ))" "$load" "${phase:-booting}"
    next_report=$((now + 180))
  fi
  sleep 10
done
