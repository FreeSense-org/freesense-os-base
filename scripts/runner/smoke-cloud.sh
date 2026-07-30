#!/usr/bin/env bash
set -euo pipefail

declare public_base_url= fingerprint= bundle= system= packages= generation= channel=
declare filesystem= virtual_size_gib= failure_dir=
while (($#)); do
  case "$1" in
    --public-base-url) public_base_url=$2; shift 2 ;;
    --fingerprint) fingerprint=$2; shift 2 ;;
    --bundle) bundle=$2; shift 2 ;;
    --system) system=$2; shift 2 ;;
    --packages) packages=$2; shift 2 ;;
    --generation) generation=$2; shift 2 ;;
    --channel) channel=$2; shift 2 ;;
    --filesystem) filesystem=$2; shift 2 ;;
    --virtual-size-gib) virtual_size_gib=$2; shift 2 ;;
    --failure-dir) failure_dir=$2; shift 2 ;;
    *) echo "unknown cloud smoke argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$filesystem" == ufs || "$filesystem" == zfs ]]
[[ "$virtual_size_gib" =~ ^[1-9][0-9]*$ ]]
virtual_size=$((virtual_size_gib * 1024 * 1024 * 1024))
for value in "$fingerprint" "$bundle" "$system" "$packages"; do
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid cloud smoke identity" >&2; exit 2; }
done
if [[ -n "$failure_dir" ]]; then
  [[ "$failure_dir" == /* ]] || {
    echo "--failure-dir must be an absolute path" >&2
    exit 2
  }
fi

work=$(mktemp -d)
package_failure_artifacts() {
  # Preserve enough of the smoke workspace for offline inspection after CI fails.
  # Never publish the generated private SSH key.
  local status=$1 name size
  [[ -n "$failure_dir" ]] || return 0
  mkdir -p \
    "${failure_dir}/logs" \
    "${failure_dir}/seed" \
    "${failure_dir}/images"
  {
    echo "FreeSense cloud smoke failure bundle"
    echo "status=${status}"
    echo "filesystem=${filesystem}"
    echo "fingerprint=${fingerprint}"
    echo "bundle=${bundle}"
    echo "system=${system}"
    echo "packages=${packages}"
    echo "generation=${generation}"
    echo "channel=${channel}"
    echo "virtual_size_gib=${virtual_size_gib}"
    echo "public_base_url=${public_base_url}"
    echo "collected_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo
    echo "Contents:"
    echo "  logs/             serial console captures"
    echo "  seed/             cloud-init seed used by smoke (no private keys)"
    echo "  images/           published xz plus post-boot qcow2 when available"
    echo "  complete.json     published cloud image marker"
    echo "  README.txt        this file"
    echo
    echo "Boot the post-boot disk (when present) with the same cidata seed under"
    echo "seed/ to inspect FreeSense-cloud-init state after the failed smoke run."
  } >"${failure_dir}/README.txt"
  cp -f "${work}/complete.json" "${failure_dir}/complete.json" 2>/dev/null || true
  for name in bios.log bios-second.log uefi-two.log; do
    [[ -f "${work}/${name}" ]] || continue
    cp -f "${work}/${name}" "${failure_dir}/logs/${name}"
  done
  for name in user-data meta-data network-config \
    user-data-two meta-data-two network-config-two; do
    [[ -f "${work}/${name}" ]] || continue
    cp -f "${work}/${name}" "${failure_dir}/seed/${name}"
  done
  [[ -f "${work}/id.pub" ]] && cp -f "${work}/id.pub" "${failure_dir}/seed/smoke-id.pub"
  # Keep the immutable published qcow2.xz (pre-boot). Prefer not to re-upload
  # multi-gigabyte raw images; post-boot qcow2 is the interesting mutable disk.
  shopt -s nullglob
  for name in "${work}"/*.qcow2.xz; do
    cp -f "$name" "${failure_dir}/images/published-$(basename "$name")" || true
  done
  shopt -u nullglob
  if [[ -f "${work}/disk.qcow2" ]]; then
    # Re-sparsify/compress the post-boot disk for GitHub artifact download.
    if command -v qemu-img >/dev/null 2>&1; then
      qemu-img convert -c -O qcow2 \
        "${work}/disk.qcow2" "${failure_dir}/images/disk-post-boot.qcow2" || \
        cp -f "${work}/disk.qcow2" "${failure_dir}/images/disk-post-boot.qcow2" || true
    else
      cp -f "${work}/disk.qcow2" "${failure_dir}/images/disk-post-boot.qcow2" || true
    fi
    if [[ -f "${failure_dir}/images/disk-post-boot.qcow2" ]] && command -v xz >/dev/null 2>&1; then
      xz -T0 -1 -f "${failure_dir}/images/disk-post-boot.qcow2" || true
    fi
  fi
  {
    echo "{"
    echo "  \"status\": ${status},"
    echo "  \"filesystem\": \"${filesystem}\","
    echo "  \"fingerprint\": \"${fingerprint}\","
    echo "  \"bundle\": \"${bundle}\","
    echo "  \"system\": \"${system}\","
    echo "  \"packages\": \"${packages}\","
    echo "  \"generation\": ${generation},"
    echo "  \"channel\": \"${channel}\","
    echo "  \"virtual_size_gib\": ${virtual_size_gib},"
    echo "  \"public_image_base\": \"${public_base_url}/artifacts/cloud/${fingerprint}\""
    echo "}"
  } >"${failure_dir}/manifest.json"
  if command -v find >/dev/null 2>&1; then
    echo "cloud smoke failure artifacts:" >&2
    find "$failure_dir" -type f -printf '  %p (%s bytes)\n' >&2 || \
      find "$failure_dir" -type f >&2 || true
  else
    echo "cloud smoke failure artifacts written to ${failure_dir}" >&2
  fi
}

cleanup() {
  local status=$?
  if [[ -n "${qemu_pid:-}" ]]; then
    kill "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
    unset qemu_pid
  fi
  if [[ -n "${failure_dir:-}" && "$status" -ne 0 ]]; then
    package_failure_artifacts "$status" || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT
base="${public_base_url}/artifacts/cloud/${fingerprint}"
curl --fail --silent --show-error --location --retry 5 \
  "${base}/complete.json" -o "${work}/complete.json"
jq -e --arg fingerprint "$fingerprint" --arg bundle "$bundle" \
  --arg system "$system" --arg packages "$packages" \
  --arg channel "$channel" --arg filesystem "$filesystem" \
  --argjson generation "$generation" --argjson virtual_size "$virtual_size" '
  .schema_version == "freesense.cloud-image/v1" and
  .fingerprint == $fingerprint and .bundle_fingerprint == $bundle and
  .generation == $generation and .channel == $channel and
  .filesystem == $filesystem and .disk.virtual_size == $virtual_size and
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
qemu-img resize "${work}/disk.qcow2" "$((virtual_size_gib + 8))G"

prepare_ovmf() {
  local vars=$1 candidate code= template=
  for candidate in \
    /usr/share/OVMF/OVMF_CODE_4M.fd \
    /usr/share/OVMF/OVMF_CODE.fd \
    /usr/share/edk2/ovmf/OVMF_CODE.fd; do
    [[ ! -f "$candidate" ]] || { code=$candidate; break; }
  done
  for candidate in \
    /usr/share/OVMF/OVMF_VARS_4M.fd \
    /usr/share/OVMF/OVMF_VARS.fd \
    /usr/share/edk2/ovmf/OVMF_VARS.fd; do
    [[ ! -f "$candidate" ]] || { template=$candidate; break; }
  done
  [[ -n "$code" && -n "$template" ]]
  cp "$template" "$vars"
  ovmf_code=$code
}

prepare_qga() {
  local socket=$1
  rm -f "$socket"
  qga_args=(
    -device virtio-serial-pci
    -chardev "socket,path=${socket},server=on,wait=off,id=qga0"
    -device "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0"
  )
}

qga_exec() {
  local label=$1 command=$2
  echo "cloud smoke guest-agent diagnostic: ${label}" >&2
  # Longer per-request timeouts: a single giant guest-exec previously hit the
  # 10s socket timeout and dropped every diagnostic section at once.
  python3 - "${work}/qga.sock" "$command" <<'PY' >&2 || true
import base64
import json
import socket
import sys
import time

socket_path, command = sys.argv[1:]
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(45)
        deadline = time.time() + 90
        while True:
            try:
                client.connect(socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if time.time() >= deadline:
                    raise
                time.sleep(0.5)
        stream = client.makefile("rwb", buffering=0)
        sequence = 0

        def request(execute, arguments=None):
            # Keep IDs stable so asynchronous or stale responses cannot be
            # mistaken for the command currently being diagnosed.
            global sequence
            sequence += 1
            identifier = f"freesense-smoke-{sequence}"
            payload = {"execute": execute, "id": identifier}
            if arguments is not None:
                payload["arguments"] = arguments
            stream.write(json.dumps(payload).encode() + b"\n")
            while True:
                line = stream.readline()
                if not line:
                    raise RuntimeError("guest agent closed the diagnostic socket")
                line = line.lstrip(b"\xff").strip()
                if not line:
                    continue
                response = json.loads(line)
                if response.get("id") != identifier:
                    continue
                if "error" in response:
                    raise RuntimeError(json.dumps(response["error"], sort_keys=True))
                return response.get("return")

        request("guest-ping")
        started = request(
            "guest-exec",
            {
                "path": "/bin/sh",
                "arg": ["-c", command],
                "capture-output": True,
            },
        )
        pid = started["pid"]
        for _ in range(300):
            status = request("guest-exec-status", {"pid": pid})
            if status.get("exited"):
                for field in ("out-data", "err-data"):
                    if status.get(field):
                        sys.stdout.write(
                            base64.b64decode(status[field]).decode(
                                "utf-8", errors="replace"
                            )
                        )
                print(f"guest-exec exitcode={status.get('exitcode')}")
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("guest-exec diagnostic did not finish")
except Exception as error:
    print(f"guest-agent diagnostic unavailable: {error}")
PY
}

diagnose_guest_ssh() {
  # Split into short guest-exec batches so a slow/hung section cannot erase
  # package version, user-data, and PF evidence for the whole failure.
  qga_exec "package and cloud-init versions" '
    echo "=== PACKAGE AND CLOUD-INIT VERSIONS ==="
    /usr/sbin/pkg query "%n-%v" FreeSense-cloud-init 2>/dev/null || true
    /usr/local/bin/cloud-init --version 2>&1 || true
    /bin/ps axww | /usr/bin/grep -E "[q]emu-ga|[q]emu_guest_agent" || true
    exit 0
  '
  qga_exec "userdata sources and FreeSense state" '
    echo "=== CLOUD-INIT USERDATA SOURCES ==="
    /usr/local/bin/cloud-init query instance-id 2>&1 || true
    /usr/local/bin/cloud-init query userdata 2>&1 | /usr/bin/head -c 2048 || true
    echo
    /bin/ls -la /var/lib/cloud/instance 2>&1 || true
    /usr/bin/head -c 2048 /var/lib/cloud/instance/user-data.txt 2>&1 || true
    echo
    echo "=== FREESENSE CLOUD STATE ==="
    /bin/cat /var/db/freesense-cloud-init/instance.json 2>&1 || true
    /usr/bin/grep -E "cloudinit|authorizedkeys|sshdkeyonly|FreeSense cloud temporary SSH|management|10[.]0[.]2[.]2" /conf/config.xml 2>&1 || true
    exit 0
  '
  qga_exec "SSH listeners and effective policy" '
    echo "=== SSH PROCESSES AND LISTENERS ==="
    /bin/ps axww | /usr/bin/grep -E "[s]shd|check_reload_status" || true
    /usr/bin/sockstat -46 -l 2>&1 || true
    echo "=== EFFECTIVE SSHD POLICY ==="
    /usr/sbin/sshd -T 2>&1 |
      /usr/bin/grep -E "^(port|listenaddress|permitrootlogin|passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|pubkeyauthentication|authorizedkeysfile) " || true
    echo "=== CLOUD SSH CONFIG ==="
    /usr/bin/grep -E "<ssh>|<sshdkeyonly>|FreeSense cloud temporary SSH|<interface>wan</interface>|<address>10[.]0[.]2[.]2/32</address>|<port>22</port>" /conf/config.xml || true
    echo "=== AUTHORIZED KEYS ON DISK ==="
    /bin/ls -la /root/.ssh 2>&1 || true
    /usr/bin/head -c 512 /root/.ssh/authorized_keys 2>&1 || true
    exit 0
  '
  qga_exec "PF rules interfaces and routes" '
    echo "=== PF FILTER AND NAT ==="
    /sbin/pfctl -vvsr 2>&1 |
      /usr/bin/grep -E "FreeSense cloud temporary SSH|port (= )?22|10[.]0[.]2[.]2|Evaluations|Packets|Bytes|States" || true
    /sbin/pfctl -sn 2>&1 || true
    echo "=== PF STATES, INTERFACES, AND ROUTES ==="
    /sbin/pfctl -ss 2>&1 | /usr/bin/grep -E "10[.]0[.]2[.]|:22" || true
    /usr/bin/netstat -rn 2>&1 || true
    /sbin/ifconfig -a 2>&1 || true
    exit 0
  '
  qga_exec "service logs and controlled sshd restart" '
    echo "=== SSH SERVICE LOGS ==="
    /usr/bin/grep -Ei "sshd|check_reload_status" /var/log/system.log 2>/dev/null |
      /usr/bin/tail -n 100 || true
    echo "=== CLOUD-INIT LOG TAIL ==="
    /usr/bin/tail -n 80 /var/log/cloud-init.log 2>/dev/null || true
    /usr/bin/tail -n 40 /var/log/cloud-init-output.log 2>/dev/null || true
    echo "=== CONTROLLED SSHD RESTART ==="
    if [ -x /etc/sshd ]; then
      /etc/sshd
      status=$?
    elif [ -x /etc/rc.d/sshd ]; then
      /etc/rc.d/sshd onerestart
      status=$?
    elif [ -x /usr/sbin/service ]; then
      /usr/sbin/service sshd onerestart
      status=$?
    else
      echo "no privileged sshd restart helper available"
      status=0
    fi
    echo "sshd restart exitcode=${status}"
    /bin/sleep 2
    /bin/ps axww | /usr/bin/grep -E "[s]shd|check_reload_status" || true
    /usr/bin/sockstat -46 -l 2>&1 | /usr/bin/grep -E "sshd|:22" || true
    exit 0
  '
}

diagnose_ssh_timeout() {
  local log=$1 user
  echo "cloud smoke SSH readiness timed out" >&2
  if timeout 3 bash -c '</dev/tcp/127.0.0.1/10022' 2>/dev/null; then
    echo "cloud smoke diagnostic: forwarded TCP/22 is reachable" >&2
  else
    echo "cloud smoke diagnostic: forwarded TCP/22 is unreachable" >&2
  fi
  diagnose_guest_ssh
  for user in admin root; do
    echo "cloud smoke diagnostic: verbose public-key attempt for ${user}" >&2
    timeout 15 ssh -vvv \
      -o BatchMode=yes -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=5 -i "${work}/id" -p 10022 \
      "${user}@127.0.0.1" true </dev/null >&2 || true
  done
  cat "$log" >&2
}

boot_and_wait() {
  local disk=$1 format=$2 firmware=$3 log=$4
  local firmware_args=()
  if [[ "$firmware" == uefi ]]; then
    prepare_ovmf "${work}/OVMF_VARS.fd"
    firmware_args=(
      -drive "if=pflash,format=raw,readonly=on,file=${ovmf_code}"
      -drive "if=pflash,format=raw,file=${work}/OVMF_VARS.fd"
    )
  fi
  prepare_qga "${work}/qga.sock"
  qemu-system-x86_64 -machine accel=kvm -m 4096 -nographic \
    "${firmware_args[@]}" \
    "${qga_args[@]}" \
    -drive "if=virtio,format=${format},file=${disk},cache=none" \
    -drive "if=virtio,format=raw,readonly=on,file=${work}/cidata.iso" \
    -netdev user,id=wan,hostfwd=tcp:127.0.0.1:10022-:22,hostfwd=tcp:127.0.0.1:10443-:443 \
    -device virtio-net-pci,netdev=wan,mac=52:54:00:12:34:56 \
    -serial "file:${log}" -monitor none &
  qemu_pid=$!
  for _ in {1..120}; do
    if ssh -q -o BatchMode=yes -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes -o ConnectTimeout=2 \
      -i "${work}/id" -p 10022 admin@127.0.0.1 true; then
      return 0
    fi
    kill -0 "$qemu_pid" 2>/dev/null || { cat "$log" >&2; return 1; }
    sleep 5
  done
  diagnose_ssh_timeout "$log"
  return 1
}

boot_and_wait "${work}/disk.qcow2" qcow2 bios "${work}/bios.log"
ssh_args=(-q -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "${work}/id" -p 10022 admin@127.0.0.1)
"${ssh_args[@]}" "test \"\$(sysrc -n qemu_guest_agent_enable)\" = YES &&
  service qemu-guest-agent status &&
  test -s /etc/ssh/ssh_host_ed25519_key &&
  grep -q freesense-cloud-smoke /conf/config.xml &&
  test \"\$(cloud-init status --wait)\" = \"status: done\" &&
  if [ '${filesystem}' = ufs ]; then
    test \"\$(df -k / | awk 'NR == 2 {print \$2}')\" -gt $((virtual_size_gib * 1024 * 1024))
  else
    test \"\$(zpool get -H -o value bootfs FreeSense)\" = FreeSense/ROOT/default &&
    test \"\$(zpool list -Hp -o size FreeSense)\" -gt ${virtual_size} &&
    zpool status -x FreeSense | grep -q healthy &&
    /sbin/bectl check &&
    zfs list -H -o name,mountpoint |
      awk '\$2 == \"/cf\" { found = (\$1 ~ /^FreeSense\\/ROOT\\/default\\//) } END { exit !found }' &&
    zfs list -H -o name,mountpoint |
      awk '\$2 == \"/var\\/db\\/pkg\" { found = (\$1 ~ /^FreeSense\\/ROOT\\/default\\//) } END { exit !found }'
  fi"
"${ssh_args[@]}" "sshd -T |
  grep -qx 'passwordauthentication no' &&
  sshd -T | grep -Eq '^(kbdinteractiveauthentication|challengeresponseauthentication) no$'"
if ssh -q -o BatchMode=yes -o PreferredAuthentications=password \
  -o PubkeyAuthentication=no -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 \
  -p 10022 admin@127.0.0.1 true; then
  echo "SSH password authentication was accepted" >&2
  exit 1
fi
if [[ "$filesystem" == zfs ]]; then
  "${ssh_args[@]}" '/usr/local/sbin/freesense-be create cloud-smoke-be &&
    /usr/local/sbin/freesense-be activate cloud-smoke-be'
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
"${ssh_args[@]}" "test \"\$(grep -c '<instance_id>freesense-smoke-' /conf/config.xml)\" = 1 &&
  test \"\$(grep -c 'FreeSense cloud temporary SSH' /conf/config.xml)\" = 1 &&
  if [ '${filesystem}' = zfs ]; then
    df / | grep -q 'FreeSense/ROOT/cloud-smoke-be'
  else
    true
  fi"
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
prepare_ovmf "${work}/OVMF_VARS-two.fd"
prepare_qga "${work}/qga-two.sock"
qemu-system-x86_64 -machine accel=kvm -m 4096 -nographic \
  -drive "if=pflash,format=raw,readonly=on,file=${ovmf_code}" \
  -drive "if=pflash,format=raw,file=${work}/OVMF_VARS-two.fd" \
  "${qga_args[@]}" \
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
