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

echo "FreeBSD build runner probe: $(freebsd-version) / ${cpus} vCPUs / ${memory} bytes RAM"
