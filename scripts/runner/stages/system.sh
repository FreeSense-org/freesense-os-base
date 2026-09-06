fetch_input "${JAIL_OBJECT}" /root/jail-base.txz
configure_source
cd /root/freesense-src

build_system_core() {
  phase system-build-core
  ./build.sh --build-core
  phase system-core-ready
  core=$(find tmp -type d -path '*-core/.real_*/All' -print -quit)
  test -n "${core}"
  core_repository=/root/work/system-core
  core_inventory=/tmp/system-core-package-inventory
  rm -rf "${core_repository}"
  mkdir -p "${core_repository}/All"
  : >"${core_inventory}"
  kernel_package=
  for package in "${core}"/*.pkg; do
    name=$(pkg query -F "${package}" '%n')
    case "${name}" in
      FreeSense-default-config|FreeSense-default-config-serial) continue ;;
    esac
    case "${name}" in
      FreeSense-kernel-debug-*) ;;
      FreeSense-kernel-*)
        [ -z "${kernel_package}" ] || {
          echo "multiple built kernel packages found" >&2
          return 1
        }
        kernel_package=${package}
        ;;
    esac
    merge_package "${package}" "${core_repository}/All" "${core_inventory}" reject
  done
  [ -n "${kernel_package}" ] || { echo "built kernel package is missing" >&2; return 1; }
  kernel_member=$(tar -tf "${kernel_package}" | grep -E '(^|/)boot/kernel/kernel(\.gz)?$' | head -1)
  [ -n "${kernel_member}" ] || { echo "built kernel payload is missing" >&2; return 1; }
  case "${kernel_member}" in
    *.gz)
      tar -xOf "${kernel_package}" "${kernel_member}" >/tmp/freesense-built-kernel.gz
      gzip -dc /tmp/freesense-built-kernel.gz >/tmp/freesense-built-kernel
      rm -f /tmp/freesense-built-kernel.gz
      ;;
    *) tar -xOf "${kernel_package}" "${kernel_member}" >/tmp/freesense-built-kernel ;;
  esac
  if [ "${PACKAGE_ARCH}" = aarch64 ]; then
    file /tmp/freesense-built-kernel | grep -Eq 'ELF 64-bit.*ARM aarch64' || {
      echo "built kernel is not ARM64" >&2; return 1;
    }
  else
    file /tmp/freesense-built-kernel | grep -Eq 'ELF 64-bit.*x86-64' || {
      echo "built kernel is not amd64" >&2; return 1;
    }
  fi
  rm -f /tmp/freesense-built-kernel
}

write_system_shard_roots() {
  all_roots=/tmp/system-farm-roots
  shard_roots=/tmp/system-shard-roots
  meta_dependencies=/tmp/system-farm-meta-dependencies
  ports_root=/usr/local/poudriere/ports/FreeSense_main
  make_conf=/usr/local/etc/poudriere.d/FreeSense_main-make.conf

  sed -e "s,%%PRODUCT_NAME%%,FreeSense,g" \
    -e "s,%%PRODUCT_VERSION%%,${PRODUCT_VERSION},g" \
    -e "s,%%FREESENSE_PACKAGE_TRAIN%%,${PACKAGE_TRAIN},g" \
    tools/conf/pfPorts/make.conf >"${make_conf}"
  cat >>"${make_conf}" <<EOF
PRODUCT_NAME=FreeSense
PRODUCT_VERSION=${PRODUCT_VERSION}
FREESENSE_PACKAGE_TRAIN=${PACKAGE_TRAIN}
POUDRIERE_PORTS_NAME=FreeSense_main
EOF
  sed 's/%%PRODUCT_NAME%%/FreeSense/g' tools/conf/pfPorts/poudriere_system \
    | sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' >"${all_roots}"
  : >"${meta_dependencies}"
  for meta_origin in security/FreeSense security/FreeSense-system; do
    env __MAKE_CONF="${make_conf}" make -C "${ports_root}/${meta_origin}" \
      -V RUN_DEPENDS -V LIB_DEPENDS >>"${meta_dependencies}" || {
      echo "failed to expand System metaport dependencies: ${meta_origin}" >&2
      return 1
    }
  done
  if grep -Eq '[$][{(]|%%[^%]+%%' "${meta_dependencies}"; then
    echo "System metaport dependencies contain unresolved variables" >&2
    cat "${meta_dependencies}" >&2
    return 1
  fi
  tr '[:space:]' '\n' <"${meta_dependencies}" | awk -F: '
    NF >= 2 {
      origin=$NF
      if (origin ~ "^[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.@-]+$") print origin
    }
  ' >>"${all_roots}"

  sed -e '/^security\/FreeSense$/d' -e '/^security\/FreeSense-system$/d' \
    "${all_roots}" | LC_ALL=C sort -u >"${all_roots}.sorted"
  root_count=$(awk 'END { print NR }' "${all_roots}.sorted")
  [ "${root_count}" -ge "${SYSTEM_SHARD_COUNT}" ] || {
    echo "System farm has fewer roots (${root_count}) than shards (${SYSTEM_SHARD_COUNT})" >&2
    return 1
  }
  awk -v shard="${SYSTEM_SHARD_INDEX}" -v count="${SYSTEM_SHARD_COUNT}" \
    '((NR - 1) % count) == shard' "${all_roots}.sorted" >"${shard_roots}"
  [ -s "${shard_roots}" ] || {
    echo "System package shard ${SYSTEM_SHARD_INDEX} has no roots" >&2
    return 1
  }
  cp "${shard_roots}" tools/conf/pfPorts/poudriere_bulk
  printf 'FreeSense System shard %s/%s roots:\n' \
    "${SYSTEM_SHARD_INDEX}" "${SYSTEM_SHARD_COUNT}"
  cat "${shard_roots}"
}

prepare_system_ports() {
  roots_mode=$1
  configure_poudriere
  create_jail
  export REPO_KIND=system OVERLAY_DIR=/root/freesense-system-ports
  phase system-ports-tree
  ./build.sh --update-poudriere-ports
  cp tools/conf/pfPorts/poudriere_system tools/conf/pfPorts/poudriere_bulk
  if [ "${PACKAGE_ARCH}" = aarch64 ]; then
    for excluded in \
      sysutils/xe-guest-utilities \
      dns/coredns \
      net/speedtest-go \
      net/cloud-init \
      sysutils/%%PRODUCT_NAME%%-cloud-init; do
      awk -v excluded="${excluded}" '$0 != excluded' tools/conf/pfPorts/poudriere_bulk \
        >tools/conf/pfPorts/poudriere_bulk.next
      mv tools/conf/pfPorts/poudriere_bulk.next tools/conf/pfPorts/poudriere_bulk
    done
  fi
  if [ "${roots_mode}" = shard ]; then
    write_system_shard_roots
  fi
  create_source_archive
}

build_system_packages() {
  phase system-packages-build
  run_poudriere_build env NOLINUX=yes ./build.sh --update-pkg-repo
  phase system-packages-ready
  latest=$(poudriere_latest_repository)
}

compose_system_repository() {
  core_source=$1 package_source=$2
  system_repository=/root/work/system
  system_inventory=/tmp/system-package-inventory
  rm -rf "${system_repository}"
  mkdir -p "${system_repository}/All"
  : >"${system_inventory}"
  for package in "${core_source}"/All/*.pkg; do
    merge_package "${package}" "${system_repository}/All" "${system_inventory}" reject
  done
  for package in "${package_source}"/All/*.pkg; do
    merge_package "${package}" "${system_repository}/All" "${system_inventory}" reject
  done

  phase system-closure-check
  : >/tmp/system-available-packages
  for package in "${system_repository}"/All/*.pkg; do
    pkg query -F "${package}" '%n|%v' >>/tmp/system-available-packages
  done
  sort -u /tmp/system-available-packages -o /tmp/system-available-packages
  for package in "${system_repository}"/All/*.pkg; do
    pkg query -F "${package}" '%dn|%dv' | while IFS= read -r dependency; do
      [ -z "${dependency}" ] || grep -Fqx "${dependency}" /tmp/system-available-packages || {
        echo "System package dependency is absent from the final closure: ${dependency}" >&2
        return 1
      }
    done
  done
  phase system-closure-ready
  sign_repository "${system_repository}"
  publish_repository "${system_repository}"
}

case "${SYSTEM_PART}" in
  core)
    build_system_core
    publish_system_checkpoint core core "${core_repository}"
    ;;
  shard)
    prepare_system_ports shard
    build_system_packages
    publish_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" "${latest}"
    ;;
  finalize)
    phase system-checkpoints-collect
    fetch_system_checkpoint core core /root/system-core-checkpoint
    shard_seed=/root/work/system-shard-seed
    shard_inventory=/tmp/system-shard-package-inventory
    rm -rf "${shard_seed}"
    mkdir -p "${shard_seed}/All"
    : >"${shard_inventory}"
    shard=0
    while [ "${shard}" -lt "${SYSTEM_SHARD_COUNT}" ]; do
      shard_directory=/root/system-shard-${shard}
      fetch_system_checkpoint shard "${shard}" "${shard_directory}"
      for package in "${shard_directory}/${PACKAGE_ARCH}/All"/*.pkg; do
        merge_package "${package}" "${shard_seed}/All" "${shard_inventory}" identical
      done
      shard=$((shard + 1))
    done
    phase system-checkpoints-collected
    prepare_system_ports full
    phase system-shard-seed
    seed_poudriere_repository "${shard_seed}"
    phase system-shard-seed-ready
    build_system_packages
    compose_system_repository \
      "/root/system-core-checkpoint/${PACKAGE_ARCH}" "${latest}"
    ;;
  full)
    build_system_core
    prepare_system_ports full
    build_system_packages
    compose_system_repository "${core_repository}" "${latest}"
    ;;
esac
