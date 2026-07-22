# Shared runtime for the three isolated FreeBSD build jobs. Every path is derived from
# immutable inputs; complete.json is committed last.

LAST_PHASE=initialization
POUDRIERE_RETRY_SOURCE=
POUDRIERE_RETRY_BASE=
phase() {
  LAST_PHASE=$1
  printf 'FreeSense phase: %s\n' "${LAST_PHASE}"
}
report_phase_failure() {
  status=$?
  trap - EXIT
  if [ "${status}" -ne 0 ]; then
    printf 'FreeSense phase failed: %s status=%s\n' "${LAST_PHASE}" "${status}" >&2
    if [ -n "${POUDRIERE_RETRY_SOURCE}" ]; then
      set +e
      save_poudriere_retry_cache "${POUDRIERE_RETRY_SOURCE}" \
        "${POUDRIERE_RETRY_BASE}"
      retry_status=$?
      set -e
      if [ "${retry_status}" -ne 0 ]; then
        echo "Unable to save the verified retry cache; preserving the build failure." >&2
      fi
    fi
  fi
  exit "${status}"
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

package_metadata() (
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
)

inventory_package() (
  package=$1 inventory=$2
  metadata=$(package_metadata "${package}") || return 1
  name=${metadata%%|*}
  filename=$(basename "${package}")
  sha=$(sha256 -q "${package}") || return 1
  existing=$(awk -F '|' -v name="${name}" -v filename="${filename}" \
    '$1 == name || $6 == filename { print; exit }' "${inventory}") || return 1
  if [ -n "${existing}" ]; then
    echo "sealed repository contains a duplicate package name or filename: ${name}" >&2
    return 1
  fi
  printf '%s|%s|%s|%s\n' "${metadata}" "${filename}" "${sha}" "${package}" \
    >>"${inventory}" || return 1
)

merge_package() (
  package=$1 destination=$2 inventory=$3 duplicate_policy=$4
  metadata=$(package_metadata "${package}") || return 1
  name=${metadata%%|*}
  filename=$(basename "${package}")
  sha=$(sha256 -q "${package}") || return 1
  existing=$(awk -F '|' -v name="${name}" -v filename="${filename}" \
    '$1 == name || $6 == filename { print; exit }' "${inventory}") || return 1
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
  mv "${temporary}" "${target}" || { rm -f "${temporary}"; return 1; }
  printf '%s|%s|%s|%s\n' "${metadata}" "${filename}" "${sha}" "${target}" \
    >>"${inventory}" || return 1
)

seed_poudriere_repository() (
  set -eu
  primary_repository=$1
  retry_repository=${2:-}
  repository=/usr/local/poudriere/data/packages/FreeSense_main_amd64-FreeSense_main
  jail_version_file=/usr/local/etc/poudriere.d/jails/FreeSense_main_amd64/version
  staging=${repository}.part.$$
  seed_inventory=/tmp/system-seed-inventory.$$

  cleanup_seed() {
    seed_status=$?
    trap - EXIT HUP INT TERM
    rm -rf "${staging}" || true
    rm -f "${seed_inventory}" || true
    exit "${seed_status}"
  }
  trap cleanup_seed EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  [ -d "${primary_repository}/All" ] || {
    echo "Poudriere seed has no primary package directory" >&2
    return 1
  }
  if [ -n "${retry_repository}" ]; then
    [ -d "${retry_repository}/All" ] || {
      echo "Poudriere retry seed has no package directory" >&2
      return 1
    }
  fi
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
  for package in "${primary_repository}"/All/*.pkg; do
    [ -f "${package}" ] || continue
    merge_package "${package}" "${staging}/All" "${seed_inventory}" reject || return 1
  done
  if [ -n "${retry_repository}" ]; then
    for package in "${retry_repository}"/All/*.pkg; do
      [ -f "${package}" ] || continue
      merge_package "${package}" "${staging}/All" "${seed_inventory}" identical || return 1
    done
  fi
  package_count=$(awk 'END { print NR + 0 }' "${seed_inventory}")
  pkg_count=$(awk -F '|' '$1 == "pkg" { count++ } END { print count + 0 }' \
    "${seed_inventory}")
  pkg_filename=$(awk -F '|' '$1 == "pkg" { print $6; exit }' "${seed_inventory}")
  [ "${package_count}" -gt 0 ] || {
    echo "Poudriere seed is empty" >&2
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
)

poudriere_latest_repository() {
  repository=/usr/local/poudriere/data/packages/FreeSense_main_amd64-FreeSense_main
  latest=${repository}/.latest
  [ -L "${latest}" ] || { echo "Poudriere repository has no atomic .latest link" >&2; return 1; }
  resolved=$(realpath "${latest}") || return 1
  [ "$(dirname "${resolved}")" = "${repository}" ] || {
    echo "Poudriere .latest escapes its expected repository" >&2
    return 1
  }
  case "$(basename "${resolved}")" in
    .real_*) : ;;
    *) echo "Poudriere .latest is not an atomic repository" >&2; return 1 ;;
  esac
  [ -d "${resolved}/All" ] && [ ! -L "${resolved}/All" ] || {
    echo "Poudriere repository has no regular package directory" >&2
    return 1
  }
  printf '%s\n' "${resolved}"
}

poudriere_building_repository() {
  repository=/usr/local/poudriere/data/packages/FreeSense_main_amd64-FreeSense_main
  building=${repository}/.building
  [ -d "${building}" ] && [ ! -L "${building}" ] || return 1
  resolved=$(realpath "${building}") || return 1
  [ "${resolved}" = "${building}" ] || {
    echo "Poudriere .building escapes its expected repository" >&2
    return 1
  }
  [ -d "${resolved}/All" ] && [ ! -L "${resolved}/All" ] || return 1
  printf '%s\n' "${resolved}"
}

show_poudriere_errors() {
  shown=0
  for directory in /usr/local/poudriere/data/logs/bulk/*/latest/logs/errors; do
    [ -d "${directory}" ] || continue
    for logfile in "${directory}"/*.log; do
      [ -f "${logfile}" ] || continue
      printf '\n===== Poudriere error: %s =====\n' "${logfile##*/}" >&2
      tail -n 80 "${logfile}" >&2 || true
      shown=$((shown + 1))
      [ "${shown}" -lt 5 ] || return 0
    done
  done
}

