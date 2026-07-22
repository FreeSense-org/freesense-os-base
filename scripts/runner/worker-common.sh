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
  IMAGE_SHA256 GENERATION PUBLIC_BASE_URL CHANNEL CHANNEL_PAYLOAD_SHA256 \
  CHANNEL_PAYLOAD_B64 CHANNEL_SIGNATURE_B64; do
  eval "$name=\$(decode \"\${${name}_B64}\")"
done
unset AWS_ACCESS_KEY_ID_B64 AWS_SECRET_ACCESS_KEY_B64 AWS_SESSION_TOKEN_B64
unset FREESENSE_REPO_SIGNING_KEY_B64
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export HOME=/root PATH="/usr/local/sbin:/usr/local/bin:${PATH}"
export ASSUME_ALWAYS_YES=yes LC_ALL=C LANG=C TZ=UTC
umask 022
case "${STAGE}" in system|packages|iso) : ;; *) echo "invalid build stage" >&2; exit 1 ;; esac
case "${CHANNEL}" in devel|stable) : ;; *) echo "invalid selected channel" >&2; exit 1 ;; esac
for value in "${FINGERPRINT}" "${PLATFORM_ID}" "${SYSTEM_ID}" "${IMAGE_SHA256}"; do
  case "${value}" in ''|*[!0-9a-f]*) echo "invalid SHA-256 build input" >&2; exit 1 ;; esac
  [ "${#value}" -eq 64 ] || { echo "invalid SHA-256 build input" >&2; exit 1; }
done
if [ "${STAGE}" = iso ]; then
  case "${CHANNEL_PAYLOAD_SHA256}" in ''|*[!0-9a-f]*)
    echo "ISO requires the exact signed channel payload" >&2; exit 1 ;;
  esac
  [ "${#CHANNEL_PAYLOAD_SHA256}" -eq 64 ] || {
    echo "ISO requires the exact signed channel payload" >&2; exit 1;
  }
  [ -n "${CHANNEL_PAYLOAD_B64}" ] && [ -n "${CHANNEL_SIGNATURE_B64}" ] || {
    echo "ISO requires the exact signed channel document" >&2; exit 1;
  }
fi
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

upload_immutable() {
  rclone copyto --immutable --checksum --multi-thread-streams 0 \
    --retries 10 --low-level-retries 20 "$1" "$2"
}

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

  FREESENSE_SOURCE_COMMIT_TIME=$(git show -s --format=%ct "${SOURCE_SHA}")
  case "${FREESENSE_SOURCE_COMMIT_TIME}" in
    ''|*[!0-9]*) echo "source commit timestamp is invalid" >&2; return 1 ;;
  esac
  DATESTRING=$(LC_ALL=C TZ=UTC date -r "${FREESENSE_SOURCE_COMMIT_TIME}" '+%Y%m%d-%H%M')
  BUILTDATESTRING=$(LC_ALL=C TZ=UTC date -r "${FREESENSE_SOURCE_COMMIT_TIME}" '+%a %b %d %T %Z %Y')
  SOURCE_DATE_EPOCH=${FREESENSE_SOURCE_COMMIT_TIME}
  FREESENSE_REQUIRE_SOURCE_DATE_EPOCH=1
  export FREESENSE_SOURCE_COMMIT_TIME SOURCE_DATE_EPOCH DATESTRING BUILTDATESTRING
  export FREESENSE_REQUIRE_SOURCE_DATE_EPOCH

  printf '%s' "${FREESENSE_REPO_SIGNING_KEY}" >/root/sign/repo.key
  chmod 400 /root/sign/repo.key
  openssl pkey -in /root/sign/repo.key -pubout -out /root/sign/repo.pub >/dev/null 2>&1
  chmod 444 /root/sign/repo.pub
  cp /root/sign/repo.pub /root/sign/channel-public.pem
  chmod 444 /root/sign/channel-public.pem
  trusted_fingerprint=$(sed -n \
    's/^[[:space:]]*fingerprint:[[:space:]]*"\([0-9a-fA-F]\{64\}\)"[[:space:]]*$/\1/p' \
    /root/freesense-src/src/usr/local/share/FreeSense/keys/pkg/trusted/freesense | \
    tr '[:upper:]' '[:lower:]')
  derived_fingerprint=$(sha256 -q /root/sign/repo.pub)
  if [ "${trusted_fingerprint}" != "${derived_fingerprint}" ]; then
    echo "trusted package fingerprint does not match the repository signing key" >&2
    return 1
  fi
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
export FREESENSE_MAKE_JOBS_NUMBER_LIMIT=4
export FREESENSE_USE_PACKAGE_FETCH=1
EOF
  phase source-ready
}

