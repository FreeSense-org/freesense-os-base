# configure_source clones the os-definition only for the System stage, but this
# stage needs it just as much: on a shard configure_signing takes the trust
# anchor from config/channel-signing-public.pem, and the stage reads
# partition_roots.py, multiarch-shards.json and package_provenance.py out of the
# same checkout. Without it every Optional shard dies at repository-signing-key.
#
# Cloning it here rather than in configure_source keeps the System fingerprint
# untouched -- worker-common.sh is in the platform and System recipe digests and
# this file is in neither, so a fix there would discard completed System work
# that it cannot affect. Fold this into configure_source's packages branch the
# next time worker-common.sh changes for its own reasons.
phase clone-os-definition
clone_exact https://github.com/FreeSense-org/freesense-os-base.git \
  /root/os-definition "${OS_BASE_SHA}"
configure_source
fetch_repository system "${SYSTEM_ID}" /root/system-repo
cd /root/freesense-src
configure_poudriere
create_jail
export REPO_KIND=packages OVERLAY_DIR=/root/freesense-packages
export FREESENSE_SYSTEM_OVERLAY_DIR=/root/freesense-system-ports
phase optional-ports-tree
./build.sh --update-poudriere-ports
mkdir -p /usr/local/etc/poudriere.d
cat >>/usr/local/etc/poudriere.d/FreeSense_main-make.conf <<'EOF'
IGNORE_OSVERSION=yes
PKG_ENV+= IGNORE_OSVERSION=yes
EOF
if [ -n "${MIRROR_PLAN_OBJECT}" ]; then
  fetch_delta_mirror
  write_delta_bulk optional
else
  cp tools/conf/pfPorts/poudriere_packages tools/conf/pfPorts/poudriere_bulk
fi
policy=/root/freesense-packages/architecture-policy.json
jq -e --arg arch "${PACKAGE_ARCH}" '
  .schema_version == "freesense.optional-package-architectures/v1" and
  (.architectures[$arch].exclude | type) == "array" and
  all(.architectures[$arch].exclude[];
    (.origin | type) == "string" and
    (.reason | type) == "string" and (.reason | length) >= 20 and
    ((.issue | type) == "string" and (.issue | startswith("https://"))) and
    (.review_date | type) == "string")
' "${policy}" >/dev/null || { echo "invalid optional-package architecture policy" >&2; exit 1; }
jq -r --arg arch "${PACKAGE_ARCH}" '.architectures[$arch].exclude[].origin' \
  "${policy}" >/tmp/optional-exclusions
while IFS= read -r origin; do
  [ -f "/root/freesense-packages/${origin}/Makefile" ] || {
    echo "optional-package exclusion origin does not exist: ${origin}" >&2; exit 1;
  }
  template_origin=$(printf '%s\n' "${origin}" | sed 's/FreeSense/%%PRODUCT_NAME%%/g')
  awk -v excluded="${origin}" -v template="${template_origin}" \
    '$0 != excluded && $0 != template' tools/conf/pfPorts/poudriere_bulk \
    >tools/conf/pfPorts/poudriere_bulk.next
  mv tools/conf/pfPorts/poudriere_bulk.next tools/conf/pfPorts/poudriere_bulk
done </tmp/optional-exclusions

phase optional-system-seed
combined=/root/optional-farm-seed
mkdir -p "${combined}/All"
inventory=/tmp/optional-farm-seed-inventory
: >"${inventory}"
rm -f "${inventory}.rebuild"
# Optional builds on top of System's result and on top of the same lower
# layer System used -- the mirror where there is one, the pin-time binary
# seed otherwise. System's packages win any overlap, which is what
# `identical` enforces.
if [ -n "${MIRROR_PLAN_OBJECT}" ]; then
  lower=/root/mirror-repo
else
  prepare_merged_binary_seed
  lower=/root/merged-binary-seed