run_poudriere_build() {
  base_repository=${1:-}
  set +e
  env NOLINUX=yes ./build.sh --update-pkg-repo
  build_status=$?
  set -e
  if [ "${build_status}" -ne 0 ]; then
    show_poudriere_errors
    if building=$(poudriere_building_repository); then
      POUDRIERE_RETRY_SOURCE=${building}
      POUDRIERE_RETRY_BASE=${base_repository}
    elif latest=$(poudriere_latest_repository); then
      POUDRIERE_RETRY_SOURCE=${latest}
      POUDRIERE_RETRY_BASE=${base_repository}
    fi
    return "${build_status}"
  fi
  POUDRIERE_RETRY_SOURCE=$(poudriere_latest_repository) || return 1
  POUDRIERE_RETRY_BASE=${base_repository}
}

seed_poudriere_with_retry() (
  set -eu
  retry_repository=$1
  primary_repository=${2:-}

  rm -rf "${retry_repository}"
  set +e
  if [ -n "${primary_repository}" ]; then
    restore_poudriere_retry_cache "${retry_repository}" "${primary_repository}"
  else
    restore_poudriere_retry_cache "${retry_repository}"
  fi
  retry_status=$?
  set -e

  case "${retry_status}" in 129|130|143) exit "${retry_status}" ;; esac
  if [ "${retry_status}" -eq 0 ]; then
    if [ -n "${primary_repository}" ]; then
      seed_poudriere_repository "${primary_repository}" "${retry_repository}"
    else
      seed_poudriere_repository "${retry_repository}"
    fi
  elif [ -n "${primary_repository}" ]; then
    echo "No verified exact-fingerprint package retry is available; using System only."
    seed_poudriere_repository "${primary_repository}"
  else
    echo "No verified exact-fingerprint package retry is available; building clean."
  fi
)

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

