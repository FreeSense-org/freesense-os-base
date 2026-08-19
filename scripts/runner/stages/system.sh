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
        exit 1
      }
      kernel_package=${package}
      ;;
  esac
  merge_package "${package}" /root/work/system/All "${inventory}" reject
done
[ -n "${kernel_package}" ] || { echo "built kernel package is missing" >&2; exit 1; }
kernel_member=$(tar -tf "${kernel_package}" | grep -E '(^|/)boot/kernel/kernel(\.gz)?$' | head -1)
[ -n "${kernel_member}" ] || { echo "built kernel payload is missing" >&2; exit 1; }
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
    echo "built kernel is not ARM64" >&2; exit 1;
  }
else
  file /tmp/freesense-built-kernel | grep -Eq 'ELF 64-bit.*x86-64' || {
    echo "built kernel is not amd64" >&2; exit 1;
  }
fi
rm -f /tmp/freesense-built-kernel

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
