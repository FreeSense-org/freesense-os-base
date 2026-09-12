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

partition_system_shard() {
  [ "${roots_mode}" = shard ] || return 0
  python_bin=$(command -v python3 || command -v python3.11)
  "${python_bin}" /root/os-definition/scripts/partition_roots.py \
    --config /root/os-definition/config/multiarch-shards.json --component system \
    --shard "${SYSTEM_SHARD_INDEX}" --roots "${all_roots}.sorted" --output "${shard_roots}" \
    --batches-output /tmp/system-shard-batches.json
}

write_system_farm_roots() {
  roots_mode=$1
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
IGNORE_OSVERSION=yes
PKG_ENV+= IGNORE_OSVERSION=yes
EOF
  if [ -n "${MIRROR_PLAN_OBJECT}" ]; then
    # The sealed plan already names exactly what this stage builds.
    # Expanding the metaports here would put the whole System closure back,
    # including every package the mirror provides, and we would compile the
    # packages we just downloaded.
    LC_ALL=C sort -u /tmp/delta-roots >"${all_roots}.sorted"
    partition_system_shard
    return 0
  fi
  sed 's/%%PRODUCT_NAME%%/FreeSense/g' tools/conf/pfPorts/poudriere_bulk \
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

  partition_system_shard
  if [ "${roots_mode}" = shard ] && [ ! -s "${shard_roots}" ]; then
    EMPTY_SOURCE_SHARD=true
    export EMPTY_SOURCE_SHARD
    : >tools/conf/pfPorts/poudriere_bulk
    echo "FreeSense System shard ${SYSTEM_SHARD_INDEX}/${SYSTEM_SHARD_COUNT} has no source deltas."
    return 0
  fi
  [ -s "${shard_roots}" ] || {
    echo "System package ${roots_mode} ${SYSTEM_SHARD_INDEX} has no roots" >&2
    return 1
  }
  cp "${shard_roots}" tools/conf/pfPorts/poudriere_bulk
  printf 'FreeSense System %s %s/%s roots:\n' \
    "${roots_mode}" "${SYSTEM_SHARD_INDEX}" "${SYSTEM_SHARD_COUNT}"
  cat "${shard_roots}"
}

prepare_system_ports() {
  roots_mode=$1
  configure_poudriere
  create_jail
  export REPO_KIND=system OVERLAY_DIR=/root/freesense-system-ports
  phase system-ports-tree
  ./build.sh --update-poudriere-ports
  if [ -n "${MIRROR_PLAN_OBJECT}" ]; then
    fetch_delta_mirror
    write_delta_bulk system
  else
    cp tools/conf/pfPorts/poudriere_system tools/conf/pfPorts/poudriere_bulk
  fi
  if [ "${PACKAGE_ARCH}" = aarch64 ] && [ -z "${MIRROR_PLAN_OBJECT}" ]; then
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
  case "${roots_mode}" in
    shard) write_system_farm_roots shard ;;
  esac
  create_source_archive
  if [ -n "${MIRROR_PLAN_OBJECT}" ]; then
    # The mirror replaces the pin-time binary seed rather than joining it.
    # It is a superset computed from the same signed catalogue, so merging
    # both would only offer Poudriere two candidates for one name.
    phase system-mirror-seed
    seed_poudriere_repository /root/mirror-repo
    phase system-mirror-seed-ready
  elif [ -n "${BINARY_SEED_OBJECT}" ]; then
    prepare_merged_binary_seed
    seed_poudriere_repository /root/merged-binary-seed
  fi
}

build_system_packages() {
  phase system-packages-build
  run_poudriere_build env NOLINUX=yes IGNORE_OSVERSION=yes ASSUME_ALWAYS_YES=yes ./build.sh --update-pkg-repo
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
    if [ "${EMPTY_SOURCE_SHARD:-false}" = true ]; then
      publish_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" /root/merged-binary-seed
      exit 0
    fi
    batch_count=$(jq -r length /tmp/system-shard-batches.json)
    [ "${batch_count}" -gt 0 ] || { echo "non-empty shard has no cumulative batches" >&2; exit 1; }
    completed_batch=$(latest_checkpoint_batch shard "${SYSTEM_SHARD_INDEX}" || true)
    next_batch=0
    if [ -n "${completed_batch}" ]; then
      [ "${completed_batch}" -lt "${batch_count}" ] || { echo "checkpoint batch exceeds plan" >&2; exit 1; }
      CHECKPOINT_BATCH=${completed_batch}; export CHECKPOINT_BATCH
      fetch_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" /root/resumed-shard-checkpoint
      jq -e --argjson expected "$(jq -c --argjson batch "${completed_batch}" '.[$batch]' /tmp/system-shard-batches.json)" \
        '.roots == $expected' /root/resumed-shard-checkpoint/complete.json >/dev/null || {
        echo "resumed System checkpoint roots conflict with cumulative plan" >&2; exit 1;
      }
      seed_poudriere_repository "/root/resumed-shard-checkpoint/${PACKAGE_ARCH}"
      next_batch=$((completed_batch + 1))
    fi
    while [ "${next_batch}" -lt "${batch_count}" ]; do
      jq -r --argjson batch "${next_batch}" '.[$batch][]' /tmp/system-shard-batches.json \
        >tools/conf/pfPorts/poudriere_bulk
      cp tools/conf/pfPorts/poudriere_bulk /tmp/checkpoint-current-roots
      build_system_packages
      CHECKPOINT_BATCH=${next_batch}; export CHECKPOINT_BATCH
      publish_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" "${latest}"
      next_batch=$((next_batch + 1))
    done
    ;;
  finalize)
    phase system-checkpoints-collect
    fetch_system_checkpoint core core /root/system-core-checkpoint
    shard_seed=/root/work/system-shard-seed
    shard_inventory=/tmp/system-shard-package-inventory
    rm -rf "${shard_seed}"
    mkdir -p "${shard_seed}/All"
    : >"${shard_inventory}"
    rm -f "${shard_inventory}.rebuild"
    seed_duplicate_policy=rebuild
    shard=0
    while [ "${shard}" -lt "${SYSTEM_SHARD_COUNT}" ]; do
      shard_directory=/root/system-shard-${shard}
      CHECKPOINT_BATCH=$(latest_checkpoint_batch shard "${shard}" || true); export CHECKPOINT_BATCH
      [ -n "${CHECKPOINT_BATCH}" ] || { echo "missing completed shard checkpoint: ${shard}" >&2; exit 1; }
      fetch_system_checkpoint shard "${shard}" "${shard_directory}"
      for package in "${shard_directory}/${PACKAGE_ARCH}/All"/*.pkg; do
        merge_package "${package}" "${shard_seed}/All" "${shard_inventory}" "${seed_duplicate_policy}"
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