verified_catalog_inventory() (
  set -eu
  set -o pipefail
  archive=$1
  output=$2
  work=$(mktemp -d /tmp/freesense-repository.XXXXXX)
  temporary=${output}.part.$$
  cleanup_catalog() {
    catalog_status=$?
    trap - EXIT HUP INT TERM
    rm -rf "${work}" || true
    rm -f "${temporary}" || true
    exit "${catalog_status}"
  }
  trap cleanup_catalog EXIT
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
    if ((.name | type) == "string" and
        (.name | test("^[A-Za-z0-9][A-Za-z0-9+_.-]*$")) and
        (.repopath | type) == "string" and
        (.repopath | test("^All/[A-Za-z0-9][A-Za-z0-9+_.-]*[.]pkg$")) and
        (.sum | type) == "string" and
        (.sum | test("^[0-9a-f]{64}$")))
    then [.name, .repopath, .sum] | @tsv
    else error("invalid signed package catalog record")
    end
  ' "${work}/packagesite.yaml" | LC_ALL=C sort >"${temporary}"
  [ -s "${temporary}" ] || {
    echo "signed package catalog is empty" >&2
    return 1
  }
  tab=$(printf '\t')
  awk -F "${tab}" '
    NF != 3 || seen_name[$1]++ || seen_path[$2]++ { invalid=1 }
    END { exit invalid }
  ' "${temporary}" || {
    echo "signed package catalog has duplicate or malformed records" >&2
    return 1
  }
  mv -f "${temporary}" "${output}"
)

verify_repository() (
  set -eu
  repository=$1
  work=$(mktemp -d /tmp/freesense-repository.XXXXXX)
  trap 'rm -rf "${work}"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  verified_catalog_inventory "${repository}/packagesite.pkg" "${work}/expected"

  : >"${work}/actual"
  for package in "${repository}"/All/*.pkg; do
    [ -f "${package}" ] || continue
    [ ! -L "${package}" ] || {
      echo "repository package is a symbolic link: ${package##*/}" >&2
      return 1
    }
    metadata=$(package_metadata "${package}") || return 1
    name=${metadata%%|*}
    printf '%s\tAll/%s\t%s\n' "${name}" "${package##*/}" \
      "$(sha256 -q "${package}")" >>"${work}/actual"
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

make_signed_repository() {
  directory=$1
  test -s /root/sign/repo.key
  if [ ! -x /root/sign/sign.sh ]; then
    fetch -qo /root/sign/sign.sh \
      https://raw.githubusercontent.com/freebsd/pkg/2678d2b6a8ca3cf80cb4dbc8da557a2998e1b5c0/scripts/sign.sh
    sed -i '' 's+ repo\.+ /root/sign/repo.+g' /root/sign/sign.sh
    chmod 700 /root/sign/sign.sh
  fi
  pkg repo "${directory}" signing_command: /root/sign/sign.sh /root/sign/repo.key
}

sign_repository() {
  directory=$1
  phase repository-sign
  make_signed_repository "${directory}"
  phase repository-signed
}

retry_download() (
  source=$1 destination=$2
  temporary=${destination}.part.$$
  rm -f "${temporary}"
  if ! rclone copyto --error-on-no-transfer --retries 10 --low-level-retries 20 \
    "${source}" "${temporary}"; then
    rm -f "${temporary}"
    return 1
  fi
  [ -s "${temporary}" ] || { rm -f "${temporary}"; return 1; }
  mv -f "${temporary}" "${destination}"
)

