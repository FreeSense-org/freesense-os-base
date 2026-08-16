# Shared runtime for the isolated FreeBSD build jobs. Every path is derived from
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
  SYSTEM_SHA PACKAGES_SHA PACKAGES_ID OS_BASE_SHA FREEBSD_SHA PORTS_SHA JAIL_OBJECT FREEBSD_PIN_ID PACKAGE_TRAIN PRODUCT_VERSION \
  IMAGE_SHA256 WORKER_TOOLS_SHA256 GENERATION SYSTEM_GENERATION PUBLIC_BASE_URL CHANNEL CHANNEL_PAYLOAD_SHA256 \
  CHANNEL_PAYLOAD_B64 CHANNEL_SIGNATURE_B64 BUNDLE_ID CLOUD_FILESYSTEM CLOUD_VIRTUAL_SIZE_GIB \
  TARGET ARCHITECTURE PACKAGE_ARCH FREEBSD_TARGET FREEBSD_TARGET_ARCH POUDRIERE_ARCH KERNEL \
  EXECUTOR IMAGE_PROFILE FIRMWARE IMAGE_CAPABILITIES INSTALLER_FORMAT PUBLISH_ENABLED; do
  eval "$name=\$(decode \"\${${name}_B64}\")"
done
unset AWS_ACCESS_KEY_ID_B64 AWS_SECRET_ACCESS_KEY_B64 AWS_SESSION_TOKEN_B64
unset FREESENSE_REPO_SIGNING_KEY_B64
export HOME=/root PATH="/usr/local/sbin:/usr/local/bin:${PATH}"
export ASSUME_ALWAYS_YES=yes LC_ALL=C LANG=C TZ=UTC
umask 022
case "${STAGE}" in system|packages|iso|cloud) : ;; *) echo "invalid build stage" >&2; exit 1 ;; esac
case "${TARGET}:${ARCHITECTURE}:${PACKAGE_ARCH}:${FREEBSD_TARGET}:${FREEBSD_TARGET_ARCH}:${POUDRIERE_ARCH}" in
  amd64:amd64:amd64:amd64:amd64:amd64.amd64) : ;;
  arm64:arm64:aarch64:arm64:aarch64:arm64.aarch64) : ;;
  *) echo "invalid target descriptor" >&2; exit 1 ;;
esac
case "${PUBLISH_ENABLED}" in true|false) : ;; *) echo "invalid publication policy" >&2; exit 1 ;; esac
case "${CHANNEL}" in devel|stable) : ;; *) echo "invalid selected channel" >&2; exit 1 ;; esac
case "${GENERATION}:${SYSTEM_GENERATION}" in
  *[!0-9:]*|:*|*:) echo "invalid build generation" >&2; exit 1 ;;
esac
if [ "${SYSTEM_GENERATION}" = 0 ]; then
  SYSTEM_GENERATION="${GENERATION}"
fi
[ "${GENERATION}" -gt 0 ] && [ "${SYSTEM_GENERATION}" -gt 0 ] || {
  echo "invalid build generation" >&2
  exit 1
}
for value in "${FINGERPRINT}" "${PLATFORM_ID}" "${SYSTEM_ID}" "${IMAGE_SHA256}" \
  "${WORKER_TOOLS_SHA256}"; do
  case "${value}" in ''|*[!0-9a-f]*) echo "invalid SHA-256 build input" >&2; exit 1 ;; esac
  [ "${#value}" -eq 64 ] || { echo "invalid SHA-256 build input" >&2; exit 1; }
done
if [ "${STAGE}" = iso ] || [ "${STAGE}" = cloud ]; then
  case "${PACKAGES_ID}" in ''|*[!0-9a-f]*) echo "invalid release Packages identity" >&2; exit 1 ;; esac
  [ "${#PACKAGES_ID}" -eq 64 ] || { echo "invalid release Packages identity" >&2; exit 1; }
  case "${BUNDLE_ID}" in ''|*[!0-9a-f]*) echo "invalid release bundle identity" >&2; exit 1 ;; esac
  [ "${#BUNDLE_ID}" -eq 64 ] || { echo "invalid release bundle identity" >&2; exit 1; }
