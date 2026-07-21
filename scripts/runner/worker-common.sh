# Shared runtime for the three isolated FreeBSD build jobs. Every path is derived from
# immutable inputs; complete.json is committed last.

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
[ "${STAGE}" = packages ] \
  && RESULT="R2:${R2_BUCKET}/${PREFIX}/artifacts/packages/${PACKAGE_TRAIN}/${FINGERPRINT}"

env ASSUME_ALWAYS_YES=yes pkg bootstrap -f
env ASSUME_ALWAYS_YES=yes pkg install -y \
  archivers/gtar archivers/zstd devel/git ftp/curl net/rclone \
  lang/python311 ports-mgmt/poudriere-devel security/openssl textproc/jq textproc/xmlstarlet
hash -r

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

if rclone cat "${RESULT}/complete.json" >/tmp/complete 2>/dev/null; then
  jq -e --arg fingerprint "${FINGERPRINT}" '.fingerprint == $fingerprint' /tmp/complete >/dev/null
  echo "Reusing complete ${STAGE} artifact ${FINGERPRINT}"
  exit 0
fi

clone_exact() {
  url=$1 destination=$2 commit=$3
  rm -rf "${destination}"
  git clone -q --filter=blob:none --no-checkout "${url}" "${destination}"
  git -C "${destination}" fetch -q --depth=1 origin "${commit}"
  git -C "${destination}" checkout -q --detach "${commit}"
  test "$(git -C "${destination}" rev-parse HEAD)" = "${commit}"
}

configure_source() {
  clone_exact https://github.com/FreeSense-org/freesense.git /root/freesense-src "${SOURCE_SHA}"
  clone_exact https://github.com/FreeSense-org/freesense-system-ports.git /root/freesense-system-ports "${SYSTEM_SHA}"
  clone_exact https://github.com/FreeSense-org/freesense-os-base.git /root/os-definition "${OS_BASE_SHA}"
  if [ "${STAGE}" = packages ]; then
    clone_exact https://github.com/FreeSense-org/freesense-packages.git /root/freesense-packages "${PACKAGES_SHA}"
  fi
  sed -i '' "s/^UPSTREAM_REF=.*/UPSTREAM_REF=\"${FREEBSD_SHA}\"/" /root/os-definition/manifest.env
  cd /root/freesense-src
  cp build.conf.sample build.conf
  printf '%s' "${FREESENSE_REPO_SIGNING_KEY}" >/root/sign/repo.key
  chmod 400 /root/sign/repo.key
  openssl rsa -in /root/sign/repo.key -pubout -out /root/sign/channel-public.pem >/dev/null 2>&1
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
EOF
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
  [ -s /root/jail-base.txz ] || fetch_input "${JAIL_OBJECT}" /root/jail-base.txz
  poudriere jail -c -j FreeSense_main_amd64 -a amd64.amd64 \
    -v 16.0-CURRENT -m tar=/root/jail-base.txz
}

fetch_input() {
  object=$1 destination=$2 expected=${1##*/}
  rclone copyto "R2:${R2_BUCKET}/${PREFIX}/${object}" "${destination}"
  test "$(sha256 -q "${destination}")" = "${expected}"
}

fetch_repository() {
  kind=$1 id=$2 destination=$3
  mkdir -p "${destination}"
  rclone copy "R2:${R2_BUCKET}/${PREFIX}/artifacts/${kind}/${id}/amd64" "${destination}"
  rclone copyto "R2:${R2_BUCKET}/${PREFIX}/artifacts/${kind}/${id}/complete.json" \
    "${destination}/complete.json"
  jq -e --arg fingerprint "${id}" '.fingerprint == $fingerprint' \
    "${destination}/complete.json" >/dev/null
}

sign_repository() {
  directory=$1
  test -s /root/sign/repo.key
  fetch -qo /root/sign/sign.sh \
    https://raw.githubusercontent.com/freebsd/pkg/2678d2b6a8ca3cf80cb4dbc8da557a2998e1b5c0/scripts/sign.sh
  sed -i '' 's+ repo\.+ /root/sign/repo.+g' /root/sign/sign.sh
  chmod 700 /root/sign/sign.sh
  pkg repo "${directory}" signing_command: /root/sign/sign.sh /root/sign/repo.key
}

publish_repository() {
  directory=$1
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
    --arg ports "${PORTS_SHA}" --argjson generation "${GENERATION}" \
    '{schema_version:"freesense.artifact/v1",stage:$stage,fingerprint:$fingerprint,generation:$generation,inputs:{platform:$platform,system:$system,source:$source,system_ports:$system_ports,packages:$packages,freebsd:$freebsd,ports:$ports}}' \
    >"${directory}/complete.json"
  rclone copyto --immutable "${directory}/complete.json" "${RESULT}/complete.json"
}