configure_poudriere() {
  config=/usr/local/etc/poudriere.conf
  mkdir -p "$(dirname "${config}")"
  temporary=${config}.tmp.$$
  rm -f "${temporary}"
  # Keep \${ABI} escaped in the file; Poudriere expands it after selecting a jail.
  cat >"${temporary}" <<'EOF'
NO_ZFS=yes
BASEFS=/usr/local/poudriere
POUDRIERE_DATA=/usr/local/poudriere/data
RESOLV_CONF=/etc/resolv.conf
DISTFILES_CACHE=/usr/ports/distfiles
PACKAGE_FETCH_URL=pkg+https://pkg.FreeBSD.org/\${ABI}
CHECK_CHANGED_OPTIONS=verbose
CHECK_CHANGED_DEPS=yes
PARALLEL_JOBS=3
PREPARE_PARALLEL_JOBS=3
ALLOW_MAKE_JOBS=yes
USE_TMPFS=wrkdir
TMPFS_LIMIT=4
ATOMIC_PACKAGE_REPOSITORY=yes
COMMIT_PACKAGES_ON_FAILURE=no
KEEP_OLD_PACKAGES=no
SAVE_WRKDIR=no
NOLINUX=yes
PKG_REPRODUCIBLE=yes
PRESERVE_TIMESTAMP=yes
BUILDER_HOSTNAME=freesense-builder
EOF
  chmod 644 "${temporary}"
  mv -f "${temporary}" "${config}"
  for setting in NO_ZFS=yes PARALLEL_JOBS=3 ALLOW_MAKE_JOBS=yes \
    USE_TMPFS=wrkdir TMPFS_LIMIT=4 PKG_REPRODUCIBLE=yes \
    PRESERVE_TIMESTAMP=yes BUILDER_HOSTNAME=freesense-builder; do
    grep -qx "${setting}" "${config}"
  done
}

package_metadata() {
  package=$1
  metadata=$(pkg query -F "${package}" '%n|%v|%o|%q|%Q') || return 1
  [ "$(printf '%s\n' "${metadata}" | awk 'END { print NR }')" -eq 1 ] || {
    echo "package has ambiguous metadata: ${package}" >&2
    return 1
  }
  [ "$(printf '%s' "${metadata}" | awk -F '|' '{ print NF }')" -eq 5 ] || {
    echo "package has incomplete metadata: ${package}" >&2
    return 1
  }
  printf '%s\n' "${metadata}"
}

inventory_package() {
  package=$1 inventory=$2
  metadata=$(package_metadata "${package}") || return 1
  name=${metadata%%|*}
  filename=$(basename "${package}")
  sha=$(sha256 -q "${package}") || return 1
  if awk -F '|' -v name="${name}" -v filename="${filename}" \
    '$1 == name || $6 == filename { found=1 } END { exit !found }' "${inventory}"; then
    echo "sealed repository contains a duplicate package name or filename: ${name}" >&2
    return 1
  fi
  printf '%s|%s|%s|%s\n' "${metadata}" "${filename}" "${sha}" "${package}" >>"${inventory}"
}