fi
for package in /root/system-repo/All/*.pkg "${lower}"/All/*.pkg; do
  merge_package "${package}" "${combined}/All" "${inventory}" identical
done
if [ "${SYSTEM_PART}" = finalize ]; then
  shard=0
  while [ "${shard}" -lt "${SYSTEM_SHARD_COUNT}" ]; do
    checkpoint=/root/optional-shard-${shard}
    CHECKPOINT_BATCH=$(latest_checkpoint_batch shard "${shard}" || true); export CHECKPOINT_BATCH
    [ -n "${CHECKPOINT_BATCH}" ] || { echo "missing completed Optional checkpoint: ${shard}" >&2; exit 1; }
    fetch_system_checkpoint shard "${shard}" "${checkpoint}"
    for package in "${checkpoint}/${PACKAGE_ARCH}/All"/*.pkg; do
      merge_package "${package}" "${combined}/All" "${inventory}" rebuild
    done
    shard=$((shard + 1))
  done
else
  # poudriere_packages is a template: 34 of its origins are spelled
  # %%PRODUCT_NAME%%-pkg-*. partition_roots.py's ORIGIN pattern does not admit
  # '%', so feeding it the raw list raises "invalid shard root plan" before a
  # single package is built. The System stage already substitutes at the same
  # point; this one never did, which is why no Optional shard has ever run.
  sed 's/%%PRODUCT_NAME%%/FreeSense/g' tools/conf/pfPorts/poudriere_bulk \
    | sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    | LC_ALL=C sort -u >/tmp/optional-all-roots
  python_bin=$(command -v python3 || command -v python3.11)
  "${python_bin}" /root/os-definition/scripts/partition_roots.py \
    --config /root/os-definition/config/multiarch-shards.json --component packages \
    --shard "${SYSTEM_SHARD_INDEX}" --roots /tmp/optional-all-roots \
    --output tools/conf/pfPorts/poudriere_bulk --batches-output /tmp/optional-shard-batches.json
  if [ ! -s tools/conf/pfPorts/poudriere_bulk ]; then
    echo "FreeSense Optional shard ${SYSTEM_SHARD_INDEX}/${SYSTEM_SHARD_COUNT} has no source deltas."
    publish_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" "${combined}"
    exit 0
  fi
fi
seed_poudriere_repository "${combined}"
phase optional-system-seed-ready

create_source_archive
if [ "${SYSTEM_PART}" = shard ]; then
  batch_count=$(jq -r length /tmp/optional-shard-batches.json)
  [ "${batch_count}" -gt 0 ] || { echo "non-empty Optional shard has no cumulative batches" >&2; exit 1; }
  completed_batch=$(latest_checkpoint_batch shard "${SYSTEM_SHARD_INDEX}" || true)
  next_batch=0
  if [ -n "${completed_batch}" ]; then
    [ "${completed_batch}" -lt "${batch_count}" ] || { echo "Optional checkpoint batch exceeds plan" >&2; exit 1; }
    CHECKPOINT_BATCH=${completed_batch}; export CHECKPOINT_BATCH
    fetch_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" /root/resumed-optional-checkpoint
    jq -e --argjson expected "$(jq -c --argjson batch "${completed_batch}" '.[$batch]' /tmp/optional-shard-batches.json)" \
      '.roots == $expected' /root/resumed-optional-checkpoint/complete.json >/dev/null || {
      echo "resumed Optional checkpoint roots conflict with cumulative plan" >&2; exit 1;
    }
    seed_poudriere_repository "/root/resumed-optional-checkpoint/${PACKAGE_ARCH}"
    next_batch=$((completed_batch + 1))
  fi
  while [ "${next_batch}" -lt "${batch_count}" ]; do
    jq -r --argjson batch "${next_batch}" '.[$batch][]' /tmp/optional-shard-batches.json \
      >tools/conf/pfPorts/poudriere_bulk
    cp tools/conf/pfPorts/poudriere_bulk /tmp/checkpoint-current-roots
    phase optional-packages-build
    run_poudriere_build env NOLINUX=yes IGNORE_OSVERSION=yes ASSUME_ALWAYS_YES=yes ./build.sh --update-pkg-repo
    phase optional-packages-ready
    latest=$(poudriere_latest_repository)
    if [ -n "${MIRROR_PLAN_OBJECT}" ]; then
      verify_delta_build "${latest}" /root/mirror-plan.json
    fi
    CHECKPOINT_BATCH=${next_batch}; export CHECKPOINT_BATCH
    publish_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" "${latest}"
    next_batch=$((next_batch + 1))
  done
  exit 0
fi
phase optional-packages-build
run_poudriere_build env NOLINUX=yes IGNORE_OSVERSION=yes ASSUME_ALWAYS_YES=yes ./build.sh --update-pkg-repo
phase optional-packages-ready
latest=$(poudriere_latest_repository)
if [ -n "${MIRROR_PLAN_OBJECT}" ]; then
  verify_delta_build "${latest}" /root/mirror-plan.json
fi
mkdir -p /root/work/packages/All
inventory=/tmp/combined-package-inventory
: >"${inventory}"
for package in /root/system-repo/All/*.pkg; do
  inventory_package "${package}" "${inventory}"
done
for package in "${latest}"/All/*.pkg; do
  merge_package "${package}" /root/work/packages/All "${inventory}" identical
done
phase optional-closure-check
: >/tmp/available-packages
for package in /root/system-repo/All/*.pkg /root/work/packages/All/*.pkg; do
  pkg query -F "${package}" '%n|%v' >>/tmp/available-packages
done
sort -u /tmp/available-packages -o /tmp/available-packages
for package in /root/work/packages/All/*.pkg; do
  pkg query -F "${package}" '%dn|%dv' | while IFS= read -r dependency; do
    [ -z "${dependency}" ] || grep -Fqx "${dependency}" /tmp/available-packages || {
      echo "optional package dependency is absent from the combined System/package closure: ${dependency}" >&2
      exit 1
    }
  done
done
phase optional-closure-ready
sign_repository /root/work/packages
publish_repository /root/work/packages
