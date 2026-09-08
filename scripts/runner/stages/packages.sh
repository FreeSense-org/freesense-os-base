configure_source
fetch_repository system "${SYSTEM_ID}" /root/system-repo
cd /root/freesense-src
configure_poudriere
create_jail
export REPO_KIND=packages OVERLAY_DIR=/root/freesense-packages
export FREESENSE_SYSTEM_OVERLAY_DIR=/root/freesense-system-ports
phase optional-ports-tree
./build.sh --update-poudriere-ports
cp tools/conf/pfPorts/poudriere_packages tools/conf/pfPorts/poudriere_bulk
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
if [ "${FARM_LAYOUT}" = delta-v1 ]; then
  load_binary_seed
  combined=/root/optional-farm-seed
  mkdir -p "${combined}/All"
  inventory=/tmp/optional-farm-seed-inventory
  : >"${inventory}"
  rm -f "${inventory}.rebuild"
  for package in /root/system-repo/All/*.pkg /root/binary-seed/All/*.pkg; do
    merge_package "${package}" "${combined}/All" "${inventory}" identical
  done
  if [ "${SYSTEM_PART}" = finalize ]; then
    shard=0
    while [ "${shard}" -lt "${SYSTEM_SHARD_COUNT}" ]; do
      checkpoint=/root/optional-shard-${shard}
      fetch_system_checkpoint shard "${shard}" "${checkpoint}"
      for package in "${checkpoint}/${PACKAGE_ARCH}/All"/*.pkg; do
        merge_package "${package}" "${combined}/All" "${inventory}" rebuild
      done
      shard=$((shard + 1))
    done
  else
    sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' tools/conf/pfPorts/poudriere_bulk \
      | LC_ALL=C sort -u >/tmp/optional-all-roots
    python3 /root/os-definition/scripts/partition_roots.py \
      --config /root/os-definition/config/multiarch-shards.json --component packages \
      --shard "${SYSTEM_SHARD_INDEX}" --roots /tmp/optional-all-roots \
      --output tools/conf/pfPorts/poudriere_bulk
    if [ ! -s tools/conf/pfPorts/poudriere_bulk ]; then
      echo "FreeSense Optional shard ${SYSTEM_SHARD_INDEX}/${SYSTEM_SHARD_COUNT} has no source deltas."
      publish_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" "${combined}"
      exit 0
    fi
  fi
  seed_poudriere_repository "${combined}"
else
  seed_poudriere_repository /root/system-repo
fi
phase optional-system-seed-ready

create_source_archive
phase optional-packages-build
run_poudriere_build env NOLINUX=yes ./build.sh --update-pkg-repo
phase optional-packages-ready
latest=$(poudriere_latest_repository)
if [ "${FARM_LAYOUT}:${SYSTEM_PART}" = delta-v1:shard ]; then
  publish_system_checkpoint shard "${SYSTEM_SHARD_INDEX}" "${latest}"
  exit 0
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
