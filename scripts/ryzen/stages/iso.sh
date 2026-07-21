configure_source
fetch_repository system "${SYSTEM_ID}" /root/system-repo
cd /root/freesense-src
mkdir -p /root/freebsd-tools/release/amd64 /root/freebsd-tools/release/scripts
fetch -qo /root/freebsd-tools/release/amd64/mkisoimages.sh \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/amd64/mkisoimages.sh"
fetch -qo /root/freebsd-tools/release/scripts/make-manifest.sh \
  "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/release/scripts/make-manifest.sh"
export FREESENSE_ASSEMBLY_BASE_REPO=/root/system-repo
export FREESENSE_ASSEMBLY_SYSTEM_REPO=/root/system-repo
export FREESENSE_ASSEMBLY_FREEBSD_SRC=/root/freebsd-tools
./build.sh --assemble-iso
iso=$(find tmp -type f -name '*.iso' -print -quit)
test -n "${iso}" && test -s "${iso}"
sha=$(sha256 -q "${iso}")
name="FreeSense-${PACKAGE_TRAIN}-g${GENERATION}-amd64.iso"
rclone copyto --immutable "${iso}" "${RESULT}/${name}"
jq -n --arg fingerprint "${FINGERPRINT}" --arg sha256 "${sha}" --arg file "${name}" \
  --arg system "${SYSTEM_ID}" --argjson generation "${GENERATION}" \
  '{schema_version:"freesense.iso/v1",fingerprint:$fingerprint,sha256:$sha256,file:$file,system:$system,generation:$generation}' \
  >/tmp/complete.json
rclone copyto --immutable /tmp/complete.json "${RESULT}/complete.json"
