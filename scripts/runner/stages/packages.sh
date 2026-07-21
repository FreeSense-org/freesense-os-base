configure_source
fetch_repository system "${SYSTEM_ID}" /root/system-repo
cd /root/freesense-src
create_jail
configure_poudriere
export REPO_KIND=packages OVERLAY_DIR=/root/freesense-packages
export FREESENSE_SYSTEM_OVERLAY_DIR=/root/freesense-system-ports
phase optional-ports-tree
./build.sh --update-poudriere-ports
cp tools/conf/pfPorts/poudriere_packages tools/conf/pfPorts/poudriere_bulk

# Seed Poudriere with the already-tested system packages. It will build only
# changed optional ports and their missing dependencies.
cache=/usr/local/poudriere/data/packages/FreeSense_main_amd64-FreeSense_main/.real_system
mkdir -p "${cache}/All"
cp /root/system-repo/All/*.pkg "${cache}/All/"
pkg repo "${cache}"
ln -sfn .real_system "${cache%/.real_system}/.latest"

create_source_archive
phase optional-packages-build
env NOLINUX=yes ./build.sh --update-pkg-repo
phase optional-packages-ready
latest=$(find /usr/local/poudriere/data/packages -type l -name .latest -exec realpath {} \; | head -1)
test -n "${latest}"
mkdir -p /root/work/packages/All
: >/tmp/system-names
for package in /root/system-repo/All/*.pkg; do pkg query -F "${package}" '%n' >>/tmp/system-names; done
sort -u /tmp/system-names -o /tmp/system-names
for package in "${latest}"/All/*.pkg; do
  name=$(pkg query -F "${package}" '%n')
  grep -qx "${name}" /tmp/system-names || cp "${package}" /root/work/packages/All/
done
sign_repository /root/work/packages
publish_repository /root/work/packages
