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
seed_poudriere_repository /root/system-repo
phase optional-system-seed-ready

create_source_archive
phase optional-packages-build
run_poudriere_build env NOLINUX=yes ./build.sh --update-pkg-repo
phase optional-packages-ready
latest=$(poudriere_latest_repository)
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