merge_package() {
  package=$1 destination=$2 inventory=$3 duplicate_policy=$4
  metadata=$(package_metadata "${package}") || return 1
  name=${metadata%%|*}
  filename=$(basename "${package}")
  sha=$(sha256 -q "${package}") || return 1
  existing=$(awk -F '|' -v name="${name}" -v filename="${filename}" \
    '$1 == name || $6 == filename { print; exit }' "${inventory}")
  if [ -n "${existing}" ]; then
    existing_without_path=${existing%|*}
    if [ "${duplicate_policy}" = identical ] && \
      [ "${existing_without_path}" = "${metadata}|${filename}|${sha}" ]; then
      echo "Reusing identical package already supplied by System: ${filename}"
      return 0
    fi
    echo "conflicting package name or filename while composing repository: ${name}" >&2
    return 1
  fi
  target="${destination}/${filename}"
  [ ! -e "${target}" ] || {
    echo "package destination already exists: ${target}" >&2
    return 1
  }
  temporary="${target}.part.$$"
  rm -f "${temporary}"
  cp "${package}" "${temporary}" || { rm -f "${temporary}"; return 1; }
  [ "$(sha256 -q "${temporary}")" = "${sha}" ] || {
    rm -f "${temporary}"
    echo "package copy checksum mismatch: ${filename}" >&2
    return 1
  }
  mv "${temporary}" "${target}"
  printf '%s|%s|%s|%s\n' "${metadata}" "${filename}" "${sha}" "${target}" >>"${inventory}"
}

seed_poudriere_repository() {
  source_repository=$1
  repository=/usr/local/poudriere/data/packages/FreeSense_main_amd64-FreeSense_main
  jail_version_file=/usr/local/etc/poudriere.d/jails/FreeSense_main_amd64/version
  staging=${repository}.part.$$
  seed_inventory=/tmp/system-seed-inventory.$$
  package_count=0
  pkg_count=0
  pkg_filename=

  [ -d "${source_repository}/All" ] || {
    echo "System repository has no package directory" >&2
    return 1
  }
  [ -s "${jail_version_file}" ] || {
    echo "Poudriere jail has no version marker" >&2
    return 1
  }
  [ "$(awk 'END { print NR }' "${jail_version_file}")" -eq 1 ] || {
    echo "Poudriere jail version marker is not one line" >&2
    return 1
  }

  rm -rf "${repository}" "${staging}"
  mkdir -p "${staging}/All" "${staging}/Latest"
  : >"${seed_inventory}"
  for package in "${source_repository}"/All/*.pkg; do
    [ -f "${package}" ] || continue
    metadata=$(package_metadata "${package}") || return 1
    name=${metadata%%|*}
    merge_package "${package}" "${staging}/All" "${seed_inventory}" reject || return 1
    package_count=$((package_count + 1))
    if [ "${name}" = pkg ]; then
      pkg_count=$((pkg_count + 1))
      pkg_filename=$(basename "${package}")
    fi
  done
  [ "${package_count}" -gt 0 ] || {
    echo "System repository is empty" >&2
    return 1
  }
  [ "${pkg_count}" -eq 1 ] || {
    echo "System repository must contain exactly one pkg bootstrap package" >&2
    return 1
  }

  ln -s "../All/${pkg_filename}" "${staging}/Latest/pkg.pkg"
  [ "$(realpath "${staging}/Latest/pkg.pkg")" = "${staging}/All/${pkg_filename}" ] || {
    echo "Poudriere pkg bootstrap link is invalid" >&2
    return 1
  }
  cp "${jail_version_file}" "${staging}/.jailversion"
  pkg repo "${staging}"
  [ -s "${staging}/meta.conf" ] || {
    echo "Poudriere seed catalog was not generated" >&2
    return 1
  }

  mv "${staging}" "${repository}"
  [ -f "${repository}/Latest/pkg.pkg" ] || return 1
  rm -f "${seed_inventory}"
}

poudriere_latest_repository() {
  repository=/usr/local/poudriere/data/packages/FreeSense_main_amd64-FreeSense_main
  latest=${repository}/.latest
  [ -L "${latest}" ] || { echo "Poudriere repository has no atomic .latest link" >&2; return 1; }
  resolved=$(realpath "${latest}") || return 1
  case "${resolved}" in
    "${repository}"/.real_*) : ;;
    *) echo "Poudriere .latest escapes its expected repository" >&2; return 1 ;;
  esac
  [ -d "${resolved}/All" ] || { echo "Poudriere repository has no package directory" >&2; return 1; }
  printf '%s\n' "${resolved}"
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

verify_repository() (
  set -eu
  set -o pipefail
  repository=$1
  archive=${repository}/packagesite.pkg
  work=$(mktemp -d /tmp/freesense-repository.XXXXXX)
  trap 'rm -rf "${work}"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  [ -s "${archive}" ] || {
    echo "repository has no signed package catalog" >&2
    return 1
  }
  [ -s /root/sign/repo.pub ] || {
    echo "trusted repository public key is missing" >&2
    return 1
  }
  tar -xpf "${archive}" -C "${work}" packagesite.yaml packagesite.yaml.sig
  for file in packagesite.yaml packagesite.yaml.sig; do
    [ -f "${work}/${file}" ] && [ ! -L "${work}/${file}" ] || {
      echo "signed repository catalog member is invalid: ${file}" >&2
      return 1
    }
  done
  digest=$(sha256 -q "${work}/packagesite.yaml")
  printf '%s' "${digest}" | openssl dgst -sha256 -verify /root/sign/repo.pub \
    -signature "${work}/packagesite.yaml.sig" >/dev/null

  jq -Rr '
    select(length > 0) | fromjson |
    if ((.repopath | type) == "string" and
        (.repopath | test("^All/[^/]+[.]pkg$")) and
        ((.repopath | test("[\\t\\r\\n]")) | not) and
        (.sum | type) == "string" and
        (.sum | test("^[0-9a-f]{64}$")))
    then [.repopath, .sum] | @tsv
    else error("invalid signed package catalog record")
    end
  ' "${work}/packagesite.yaml" | LC_ALL=C sort >"${work}/expected"
  [ -s "${work}/expected" ] || {
    echo "signed package catalog is empty" >&2
    return 1
  }

  : >"${work}/actual"
  for package in "${repository}"/All/*.pkg; do
    [ -f "${package}" ] || continue
    printf 'All/%s\t%s\n' "${package##*/}" "$(sha256 -q "${package}")" \
      >>"${work}/actual"
  done
  LC_ALL=C sort -o "${work}/actual" "${work}/actual"
  cmp -s "${work}/expected" "${work}/actual" || {
    echo "repository packages do not match the signed catalog" >&2
    return 1
  }
)

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
  phase repository-verify
  verify_repository "${part}"
  phase repository-verified
  mv "${part}" "${destination}"
  phase repository-ready
}

