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
find "${core}" -type f -name '*.pkg' -exec cp {} /root/work/system/All/ \;

create_jail
configure_poudriere
export REPO_KIND=system OVERLAY_DIR=/root/freesense-system-ports
phase system-ports-tree
./build.sh --update-poudriere-ports
cp tools/conf/pfPorts/poudriere_system tools/conf/pfPorts/poudriere_bulk
create_source_archive
phase system-packages-build
env NOLINUX=yes ./build.sh --update-pkg-repo
phase system-packages-ready
latest=$(find /usr/local/poudriere/data/packages -type l -name .latest -exec realpath {} \; | head -1)
test -n "${latest}"
find "${latest}/All" -type f -name '*.pkg' -exec cp {} /root/work/system/All/ \;
sign_repository /root/work/system
publish_repository /root/work/system