fi
if [ "${STAGE}" = cloud ]; then
  case "${CLOUD_FILESYSTEM}" in ufs|zfs) : ;; *)
    echo "invalid cloud filesystem" >&2; exit 1 ;;
  esac
  case "${CLOUD_VIRTUAL_SIZE_GIB}" in ''|*[!0-9]*|0)
    echo "invalid cloud virtual size" >&2; exit 1 ;;
  esac
fi
case "${FREEBSD_PIN_ID}" in ''|*[!0-9a-f]*) echo "invalid FreeBSD pin identity" >&2; exit 1 ;; esac
[ "${#FREEBSD_PIN_ID}" -eq 64 ] || { echo "invalid FreeBSD pin identity" >&2; exit 1; }
printf '%s\n' "${PRODUCT_VERSION}" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+-(RELEASE|DEVELOPMENT)$' || {
  echo "invalid product version" >&2
  exit 1
}
product_train=$(printf '%s\n' "${PRODUCT_VERSION}" | sed -E 's/^([0-9]+\.[0-9]+)\..*$/\1/')
[ "${product_train}" = "${PACKAGE_TRAIN}" ] || {
  echo "product version does not match package train" >&2
  exit 1
}
if [ "${STAGE}" = iso ] || [ "${STAGE}" = cloud ]; then
  case "${CHANNEL_PAYLOAD_SHA256}" in ''|*[!0-9a-f]*)
    echo "release image requires the exact signed channel payload" >&2; exit 1 ;;
  esac
  [ "${#CHANNEL_PAYLOAD_SHA256}" -eq 64 ] || {
    echo "release image requires the exact signed channel payload" >&2; exit 1;
  }
  [ -n "${CHANNEL_PAYLOAD_B64}" ] || {
    echo "release image requires the exact channel payload" >&2; exit 1;
  }
  if [ "${PUBLISH_ENABLED}" = true ] && [ -z "${CHANNEL_SIGNATURE_B64}" ]; then
    echo "published release image requires the exact signed channel document" >&2; exit 1
  fi
fi
PREFIX=v1
RESULT="R2:${R2_BUCKET}/${PREFIX}/artifacts/${STAGE}/${FINGERPRINT}"
if [ "${STAGE}" = packages ]; then
  RESULT="R2:${R2_BUCKET}/${PREFIX}/artifacts/packages/${PACKAGE_TRAIN}/${FINGERPRINT}"
fi

install_worker_tools

phase storage-config
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
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
  mkdir -p "${destination}"
  git -C "${destination}" init -q
  git -C "${destination}" remote add origin "${url}"
  git -C "${destination}" fetch -q --depth=1 origin "${commit}"
  git -C "${destination}" checkout -q --detach FETCH_HEAD
  test "$(git -C "${destination}" rev-parse HEAD)" = "${commit}"
}

