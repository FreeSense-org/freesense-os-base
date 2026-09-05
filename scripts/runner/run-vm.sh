#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: run-vm.sh --image-sha256 SHA256 --script FILE [--timeout SECONDS] [--vcpus N] [--memory-mib N] [--disk-gib N] [--minimum-free-gib N] [--failure-dir DIR]" >&2
  exit 2
}

image_sha=""
script_path=""
timeout_seconds=19800
failure_dir=""
vcpus=12
memory_mib=32768
disk_gib=160
minimum_free_gib=80
while (($#)); do
  case "$1" in
    --image-sha256) image_sha=${2:-}; shift 2 ;;
    --script) script_path=${2:-}; shift 2 ;;
    --timeout) timeout_seconds=${2:-}; shift 2 ;;
    --vcpus) vcpus=${2:-}; shift 2 ;;
    --memory-mib) memory_mib=${2:-}; shift 2 ;;
    --disk-gib) disk_gib=${2:-}; shift 2 ;;
    --minimum-free-gib) minimum_free_gib=${2:-}; shift 2 ;;
    --failure-dir) failure_dir=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ $image_sha =~ ^[0-9a-f]{64}$ ]] || usage
[[ -f $script_path ]] || usage
[[ -z $failure_dir || $failure_dir == /* ]] || usage
for value in "$timeout_seconds" "$disk_gib" "$minimum_free_gib"; do
  [[ $value =~ ^[1-9][0-9]*$ ]] || usage
done
[[ $vcpus == auto || $vcpus =~ ^[1-9][0-9]*$ ]] || usage
[[ $memory_mib == auto || $memory_mib =~ ^[1-9][0-9]*$ ]] || usage
: "${FSBUILD:?FSBUILD must point to the fsbuild executable}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

for tool in qemu-system-x86_64 qemu-img cloud-localds curl sha256sum base64 awk; do
  command -v "$tool" >/dev/null || { echo "missing host dependency: $tool" >&2; exit 1; }
done
[[ -r /dev/kvm && -w /dev/kvm ]] || { echo "/dev/kvm is not available to the runner" >&2; exit 1; }
if [[ $vcpus == auto ]]; then
  vcpus=$(nproc)
fi
host_memory_available_kib=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
[[ $host_memory_available_kib =~ ^[1-9][0-9]*$ ]] || {
  echo "host MemAvailable could not be determined" >&2
  exit 1
}
if [[ $memory_mib == auto ]]; then
  memory_mib=$((host_memory_available_kib * 80 / 100 / 1024))
  memory_mib=$((memory_mib / 256 * 256))
fi
(( $(nproc) >= vcpus )) || { echo "the build runner exposes fewer than ${vcpus} CPU threads" >&2; exit 1; }
(( memory_mib >= 4096 )) || { echo "less than 4 GiB is available for the FreeBSD VM" >&2; exit 1; }
echo "Configuring FreeBSD build VM with -smp ${vcpus} from host nproc=$(nproc)"
echo "Configuring FreeBSD build VM with -m ${memory_mib} MiB from host MemAvailable=$((host_memory_available_kib / 1024)) MiB (20% reserved)"

cache_dir=${HOME}/.cache/freesense-build/images
base_image=${cache_dir}/${image_sha}.qcow2
download=""
run_dir=""
overlay=""
pidfile=""

qemu_owns_overlay() {
  local qemu_pid=$1 qemu_overlay=$2
  [[ $qemu_pid =~ ^[0-9]+$ ]] || return 1
  [[ -r /proc/${qemu_pid}/cmdline ]] || return 1
  grep -Fq -- "$qemu_overlay" "/proc/${qemu_pid}/cmdline"
}

stop_qemu() {
  local qemu_pid=$1 qemu_overlay=$2
  kill -0 "$qemu_pid" 2>/dev/null || return 0
  qemu_owns_overlay "$qemu_pid" "$qemu_overlay" || return 1
  kill "$qemu_pid" 2>/dev/null || true
  for _ in {1..20}; do
    kill -0 "$qemu_pid" 2>/dev/null || return 0
    qemu_owns_overlay "$qemu_pid" "$qemu_overlay" || return 0
    sleep 1
  done
  qemu_owns_overlay "$qemu_pid" "$qemu_overlay" || return 0
  kill -9 "$qemu_pid" 2>/dev/null || true
}

cleanup() {
  status=$?
  if [[ $status -ne 0 && -n $failure_dir ]]; then
    mkdir -p "$failure_dir"
    [[ -z $serial || ! -f $serial ]] || cp -f "$serial" "${failure_dir}/serial.log"
    {
      echo "FreeSense build VM failure"
      echo "status=${status}"
      echo "image_sha256=${image_sha}"
      echo "timeout_seconds=${timeout_seconds}"
      echo "github_run_id=${GITHUB_RUN_ID:-local}"
      echo "github_run_attempt=${GITHUB_RUN_ATTEMPT:-1}"
      echo "collected_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "The rendered worker and cidata are intentionally excluded because they contain credentials."
    } >"${failure_dir}/README.txt"
  fi
  if [[ -n $pidfile && -f $pidfile ]]; then
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      if ! stop_qemu "$pid" "$overlay"; then
        echo "refusing to stop PID $pid because it does not own this build overlay" >&2
      fi
    fi
  fi
  [[ -z $run_dir ]] || rm -rf -- "$run_dir"
  [[ -z $download ]] || rm -f -- "$download"
  return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$cache_dir"
chmod 700 "$cache_dir"

while IFS= read -r -d '' stale_download; do
  rm -f -- "$stale_download"
done < <(find "$cache_dir" -mindepth 1 -maxdepth 1 -type f -name '*.download.*' -print0)

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
  download=""
fi
while IFS= read -r -d '' stale_image; do
  [[ $stale_image == "$base_image" ]] || rm -f -- "$stale_image"
done < <(find "$cache_dir" -mindepth 1 -maxdepth 1 -type f -name '*.qcow2' -mtime +14 -print0)

cleanup_orphans() {
  while IFS= read -r -d '' directory; do
    orphan_pid=$(cat "${directory}/qemu.pid" 2>/dev/null || true)
    if [[ $orphan_pid =~ ^[0-9]+$ ]] && kill -0 "$orphan_pid" 2>/dev/null; then
      if qemu_owns_overlay "$orphan_pid" "${directory}/worker.qcow2"; then
        echo "Stopping orphaned FreeSense QEMU process ${orphan_pid}"
        stop_qemu "$orphan_pid" "${directory}/worker.qcow2"
      else
        echo "Discarding stale runner directory with reused PID ${orphan_pid}"
      fi
    fi
    rm -rf -- "$directory"
  done < <(find "$RUNNER_TEMP" -mindepth 1 -maxdepth 1 -type d -name 'freesense-runner.*' -print0)
}
cleanup_orphans

run_dir=$(mktemp -d "${RUNNER_TEMP}/freesense-runner.XXXXXX")
overlay=${run_dir}/worker.qcow2
seed=${run_dir}/seed.img
serial=${run_dir}/serial.log
pidfile=${run_dir}/qemu.pid
vars=${run_dir}/OVMF_VARS.fd
nonce=$(printf '%s-%s-%s' "${GITHUB_RUN_ID:-local}" "${GITHUB_RUN_ATTEMPT:-1}" "$RANDOM" | sha256sum | awk '{print substr($1,1,24)}')
begin_marker=FREESENSE_RUNNER_JOB_BEGIN_${nonce}
ok_marker=FREESENSE_RUNNER_JOB_OK_${nonce}
fail_marker=FREESENSE_RUNNER_JOB_FAILED_${nonce}

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
qemu-img resize -q "$overlay" "${disk_gib}G"
available_kib=$(df -Pk "$RUNNER_TEMP" | awk 'END {print $4}')
required_kib=$((minimum_free_gib * 1024 * 1024))
(( available_kib >= required_kib )) || {
  echo "build runner has less than ${minimum_free_gib} GiB free for the VM overlay" >&2
  exit 1
}

payload_b64=$(base64 -w 0 "$script_path")
wrapper=$(cat <<EOF
#!/bin/sh
set -u
serial=/dev/ttyu0
[ -c "\$serial" ] || serial=/dev/console
exec <"\$serial" >"\$serial" 2>&1
echo "$begin_marker"
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
  -smp "$vcpus" \
  -m "$memory_mib" \
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

show_diagnostics() {
  echo "FreeSense phase history:" >&2
  tr '\r' '\n' <"$serial" \
    | grep -E '^(FreeSense phase|FREESENSE_RUNNER_JOB_)' \
    | tail -n 100 >&2 || true
  echo "FreeBSD serial tail:" >&2
  tr '\r' '\n' <"$serial" | tail -n 400 >&2
}

while true; do
  now=$(date +%s)
  recent=$(tail -c 1048576 "$serial")
  if grep -Fq "$ok_marker" <<<"$recent"; then
    echo "FreeBSD stage completed successfully"
    break
  fi
  if grep -Fq "$fail_marker" <<<"$recent"; then
    echo "FreeBSD stage reported failure" >&2
    show_diagnostics
    exit 1
  fi
  if [[ $seen_begin == false ]] && grep -Fq "$begin_marker" "$serial"; then seen_begin=true; fi
  if [[ $seen_begin == false ]] && (( now - start >= 300 )); then
    echo "FreeBSD booted without executing nuageinit user-data within 300s" >&2
    show_diagnostics
    exit 1
  fi
  if (( now - start >= timeout_seconds )); then
    echo "FreeBSD stage exceeded ${timeout_seconds}s" >&2
    show_diagnostics
    exit 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "FreeBSD VM stopped before its success marker" >&2
    show_diagnostics
    exit 1
  fi
  if (( now >= next_report )); then
    cpu=$(ps -p "$pid" -o %cpu= 2>/dev/null | xargs || true)
    rss_kib=$(ps -p "$pid" -o rss= 2>/dev/null | xargs || true)
    avail_kib=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
    load=$(awk '{print $1, $2, $3}' /proc/loadavg)
    phase=$(printf '%s\n' "$recent" | tr '\r' '\n' \
      | grep -E '^(FreeSense|==>|---|FREESENSE_)' | tail -n 1 || true)
    printf 'Build runner heartbeat: guest_started=%s qemu_cpu=%s%% qemu_rss=%sMiB host_available=%sMiB load=%s phase=%s\n' \
      "$seen_begin" "${cpu:-0}" "$(( ${rss_kib:-0} / 1024 ))" "$(( ${avail_kib:-0} / 1024 ))" "$load" "${phase:-booting}"
    next_report=$((now + 180))
  fi
  sleep 10
done