create_source_archive() {
  phase source-archive
  source_time=${FREESENSE_SOURCE_COMMIT_TIME:-}
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
    upload_immutable "${file}" "${RESULT}/amd64/${relative}"
  done
  jq -n --arg stage "${STAGE}" --arg fingerprint "${FINGERPRINT}" \
    --arg platform "${PLATFORM_ID}" --arg system "${SYSTEM_ID}" \
    --arg source "${SOURCE_SHA}" --arg system_ports "${SYSTEM_SHA}" \
    --arg packages "${PACKAGES_SHA}" --arg freebsd "${FREEBSD_SHA}" \
    --arg ports "${PORTS_SHA}" --arg package_train "${PACKAGE_TRAIN}" \
    --arg os_definition "${OS_BASE_SHA}" --arg worker_image "${IMAGE_SHA256}" \
    --arg jail_object "${JAIL_OBJECT}" --arg signing_public_key "${derived_fingerprint}" \
    --argjson generation "${GENERATION}" \
    '{schema_version:"freesense.artifact/v1",stage:$stage,fingerprint:$fingerprint,generation:$generation,inputs:{platform:$platform,system:$system,source:$source,system_ports:$system_ports,freebsd:$freebsd,ports:$ports,package_train:$package_train,os_definition:$os_definition,worker_image:$worker_image,jail_object:$jail_object,signing_public_key:$signing_public_key}} | if $stage == "packages" then .inputs.packages = $packages else . end' \
    >"${directory}/complete.json"
  upload_immutable "${directory}/complete.json" "${RESULT}/complete.json"
  phase repository-complete
}