configure_source() {
  os_definition_dir=
  phase clone-source
  clone_exact https://github.com/FreeSense-org/freesense.git /root/freesense-src "${SOURCE_SHA}"
  case "${STAGE}" in
    system)
      phase clone-system-ports
      clone_exact https://github.com/FreeSense-org/freesense-system-ports.git \
        /root/freesense-system-ports "${SYSTEM_SHA}"
      phase clone-os-definition
      clone_exact https://github.com/FreeSense-org/freesense-os-base.git \
        /root/os-definition "${OS_BASE_SHA}"
      os_definition_dir=/root/os-definition
      ;;
    packages)
      phase clone-system-ports
      clone_exact https://github.com/FreeSense-org/freesense-system-ports.git \
        /root/freesense-system-ports "${SYSTEM_SHA}"
      phase clone-optional-packages
      clone_exact https://github.com/FreeSense-org/freesense-packages.git \
        /root/freesense-packages "${PACKAGES_SHA}"
      ;;
    iso) : ;;
  esac
  phase configure-source
  if [ "${STAGE}" = system ]; then
    sed -i '' "s/^UPSTREAM_REF=.*/UPSTREAM_REF=\"${FREEBSD_SHA}\"/" \
      /root/os-definition/manifest.env
  fi
  cd /root/freesense-src
  cp build.conf.sample build.conf

  # The source repository follows the rolling train, while a checked stable
  # build may intentionally target an older 1.0.x release. Bind packaged
  # version files to the explicit build input rather than the branch marker.
  printf '%s\n' "${PRODUCT_VERSION}" >src/etc/version

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
  cat >>build.conf <<EOF
export PRODUCT_NAME_SUFFIX=""
export PRODUCT_VERSION="${PRODUCT_VERSION}"
export POUDRIERE_BRANCH=main
export POUDRIERE_PORTS_GIT_URL="https://github.com/freebsd/freebsd-ports.git"
export POUDRIERE_PORTS_GIT_BRANCH="main"
export FREEBSD_SRC_PATCHES_DIR="${os_definition_dir}"
export FREESENSE_PORTS_COMMIT="${PORTS_SHA}"
export FREESENSE_PACKAGE_TRAIN="${PACKAGE_TRAIN}"
export PRODUCT_REVISION="${GENERATION}"
export FREESENSE_DIST_WORLD_ARCHIVE="/root/jail-base.txz"
export FREESENSE_SYSTEM_FINGERPRINT="${SYSTEM_ID}"
export FREESENSE_CHANNEL_PUBLIC_KEY_FILE="/root/sign/channel-public.pem"
export DO_NOT_SIGN_PKG_REPO=1
export FREESENSE_MAKE_JOBS_NUMBER_LIMIT=4
export TARGET="${FREEBSD_TARGET}"
export TARGET_ARCH="${FREEBSD_TARGET_ARCH}"
export ARCH_LIST="${POUDRIERE_ARCH}"
export BUILD_KERNELS="${KERNEL}"
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
CHECK_CHANGED_OPTIONS=verbose
CHECK_CHANGED_DEPS=yes
PARALLEL_JOBS=3
PREPARE_PARALLEL_JOBS=3
ALLOW_MAKE_JOBS=yes
USE_TMPFS=wrkdir
TMPFS_LIMIT=4
TMPFS_BLACKLIST="rust telegraf"
TMPFS_BLACKLIST_TMPDIR=/usr/local/poudriere/data/cache/tmp
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
}

