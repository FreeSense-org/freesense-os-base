#!/bin/sh
#
# apply.sh — apply the FreeSense FreeBSD-src change-set onto a stock checkout.
#
# Usage: apply.sh <freebsd-src-dir>
#
#   <freebsd-src-dir> must be a STOCK freebsd/freebsd-src git checkout at the
#   commit named by UPSTREAM_REF in manifest.env. The builder's
#   update_freebsd_sources() resets the tree every run, so re-applying is clean.
#
# Patches are git-format diffs (created with `git diff <base>..devel-main`), so
# we apply them with `git apply` (handles new files + modes). They are split by
# subsystem and ordered by filename; order is not significant (disjoint paths)
# but is kept deterministic.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "${here}/manifest.env"

dir="${1:?usage: apply.sh <freebsd-src-dir>}"
[ -d "${dir}/.git" ] || { echo ">>> ERROR: ${dir} is not a git checkout" >&2; exit 1; }

cd "${dir}"
cur=$(git rev-parse --verify -q HEAD || echo "?")
echo ">>> FreeSense: applying freebsd-src change-set onto ${dir}"
echo "    HEAD=${cur}  expected base=${UPSTREAM_REF}"
if [ "${cur}" != "${UPSTREAM_REF}" ]; then
	echo "    NOTE: HEAD != UPSTREAM_REF — patches may fuzz/fail if upstream moved."
fi

n=0
for p in "${here}"/patches/*.patch; do
	[ -f "${p}" ] || continue
	echo "    applying $(basename "${p}")"
	# --recount: infer hunk line-counts from the body instead of trusting the
	#   @@ headers, so a hand-edited patch with a stale/off-by-one header still
	#   applies (git-scm docs: "after editing the patch without adjusting the
	#   hunk headers appropriately"). This has bitten manual installer-patch edits.
	# On failure, retry once with -3 (3-way merge) which is far more tolerant of
	#   context drift, then emit .rej files for a precise diagnostic before dying.
	if ! git apply --recount --whitespace=nowarn "${p}"; then
		echo "    NOTE: plain apply failed for $(basename "${p}") — retrying with 3-way merge"
		if ! git apply --recount --3way --whitespace=nowarn "${p}"; then
			echo ">>> ERROR: $(basename "${p}") does not apply. Generating .rej for diagnosis:" >&2
			git apply --recount --reject --whitespace=nowarn "${p}" >&2 || true
			find . -name '*.rej' -exec sh -c 'echo "----- {} -----"; cat "{}"' \; >&2 || true
			exit 1
		fi
	fi
	n=$((n + 1))
done
echo ">>> Done: ${n} patches applied."