save_poudriere_retry_cache() (
  set -eu
  source_repository=$1
  base_repository=${2:-}
  work=$(mktemp -d /tmp/freesense-retry-save.XXXXXX)
  repository=${work}/repository
  inventory=${work}/inventory
  retry_root=${RESULT}/_retry/v1
  cleanup_retry_save() {
    retry_status=$?
    trap - EXIT HUP INT TERM
    rm -rf "${work}" || true
    exit "${retry_status}"
  }
  trap cleanup_retry_save EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  poudriere_repository=/usr/local/poudriere/data/packages/FreeSense_main_amd64-FreeSense_main
  resolved_source=$(realpath "${source_repository}") || return 1
  [ "${resolved_source}" = "${source_repository}" ] || return 1
  [ "$(dirname "${resolved_source}")" = "${poudriere_repository}" ] || return 1
  case "$(basename "${resolved_source}")" in
    .building|.real_*) : ;;
    *) return 1 ;;
  esac
  [ -d "${resolved_source}/All" ] && [ ! -L "${resolved_source}" ] && \
    [ ! -L "${resolved_source}/All" ] || return 1
  mkdir -p "${repository}/All"
  : >"${inventory}"
  if [ -n "${base_repository}" ]; then
    [ -d "${base_repository}/All" ] || return 1
    for package in "${base_repository}"/All/*.pkg; do
      [ -e "${package}" ] || continue
      [ -f "${package}" ] && [ ! -L "${package}" ] || return 1
      inventory_package "${package}" "${inventory}" || return 1
    done
  fi
  for package in "${resolved_source}/All"/*.pkg; do
    [ -e "${package}" ] || continue
    [ -f "${package}" ] && [ ! -L "${package}" ] || {
      echo "retry cache rejected a non-regular package" >&2
      return 1
    }
    merge_package "${package}" "${repository}/All" "${inventory}" identical || return 1
  done
  package_count=$(find "${repository}/All" -type f -name '*.pkg' | awk 'END { print NR + 0 }')
  [ "${package_count}" -gt 0 ] || return 1
  if [ -z "${base_repository}" ]; then
    pkg_count=$(awk -F '|' '$1 == "pkg" { count++ } END { print count + 0 }' \
      "${inventory}")
    [ "${pkg_count}" -eq 1 ] || return 1
  fi

  make_signed_repository "${repository}"
  verify_repository "${repository}"
  catalog_sha=$(sha256 -q "${repository}/packagesite.pkg")
  snapshot=${work}/snapshot.json
  signature=${work}/snapshot.sig
  jq -cnS --arg stage "${STAGE}" --arg fingerprint "${FINGERPRINT}" \
    --arg system "${SYSTEM_ID}" --arg package_train "${PACKAGE_TRAIN}" \
    --arg catalog "${catalog_sha}" --argjson generation "${GENERATION}" \
    '{schema_version:"freesense.retry/v1",stage:$stage,fingerprint:$fingerprint,
      generation:$generation,system_fingerprint:$system,package_train:$package_train,
      catalog_sha256:$catalog}' >"${snapshot}"
  openssl dgst -sha256 -sign /root/sign/repo.key -out "${signature}" "${snapshot}"

  for package in "${repository}/All"/*.pkg; do
    package_sha=$(sha256 -q "${package}")
    upload_immutable "${package}" "${retry_root}/objects/sha256/${package_sha}"
  done
  snapshot_root=${retry_root}/snapshots/${catalog_sha}
  upload_immutable "${repository}/packagesite.pkg" "${snapshot_root}/packagesite.pkg"
  upload_immutable "${snapshot}" "${snapshot_root}/snapshot.json"
  upload_immutable "${signature}" "${snapshot_root}/snapshot.sig"
  echo "Saved ${package_count} verified package(s) for an exact-fingerprint retry."
)

restore_poudriere_retry_cache() (
  set -eu
  destination=$1
  base_repository=${2:-}
  retry_root=${RESULT}/_retry/v1
  work=$(mktemp -d /tmp/freesense-retry-restore.XXXXXX)
  staging=${destination}.part.$$
  combined=${work}/combined
  composition=${work}/composition
  cleanup_retry_restore() {
    retry_status=$?
    trap - EXIT HUP INT TERM
    rm -rf "${work}" "${staging}" || true
    exit "${retry_status}"
  }
  trap cleanup_retry_restore EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  [ ! -e "${destination}" ] || return 1
  if [ -n "${base_repository}" ]; then
    [ -d "${base_repository}/All" ] || return 1
  fi
  mkdir -p "${staging}/All" "${work}/objects" "${work}/packages"
  : >"${combined}"
  : >"${composition}"
  if ! rclone lsf --recursive --files-only "${retry_root}/snapshots" \
    >"${work}/listed" 2>/dev/null; then
    return 1
  fi
  awk -F / '$2 == "snapshot.sig" && NF == 2 && length($1) == 64 &&
    $1 !~ /[^0-9a-f]/ { print }' "${work}/listed" | LC_ALL=C sort \
    >"${work}/markers"
  [ -s "${work}/markers" ] || return 1

  tab=$(printf '\t')
  while IFS= read -r marker; do
    catalog_sha=${marker%/snapshot.sig}
    snapshot_directory=${work}/snapshots/${catalog_sha}
    mkdir -p "${snapshot_directory}"
    for file in packagesite.pkg snapshot.json snapshot.sig; do
      retry_download "${retry_root}/snapshots/${catalog_sha}/${file}" \
        "${snapshot_directory}/${file}" || return 1
    done
    openssl dgst -sha256 -verify /root/sign/repo.pub \
      -signature "${snapshot_directory}/snapshot.sig" \
      "${snapshot_directory}/snapshot.json" >/dev/null || return 1
    jq -e --arg stage "${STAGE}" --arg fingerprint "${FINGERPRINT}" \
      --arg generation "${GENERATION}" --arg system "${SYSTEM_ID}" \
      --arg package_train "${PACKAGE_TRAIN}" --arg catalog "${catalog_sha}" '
      type == "object" and .schema_version == "freesense.retry/v1" and
      .stage == $stage and .fingerprint == $fingerprint and
      (.generation | tostring) == $generation and
      .system_fingerprint == $system and .package_train == $package_train and
      .catalog_sha256 == $catalog
    ' "${snapshot_directory}/snapshot.json" >/dev/null || return 1
    [ "$(sha256 -q "${snapshot_directory}/packagesite.pkg")" = "${catalog_sha}" ] || \
      return 1
    verified_catalog_inventory "${snapshot_directory}/packagesite.pkg" \
      "${snapshot_directory}/inventory"

    while IFS="${tab}" read -r name repopath package_sha; do
      record=$(printf '%s\t%s\t%s' "${name}" "${repopath}" "${package_sha}")
      existing=$(awk -F "${tab}" -v name="${name}" -v path="${repopath}" \
        '$1 == name || $2 == path { print; exit }' "${combined}")
      if [ -n "${existing}" ]; then
        [ "${existing}" = "${record}" ] || {
          echo "retry cache contains conflicting signed package snapshots" >&2
          return 1
        }
        continue
      fi
      printf '%s\n' "${record}" >>"${combined}"
    done <"${snapshot_directory}/inventory"
  done <"${work}/markers"
  [ -s "${combined}" ] || return 1

  if [ -n "${base_repository}" ]; then
    for package in "${base_repository}"/All/*.pkg; do
      [ -e "${package}" ] || continue
      [ -f "${package}" ] && [ ! -L "${package}" ] || return 1
      inventory_package "${package}" "${composition}" || return 1
    done
  fi
  LC_ALL=C sort -o "${combined}" "${combined}"
  while IFS="${tab}" read -r name repopath package_sha; do
    filename=${repopath#All/}
    object=${work}/objects/${package_sha}
    if [ ! -f "${object}" ]; then
      retry_download "${retry_root}/objects/sha256/${package_sha}" "${object}" || return 1
      [ "$(sha256 -q "${object}")" = "${package_sha}" ] || return 1
    fi
    package=${work}/packages/${filename}
    cp "${object}" "${package}"
    metadata=$(package_metadata "${package}") || return 1
    [ "${metadata%%|*}" = "${name}" ] || return 1
    merge_package "${package}" "${staging}/All" "${composition}" identical || return 1
  done <"${combined}"

  restored_count=$(find "${staging}/All" -type f -name '*.pkg' | awk 'END { print NR + 0 }')
  [ "${restored_count}" -gt 0 ] || return 1
  if [ -z "${base_repository}" ]; then
    pkg_count=$(awk -F '|' '$1 == "pkg" { count++ } END { print count + 0 }' \
      "${composition}")
    [ "${pkg_count}" -eq 1 ] || return 1
  fi
  mv "${staging}" "${destination}"
  echo "Restored ${restored_count} verified package(s) from failed exact-fingerprint runs."
)

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