run_poudriere_build() {
  set +e
  "$@"
  status=$?
  set -e
  [ "${status}" -eq 0 ] && return 0

  logs_root=${POUDRIERE_LOGS_ROOT:-/usr/local/poudriere/data/logs/bulk}
  error_list=/tmp/freesense-poudriere-error-logs.$$
  echo "FreeSense Poudriere failure diagnostics begin" >&2
  if [ -d "${logs_root}" ]; then
    find -L "${logs_root}" -type f -path '*/logs/errors/*.log' -print 2>/dev/null \
      | sort | tail -n 10 >"${error_list}"
  else
    : >"${error_list}"
  fi
  if [ -s "${error_list}" ]; then
    while IFS= read -r error_log; do
      echo "----- Poudriere error log: ${error_log} -----" >&2
      tail -n 1200 "${error_log}" >&2 || true
      echo "----- End Poudriere error log: ${error_log} -----" >&2
    done <"${error_list}"
  else
    echo "No Poudriere logs/errors files were found; recent log files:" >&2
    find "${logs_root}" -type f -name '*.log' -print 2>/dev/null \
      | sort | tail -n 20 >&2 || true
  fi
  rm -f "${error_list}"
  echo "FreeSense Poudriere failure diagnostics end status=${status}" >&2
  return "${status}"
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
  jail_name="FreeSense_main_${FREEBSD_TARGET_ARCH}"
  repository="/usr/local/poudriere/data/packages/${jail_name}-FreeSense_main"
  jail_version_file="/usr/local/etc/poudriere.d/jails/${jail_name}/version"
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
  repository="/usr/local/poudriere/data/packages/FreeSense_main_${FREEBSD_TARGET_ARCH}-FreeSense_main"
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
  poudriere_cross_args=
  if [ "${FREEBSD_TARGET_ARCH}" = aarch64 ]; then
    command -v qemu-aarch64-static >/dev/null || { echo "qemu-aarch64-static is required" >&2; return 1; }
    service qemu_user_static forcestart >/dev/null
    binmiscctl lookup aarch64 >/dev/null || { echo "aarch64 binmisc registration is missing" >&2; return 1; }
    poudriere_cross_args=-X
  fi
  poudriere jail -c -j "FreeSense_main_${FREEBSD_TARGET_ARCH}" -a "${POUDRIERE_ARCH}" \
    ${poudriere_cross_args} -v 16.0-CURRENT -m tar=/root/jail-base.txz
  if [ "${FREEBSD_TARGET_ARCH}" = aarch64 ]; then
    jail_root="/usr/local/poudriere/jails/FreeSense_main_${FREEBSD_TARGET_ARCH}"
    probe="${jail_root}/bin/echo"
    file "${probe}" | grep -q 'ARM aarch64' || { echo "aarch64 jail probe has wrong architecture" >&2; return 1; }
    qemu-aarch64-static -L "${jail_root}" "${probe}" freesense-aarch64-probe \
      | grep -qx freesense-aarch64-probe || {
      echo "aarch64 target executable probe failed" >&2; return 1;
    }
  fi
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

  strict_arch=${PACKAGE_ARCH:+yes}
  jq -Rr --arg expected_arch "FreeBSD:16:${PACKAGE_ARCH:-amd64}" --arg strict_arch "${strict_arch}" '
    select(length > 0) | fromjson |
    if ((.arch == $expected_arch or ($strict_arch == "" and (.arch == null))) and
        (.repopath | type) == "string" and
        (.repopath | test("^All/[^/]+[.]pkg$")) and
        ((.repopath | test("[\\t\\r\\n]")) | not) and
        (.sum | type) == "string" and
        (.sum as $sum |
         (($sum | test("^[0-9a-f]{64}$")) or
          (((($sum | startswith("0$")) or ($sum | startswith("5$"))) and
            ($sum[2:] | test("^[ybndrfg8ejkmcpqxot1uwisza345h769]{52}$")))) or
          (($sum | startswith("1$")) and ($sum[2:] | test("^[0-9a-f]{64}$"))) or
          (($sum | startswith("2$")) and
           ($sum[2:] | test("^[ybndrfg8ejkmcpqxot1uwisza345h769]{103}$"))))))
    then [.repopath, .sum] | @tsv
    else error("invalid signed package catalog record")
    end
  ' "${work}/packagesite.yaml" | LC_ALL=C sort >"${work}/expected"
  [ -s "${work}/expected" ] || {
    echo "signed package catalog is empty" >&2
    return 1
  }

  cut -f 1 "${work}/expected" >"${work}/expected-paths"
  : >"${work}/actual-paths"
  for package in "${repository}"/All/*.pkg; do
    if [ ! -e "${package}" ] && [ ! -L "${package}" ]; then
      continue
    fi
    [ -f "${package}" ] && [ ! -L "${package}" ] || {
      echo "repository package member is not a regular file: ${package##*/}" >&2
      return 1
    }
    printf 'All/%s\n' "${package##*/}" >>"${work}/actual-paths"
  done
  LC_ALL=C sort -o "${work}/actual-paths" "${work}/actual-paths"
  cmp -s "${work}/expected-paths" "${work}/actual-paths" || {
    echo "repository packages do not match the signed catalog" >&2
    return 1
  }

  tab=$(printf '\t')
  while IFS="${tab}" read -r repopath checksum; do
    package=${repository}/${repopath}
    pkg checksum -q -c "${checksum}" "${package}" || {
      echo "repository package checksum does not match the signed catalog: ${repopath}" >&2
      return 1
    }
  done <"${work}/expected"
)

