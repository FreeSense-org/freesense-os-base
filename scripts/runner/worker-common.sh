# Shared runtime for the three isolated FreeBSD build jobs. Every path is derived from
# immutable inputs; complete.json is committed last.

LAST_PHASE=initialization
phase() {
  LAST_PHASE=$1
  printf 'FreeSense phase: %s\n' "${LAST_PHASE}"
}
report_phase_failure() {
  status=$?
  if [ "${status}" -ne 0 ]; then
    printf 'FreeSense phase failed: %s status=%s\n' "${LAST_PHASE}" "${status}" >&2
  fi
}
trap report_phase_failure EXIT

decode() { printf '%s' "$1" | base64 -d; }
for name in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN R2_ENDPOINT R2_BUCKET \
  FREESENSE_REPO_SIGNING_KEY STAGE FINGERPRINT PLATFORM_ID SYSTEM_ID SOURCE_SHA \
  SYSTEM_SHA PACKAGES_SHA OS_BASE_SHA FREEBSD_SHA PORTS_SHA JAIL_OBJECT PACKAGE_TRAIN \
  GENERATION PUBLIC_BASE_URL; do
  eval "$name=\$(decode \"\${${name}_B64}\")"
done
unset AWS_ACCESS_KEY_ID_B64 AWS_SECRET_ACCESS_KEY_B64 AWS_SESSION_TOKEN_B64
unset FREESENSE_REPO_SIGNING_KEY_B64
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export HOME=/root PATH="/usr/local/sbin:/usr/local/bin:${PATH}"
export ASSUME_ALWAYS_YES=yes
PREFIX=v1
RESULT="R2:${R2_BUCKET}/${PREFIX}/artifacts/${STAGE}/${FINGERPRINT}"
if [ "${STAGE}" = packages ]; then
  RESULT="R2:${R2_BUCKET}/${PREFIX}/artifacts/packages/${PACKAGE_TRAIN}/${FINGERPRINT}"
fi

phase tools-bootstrap
env ASSUME_ALWAYS_YES=yes pkg bootstrap -f
phase tools-install
set +e
env ASSUME_ALWAYS_YES=yes pkg install -y \
  archivers/gtar archivers/zstd devel/git ftp/curl net/rclone \
  lang/python311 ports-mgmt/poudriere-devel security/openssl textproc/jq textproc/xmlstarlet
tool_install_status=$?
set -e
if [ "${tool_install_status}" -ne 0 ]; then
  echo "worker tool installation failed with pkg status ${tool_install_status}" >&2
  exit "${tool_install_status}"
fi
for tool in gtar zstd git curl rclone python3.11 poudriere openssl jq xml; do
  command -v "${tool}" >/dev/null || {
    echo "worker tool installation did not provide ${tool}" >&2
    exit 1
  }
done
phase tools-ready

phase storage-config
RCLONE_CONFIG=/root/.config/rclone/rclone.conf
export RCLONE_CONFIG
mkdir -p "$(dirname "${RCLONE_CONFIG}")" /root/work /root/sign
cat >"${RCLONE_CONFIG}" <<EOF
[R2]
type = s3
provider = Cloudflare
access_key_id = ${AWS_ACCESS_KEY_ID}
secret_access_key = ${AWS_SECRET_ACCESS_KEY}
session_token = ${AWS_SESSION_TOKEN}
endpoint = ${R2_ENDPOINT}
region = auto
acl = private
no_check_bucket = true
upload_cutoff = 5G
EOF
chmod 600 "${RCLONE_CONFIG}"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
phase storage-ready

clone_exact() {
  url=$1 destination=$2 commit=$3
  rm -rf "${destination}"
  git clone -q --filter=blob:none --no-checkout "${url}" "${destination}"
  git -C "${destination}" fetch -q --depth=1 origin "${commit}"
  git -C "${destination}" checkout -q --detach "${commit}"
  test "$(git -C "${destination}" rev-parse HEAD)" = "${commit}"
}

