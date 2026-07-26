fetch_input "${JAIL_OBJECT}" /root/jail-base.txz
configure_source
cd /root/freesense-src

# World, kernel, boot, rc, and default configuration form the platform half of
# the system repository. The system overlay is then built against that exact
# world in the same isolated VM.
phase system-build-core
./build.sh --build-core
phase system-core-ready
core=$(find tmp -type d -path '*-core/.real_*/All' -print -quit)
test -n "${core}"
mkdir -p /root/work/system/All
inventory=/tmp/system-package-inventory
: >"${inventory}"
for package in "${core}"/*.pkg; do
  name=$(pkg query -F "${package}" '%n')
  case "${name}" in
    FreeSense-default-config|FreeSense-default-config-serial) continue ;;
  esac
  merge_package "${package}" /root/work/system/All "${inventory}" reject
done

configure_poudriere
create_jail
export REPO_KIND=system OVERLAY_DIR=/root/freesense-system-ports
phase system-ports-tree
./build.sh --update-poudriere-ports
cp tools/conf/pfPorts/poudriere_system tools/conf/pfPorts/poudriere_bulk
create_source_archive
phase system-packages-build
run_poudriere_build env NOLINUX=yes ./build.sh --update-pkg-repo
phase system-packages-ready
latest=$(poudriere_latest_repository)
for package in "${latest}"/All/*.pkg; do
  merge_package "${package}" /root/work/system/All "${inventory}" reject
done
sign_repository /root/work/system
publish_repository /root/work/system
