#!/bin/sh
set -eu

kernel=$(uname -K)
cpus=$(sysctl -n hw.ncpu)
memory=$(sysctl -n hw.physmem)

[ "$kernel" -ge 1600000 ] || { echo "expected FreeBSD 16, got kernel version $kernel" >&2; exit 1; }
[ "$cpus" -eq 12 ] || { echo "expected exactly 12 guest CPUs, got $cpus" >&2; exit 1; }
[ "$memory" -ge 33000000000 ] || { echo "expected a 32 GiB guest, got $memory bytes" >&2; exit 1; }
for tool in fetch openssl pkg sha256 tar; do
	command -v "${tool}" >/dev/null || {
		echo "pinned worker image lacks base tool ${tool}" >&2
		exit 1
	}
done
pkg query '%n-%v' pkg >/dev/null
checksum_probe=$(mktemp)
trap 'rm -f "${checksum_probe}"' EXIT HUP INT TERM
printf 'bar\n' >"${checksum_probe}"
pkg checksum -q \
  -c '2$gf8mcrnmm6p6hg6wa9xkfb98zo8g6nxu8z4q7s93boz8hzf5ogrsr4qgpsb7utd6speio3op18ocyrsa9ms8jj15byttiq7ofbih8gn' \
  "${checksum_probe}"
rm -f "${checksum_probe}"
trap - EXIT HUP INT TERM

echo "FreeBSD build runner probe: $(freebsd-version) / ${cpus} vCPUs / ${memory} bytes RAM"