fetch_repository() {
  kind=$1 id=$2 destination=$3
  part="${destination}.part"
  rm -rf "${part}" "${destination}"
  mkdir -p "${part}"
  phase repository-fetch
  rclone copy --error-on-no-transfer --retries 10 --low-level-retries 20 \
    "R2:${R2_BUCKET}/${PREFIX}/artifacts/${kind}/${id}/${PACKAGE_ARCH}" "${part}"
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
  cat >/root/sign/sign.sh <<'EOF'
#!/bin/sh
set -e
read -t 2 sum
[ -n "${sum}" ]
echo SIGNATURE
printf '%s' "${sum}" | /usr/bin/openssl dgst -sign /root/sign/repo.key -sha256 -binary
echo
echo CERT
cat /root/sign/repo.pub
echo END
EOF
  chmod 700 /root/sign/sign.sh
  pkg repo "${directory}" signing_command: /root/sign/sign.sh
  phase repository-signed
}

publish_repository() {
  directory=$1
  phase repository-publish
  test -n "$(find "${directory}/All" -type f -name '*.pkg' -print -quit)"
  find "${directory}" -type f ! -name complete.json | while IFS= read -r file; do
    relative=${file#"${directory}/"}
    upload_immutable "${file}" "${RESULT}/${PACKAGE_ARCH}/${relative}"
  done
  jq -n --arg stage "${STAGE}" --arg fingerprint "${FINGERPRINT}" \
    --arg platform "${PLATFORM_ID}" --arg system "${SYSTEM_ID}" \
    --arg source "${SOURCE_SHA}" --arg system_ports "${SYSTEM_SHA}" \
    --arg packages "${PACKAGES_SHA}" --arg freebsd "${FREEBSD_SHA}" \
    --arg ports "${PORTS_SHA}" --arg package_train "${PACKAGE_TRAIN}" \
    --arg os_definition "${OS_BASE_SHA}" --arg worker_image "${IMAGE_SHA256}" \
    --arg worker_tools "${WORKER_TOOLS_SHA256}" \
    --arg freebsd_pin_id "${FREEBSD_PIN_ID}" \
    --arg jail_object "${JAIL_OBJECT}" --arg signing_public_key "${derived_fingerprint}" \
    --arg architecture "${ARCHITECTURE}" --arg package_arch "${PACKAGE_ARCH}" \
    --arg image_profile "${IMAGE_PROFILE}" --arg firmware "${FIRMWARE}" \
    --argjson capabilities "${IMAGE_CAPABILITIES}" --argjson generation "${GENERATION}" \
    '{schema_version:"freesense.artifact/v1",stage:$stage,fingerprint:$fingerprint,generation:$generation,architecture:$architecture,package_arch:$package_arch,platform:$image_profile,firmware:($firmware|split(",")),capabilities:$capabilities,inputs:{platform:$platform,system:$system,source:$source,system_ports:$system_ports,freebsd:$freebsd,ports:$ports,freebsd_pin_id:$freebsd_pin_id,package_train:$package_train,os_definition:$os_definition,worker_image:$worker_image,worker_tools:$worker_tools,jail_object:$jail_object,signing_public_key:$signing_public_key}} | if $stage == "packages" then .inputs.packages = $packages | .inputs.built_against_system = $system else . end' \
    >"${directory}/complete.json"
  upload_immutable "${directory}/complete.json" "${RESULT}/complete.json"
  phase repository-complete
}