configure_source() {
  phase clone-source
  clone_exact https://github.com/FreeSense-org/freesense.git /root/freesense-src "${SOURCE_SHA}"
  phase clone-system-ports
  clone_exact https://github.com/FreeSense-org/freesense-system-ports.git /root/freesense-system-ports "${SYSTEM_SHA}"
  phase clone-os-definition
  clone_exact https://github.com/FreeSense-org/freesense-os-base.git /root/os-definition "${OS_BASE_SHA}"
  if [ "${STAGE}" = packages ]; then
    phase clone-optional-packages
    clone_exact https://github.com/FreeSense-org/freesense-packages.git /root/freesense-packages "${PACKAGES_SHA}"
  fi
  phase configure-source
  sed -i '' "s/^UPSTREAM_REF=.*/UPSTREAM_REF=\"${FREEBSD_SHA}\"/" /root/os-definition/manifest.env
  cd /root/freesense-src
  cp build.conf.sample build.conf
  printf '%s' "${FREESENSE_REPO_SIGNING_KEY}" >/root/sign/repo.key
  chmod 400 /root/sign/repo.key
  openssl pkey -in /root/sign/repo.key -pubout -out /root/sign/channel-public.pem >/dev/null 2>&1
  packages_url=""
  packages_fingerprint=""
  if [ "${STAGE}" = packages ]; then
    packages_url="${PUBLIC_BASE_URL}/artifacts/packages/${PACKAGE_TRAIN}/${FINGERPRINT}/amd64"
    packages_fingerprint="${FINGERPRINT}"
  fi
  cat >>build.conf <<EOF
export PRODUCT_NAME_SUFFIX=""
export POUDRIERE_BRANCH=main
export POUDRIERE_PORTS_GIT_URL="https://github.com/freebsd/freebsd-ports.git"
export POUDRIERE_PORTS_GIT_BRANCH="main"
export FREEBSD_SRC_PATCHES_DIR="/root/os-definition"
export FREESENSE_PORTS_COMMIT="${PORTS_SHA}"
export FREESENSE_PACKAGE_TRAIN="${PACKAGE_TRAIN}"
export PRODUCT_REVISION="${GENERATION}"
export FREESENSE_DIST_WORLD_ARCHIVE="/root/jail-base.txz"
export FREESENSE_SYSTEM_REPO_URL="${PUBLIC_BASE_URL}/artifacts/system/${SYSTEM_ID}/amd64"
export FREESENSE_SYSTEM_FINGERPRINT="${SYSTEM_ID}"
export FREESENSE_PACKAGES_REPO_URL="${packages_url}"
export FREESENSE_PACKAGES_FINGERPRINT="${packages_fingerprint}"
export FREESENSE_CHANNEL_PUBLIC_KEY_FILE="/root/sign/channel-public.pem"
export DO_NOT_SIGN_PKG_REPO=1
EOF
  phase source-ready
}

configure_poudriere() {
  config=/usr/local/etc/poudriere.conf
  mkdir -p "$(dirname "${config}")"
  touch "${config}"
  if ! grep -qx 'NOLINUX=yes' "${config}"; then
    printf '\n# FreeSense builds do not need Linux compatibility modules.\nNOLINUX=yes\n' >>"${config}"
  fi
  grep -qx 'NOLINUX=yes' "${config}"
}

create_jail() {
  phase poudriere-jail
  [ -s /root/jail-base.txz ] || fetch_input "${JAIL_OBJECT}" /root/jail-base.txz
  poudriere jail -c -j FreeSense_main_amd64 -a amd64.amd64 \
    -v 16.0-CURRENT -m tar=/root/jail-base.txz
  phase poudriere-jail-ready
}

