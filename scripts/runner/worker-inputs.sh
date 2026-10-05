# Input acquisition for the build worker.
#
# Everything here fetches an input that is pinned elsewhere -- a Git commit in
# the plan, an inputs/sha256 object, a repository fingerprint -- and fails
# unless what arrived matches it. None of it decides what is built or how, so
# this file is not part of any component fingerprint (plan.py): fixing a
# download, retry or restore path does not rebuild System, Optional Packages
# or the images. Anything that shapes, composes, signs or publishes packages
# belongs in worker-common.sh or a stage, which are fingerprinted.
# tests/test_worker_inputs.py keeps build-shaping code out of this file.

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

# The pin stores each FreeBSD src and ports commit once as a bare one-commit
# repository whose main branch is that commit. Restored here, the builder clones
# it over file:// instead of fetching the same commit from GitHub every build.
#
# sh has no locals and fetch_input assigns object and destination, so these
# names must stay distinct from every helper this calls.
restore_upstream() {
  upstream_object=$1 upstream_dir=$2 upstream_commit=$3
  upstream_tar="${upstream_dir}.tar"
  case "${upstream_object}" in
    inputs/sha256/*) : ;;
    *) echo "upstream archive is not an immutable input" >&2; return 1 ;;
  esac
  phase "restore-$(basename "${upstream_dir}" .git)"
  rm -rf "${upstream_dir}" "${upstream_tar}"
  fetch_input "${upstream_object}" "${upstream_tar}"
  tar -C "$(dirname "${upstream_dir}")" -xf "${upstream_tar}"
  rm -f "${upstream_tar}"
  test "$(git -C "${upstream_dir}" rev-parse refs/heads/main)" = "${upstream_commit}" || {
    echo "upstream archive does not hold the pinned commit ${upstream_commit}" >&2
    return 1
  }
  phase "restored-$(basename "${upstream_dir}" .git)"
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

# Fetch every source a stage builds from, each verified against its pinned
# commit or hash, and point the builder at the local copies. Sets
# os_definition_dir and ports_url for configure_source.
acquire_sources() {
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
      # The stage signs with config/channel-signing-public.pem and reads
      # partition_roots.py, multiarch-shards.json and package_provenance.py from
      # here. It is not a patch set: FreeBSD src is never built in this stage.
      phase clone-os-definition
      clone_exact https://github.com/FreeSense-org/freesense-os-base.git \
        /root/os-definition "${OS_BASE_SHA}"
      ;;
    iso) : ;;
  esac
  phase configure-source
  if [ "${STAGE}" = system ]; then
    sed -i '' "s/^UPSTREAM_REF=.*/UPSTREAM_REF=\"${FREEBSD_SHA}\"/" \
      /root/os-definition/manifest.env
    if [ -n "${FREEBSD_SRC_OBJECT}" ]; then
      restore_upstream "${FREEBSD_SRC_OBJECT}" /root/freebsd-src.git "${FREEBSD_SHA}"
      sed -i '' 's|^UPSTREAM_URL=.*|UPSTREAM_URL="file:///root/freebsd-src.git"|' \
        /root/os-definition/manifest.env
    else
      echo "No stored FreeBSD src archive; fetching ${FREEBSD_SHA} from GitHub."
    fi
  fi
  ports_url=https://github.com/freebsd/freebsd-ports.git
  case "${STAGE}" in
    system|packages)
      if [ -n "${PORTS_OBJECT}" ]; then
        restore_upstream "${PORTS_OBJECT}" /root/freebsd-ports.git "${PORTS_SHA}"
        ports_url=file:///root/freebsd-ports.git
      else
        echo "No stored ports archive; fetching ${PORTS_SHA} from GitHub."
      fi
      ;;
  esac
}

# One file of the pinned FreeBSD source tree: from the stored src archive when
# the plan names one (restored on first use), otherwise from GitHub at the
# same commit. Names stay distinct from restore_upstream's and fetch_input's.
freebsd_src_file() {
  src_path=$1 src_destination=$2
  mkdir -p "$(dirname "${src_destination}")"
  if [ -n "${FREEBSD_SRC_OBJECT}" ]; then
    [ -d /root/freebsd-src.git ] || \
      restore_upstream "${FREEBSD_SRC_OBJECT}" /root/freebsd-src.git "${FREEBSD_SHA}"
    git -C /root/freebsd-src.git show "${FREEBSD_SHA}:${src_path}" >"${src_destination}"
  else
    fetch -qo "${src_destination}" \
      "https://raw.githubusercontent.com/freebsd/freebsd-src/${FREEBSD_SHA}/${src_path}"
  fi
  test -s "${src_destination}"
}

# A work tree of the pinned ports commit: from the stored ports archive when
# the plan names one, otherwise from GitHub.
freebsd_ports_tree() {
  ports_destination=$1
  if [ -n "${PORTS_OBJECT}" ]; then
    [ -d /root/freebsd-ports.git ] || \
      restore_upstream "${PORTS_OBJECT}" /root/freebsd-ports.git "${PORTS_SHA}"
    clone_exact file:///root/freebsd-ports.git "${ports_destination}" "${PORTS_SHA}"
  else
    clone_exact https://github.com/freebsd/freebsd-ports.git "${ports_destination}" "${PORTS_SHA}"
  fi
}
