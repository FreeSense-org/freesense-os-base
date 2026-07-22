# Assemble only from the exact signed system repository selected by the channel.
fetch_input "${JAIL_OBJECT}" /root/jail-base.txz
configure_source
fetch_repository system "${SYSTEM_ID}" /root/system-repo
cd /root/freesense-src
phase iso-assembly-adapter
if ! grep -Fq 'LOGFILE="${BUILDER_LOGS}/isoimage.${TARGET}"' \
  tools/ci/freesense-assemble-iso.sh; then
  [ "${SOURCE_SHA}" = b094eb3c173b675f224c33a0ad2968df98dedb58 ] || {
    echo "unsupported ISO assembler source: ${SOURCE_SHA}" >&2
    exit 1
  }
  git fetch -q --depth=1 origin c5d29a04e9972a4dc9114bc2b55f16a72a34712a
  git restore --source=c5d29a04e9972a4dc9114bc2b55f16a72a34712a -- \
    tools/builder_common.sh tools/ci/freesense-assemble-iso.sh
fi
grep -Fq 'LOGFILE="${BUILDER_LOGS}/isoimage.${TARGET}"' \
  tools/ci/freesense-assemble-iso.sh
phase channel-fetch
printf '%s' "${CHANNEL_PAYLOAD_B64}" | openssl base64 -d -A >/tmp/channel-payload.json
printf '%s' "${CHANNEL_SIGNATURE_B64}" | openssl base64 -d -A >/tmp/channel-signature.bin
openssl dgst -sha256 -verify /root/sign/channel-public.pem \
  -signature /tmp/channel-signature.bin /tmp/channel-payload.json >/dev/null
test "$(sha256 -q /tmp/channel-payload.json)" = "${CHANNEL_PAYLOAD_SHA256}"
jq -e --arg channel "${CHANNEL}" --arg system "${SYSTEM_ID}" \
  --arg train "${PACKAGE_TRAIN}" --argjson generation "${GENERATION}" \
  '.schema_version == "freesense.channels/v1" and
   .channels[$channel].package_train == $train and
   .channels[$channel].system.fingerprint == $system and
   .channels[$channel].system.generation == $generation and
   (.channels[$channel].packages == null or
    .channels[$channel].packages.system_fingerprint == $system)' \
  /tmp/channel-payload.json >/dev/null
export FREESENSE_ASSEMBLY_CHANNEL="${CHANNEL}"
export FREESENSE_ASSEMBLY_CHANNEL_PAYLOAD=/tmp/channel-payload.json
phase channel-ready
phase iso-tools-fetch
mkdir -p /root/freebsd-tools/release/amd64 /root/freebsd-tools/release/scripts
fetch -qo /root/freebsd-tools/release/amd64/mkisoimages.sh \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/amd64/mkisoimages.sh"
fetch -qo /root/freebsd-tools/release/scripts/make-manifest.sh \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/scripts/make-manifest.sh"
fetch -qo /root/freebsd-tools/release/scripts/tools.subr \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/scripts/tools.subr"
mkdir -p /root/freebsd-tools/tools/boot
fetch -qo /root/freebsd-tools/tools/boot/install-boot.sh \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/tools/boot/install-boot.sh"
test -s /root/freebsd-tools/release/amd64/mkisoimages.sh
test -s /root/freebsd-tools/release/scripts/make-manifest.sh
test -s /root/freebsd-tools/release/scripts/tools.subr
test -s /root/freebsd-tools/tools/boot/install-boot.sh
export FREESENSE_ASSEMBLY_SYSTEM_REPO=/root/system-repo
export FREESENSE_ASSEMBLY_FREEBSD_SRC=/root/freebsd-tools

# Pin both filesystem timestamps and hybrid GPT identifiers to the source
# commit. mkisoimages invokes makefs directly for its EFI image, so PATH
# wrappers cover that call as well as the final makefs and mkimg calls.
real_makefs=$(command -v makefs)
real_mkimg=$(command -v mkimg)
mkdir -p /root/deterministic-tools
cat >/root/deterministic-tools/makefs <<EOF
#!/bin/sh
exec "${real_makefs}" -T "${FREESENSE_SOURCE_COMMIT_TIME}" "\$@"
EOF
cat >/root/deterministic-tools/mkimg <<EOF
#!/bin/sh
exec "${real_mkimg}" -t "${FREESENSE_SOURCE_COMMIT_TIME}" "\$@"
EOF
chmod 555 /root/deterministic-tools/makefs /root/deterministic-tools/mkimg
PATH="/root/deterministic-tools:${PATH}"
export PATH
phase iso-assemble
./build.sh --assemble-iso
phase iso-ready
iso=$(find tmp -type f -name '*.iso' -print -quit)
test -n "${iso}" && test -s "${iso}"
sha=$(sha256 -q "${iso}")
size=$(stat -f %z "${iso}")
name="FreeSense-${PACKAGE_TRAIN}-g${GENERATION}-amd64.iso"
phase iso-publish
rclone copyto --immutable --checksum --retries 10 --low-level-retries 20 \
  "${iso}" "${RESULT}/${name}"
jq -n --arg fingerprint "${FINGERPRINT}" --arg sha256 "${sha}" --arg file "${name}" \
  --arg system "${SYSTEM_ID}" --arg platform "${PLATFORM_ID}" --arg source "${SOURCE_SHA}" \
  --arg freebsd "${FREEBSD_SHA}" --arg worker_image "${IMAGE_SHA256}" \
  --arg package_train "${PACKAGE_TRAIN}" --arg channel "${CHANNEL}" \
  --arg channel_payload "${CHANNEL_PAYLOAD_SHA256}" --argjson size "${size}" \
  --argjson generation "${GENERATION}" \
  '{schema_version:"freesense.iso/v1",fingerprint:$fingerprint,sha256:$sha256,size:$size,file:$file,system:$system,generation:$generation,inputs:{platform:$platform,source:$source,freebsd:$freebsd,worker_image:$worker_image,package_train:$package_train,channel:$channel,channel_payload:$channel_payload}}' \
  >/tmp/complete.json
rclone copyto --immutable --checksum --retries 10 --low-level-retries 20 \
  /tmp/complete.json "${RESULT}/complete.json"
phase iso-complete