fetch_input() {
  object=$1 destination=$2 expected=${1##*/}
  part="${destination}.part"
  rm -f "${part}"
  phase input-fetch
  set +e
  rclone copyto --error-on-no-transfer --retries 10 --low-level-retries 20 \
    "R2:${R2_BUCKET}/${PREFIX}/${object}" "${part}"
  status=$?
  set -e
  if [ "${status}" -ne 0 ]; then
    rm -f "${part}"
    echo "immutable input download failed with rclone status ${status}" >&2
    return "${status}"
  fi
  if [ ! -s "${part}" ]; then
    rm -f "${part}"
    echo "immutable input download produced an empty file" >&2
    return 1
  fi
  actual=$(sha256 -q "${part}")
  if [ "${actual}" != "${expected}" ]; then
    rm -f "${part}"
    echo "immutable input checksum mismatch" >&2
    return 1
  fi
  mv -f "${part}" "${destination}"
  phase input-ready
}

fetch_repository() {
  kind=$1 id=$2 destination=$3
  part="${destination}.part"
  rm -rf "${part}" "${destination}"
  mkdir -p "${part}"
  phase repository-fetch
  rclone copy --error-on-no-transfer --retries 10 --low-level-retries 20 \
    "R2:${R2_BUCKET}/${PREFIX}/artifacts/${kind}/${id}/amd64" "${part}"
  rclone copyto --error-on-no-transfer --retries 10 --low-level-retries 20 \
    "R2:${R2_BUCKET}/${PREFIX}/artifacts/${kind}/${id}/complete.json" \
    "${part}/complete.json"
  jq -e --arg fingerprint "${id}" '.fingerprint == $fingerprint' \
    "${part}/complete.json" >/dev/null
  mv "${part}" "${destination}"
  phase repository-ready
}

create_source_archive() {
  phase source-archive
  source_time=$(git -C /root/freesense-src show -s --format=%ct "${SOURCE_SHA}")
  case "${source_time}" in
    ''|*[!0-9]*) echo "source commit timestamp is invalid" >&2; return 1 ;;
  esac
  mkdir -p /usr/ports/distfiles
  rm -f /usr/ports/distfiles/freesense-src.tar.gz
  TZ=UTC gtar --sort=name --format=pax --mtime="@${source_time}" \
    --owner=0 --group=0 --numeric-owner \
    --pax-option=delete=atime,delete=ctime \
    --use-compress-program='/usr/bin/gzip -n' \
    -cf /usr/ports/distfiles/freesense-src.tar.gz -C /root \
    --exclude='freesense-src/.git' --exclude='freesense-src/tmp' \
    --exclude='freesense-src/logs' freesense-src
  phase source-archive-ready
}

sign_repository() {
  directory=$1
  phase repository-sign
  test -s /root/sign/repo.key
  fetch -qo /root/sign/sign.sh \
    https://raw.githubusercontent.com/freebsd/pkg/2678d2b6a8ca3cf80cb4dbc8da557a2998e1b5c0/scripts/sign.sh
  sed -i '' 's+ repo\.+ /root/sign/repo.+g' /root/sign/sign.sh
  chmod 700 /root/sign/sign.sh
  pkg repo "${directory}" signing_command: /root/sign/sign.sh /root/sign/repo.key
  phase repository-signed
}

publish_repository() {
  directory=$1
  phase repository-publish
  test -n "$(find "${directory}/All" -type f -name '*.pkg' -print -quit)"
  find "${directory}" -type f ! -name complete.json | while IFS= read -r file; do
    relative=${file#"${directory}/"}
    rclone copyto --immutable --retries 10 --low-level-retries 20 \
      "${file}" "${RESULT}/amd64/${relative}"
  done
  jq -n --arg stage "${STAGE}" --arg fingerprint "${FINGERPRINT}" \
    --arg platform "${PLATFORM_ID}" --arg system "${SYSTEM_ID}" \
    --arg source "${SOURCE_SHA}" --arg system_ports "${SYSTEM_SHA}" \
    --arg packages "${PACKAGES_SHA}" --arg freebsd "${FREEBSD_SHA}" \
    --arg ports "${PORTS_SHA}" --arg package_train "${PACKAGE_TRAIN}" \
    --argjson generation "${GENERATION}" \
    '{schema_version:"freesense.artifact/v1",stage:$stage,fingerprint:$fingerprint,generation:$generation,inputs:{platform:$platform,system:$system,source:$source,system_ports:$system_ports,freebsd:$freebsd,ports:$ports,package_train:$package_train}} | if $stage == "packages" then .inputs.packages = $packages else . end' \
    >"${directory}/complete.json"
  rclone copyto --immutable "${directory}/complete.json" "${RESULT}/complete.json"
  phase repository-complete
}
