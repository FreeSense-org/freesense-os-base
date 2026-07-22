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

phase optional-system-seed
retry_repository=/root/work/poudriere-retry
rm -rf "${retry_repository}"
set +e
restore_poudriere_retry_cache "${retry_repository}" /root/system-repo
retry_status=$?
set -e
case "${retry_status}" in 129|130|143) exit "${retry_status}" ;; esac
if [ "${retry_status}" -eq 0 ]; then
  seed_poudriere_repository /root/system-repo "${retry_repository}"
else
  echo "No verified exact-fingerprint package retry is available; using System only."
  seed_poudriere_repository /root/system-repo
fi
phase optional-system-seed-ready

create_source_archive
phase optional-packages-build
run_poudriere_build /root/system-repo
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
