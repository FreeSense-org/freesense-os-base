#!/bin/sh
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# Native FreeBSD contract checks using a synthetic media directory and staged root.
set -eu
umask 077
lib=${1:?installer library directory required}
export FREESENSE_PROVISION_LIB="$lib"
# shellcheck source=/dev/null
. "$lib/provision.sh"
testroot=$(mktemp -d)
trap 'rm -rf "$testroot"' EXIT
cd "$testroot"
uuid=01234567-1234-4234-8234-0123456789ab
secret=$(printf '%043d' 0)
pin=$(printf '%064d' 0)
link=https://controller.invalid/fleet/join#000000000000.$secret.$pin
seed() { printf '{"version":1,"mode":"attended","link":"%s.%s"}' "$link" "$1"; }
plan() { printf 'version=1\naction=install\nmachine_uuid=%s\ntarget_id=TEST-DISK-01\n' "$uuid"; }
must_reject() {
	kind=$1
	mkdir -m 0700 refused
	if "$kind" "$2" "$PWD/refused" >out 2>err; then exit 1; fi
	test -z "$(ls -A refused)"
	if grep -qF "$secret" out err; then exit 1; fi
	rmdir refused
}
new_target() {
	mkdir -p "$1/usr/local/sbin"
	touch "$1/usr/local/sbin/freesense-config"
	chmod 0555 "$1/usr/local/sbin/freesense-config"
}
plan >plan.conf
printf 'lan=em0\nfleet_seed=%s\n' "$(seed t)" >>plan.conf
mkdir -m 0700 parsed
guided_parse_plan "$PWD/plan.conf" "$PWD/parsed"
test "$(cat parsed/machine_uuid)" = "$uuid"
test "$(cat parsed/target_id)" = TEST-DISK-01
test "$(cat parsed/fleet_mode)" = attended
test "$(cat parsed/transport)" = tunnel
test "$(cat parsed/fleet_seed)" = "$(seed t)"
test "$(stat -f %Lp parsed/fleet_seed)" = 600
guided_seed_summary parsed >out
if grep -qF "$secret" out; then exit 1; fi
new_target "$PWD/target"
guided_apply_plan "$PWD/parsed" "$PWD/target" include
path="$PWD/target/cf/conf/freesense/provision-pending"
cmp "$path/lan" parsed/lan
cmp "$path/fleet_seed" parsed/fleet_seed
test "$(stat -f %Lp "$path")" = 700
test "$(stat -f %Lp "$path/fleet_seed")" = 600
if guided_apply_plan "$PWD/parsed" "$PWD/target" include; then exit 1; fi
new_target "$PWD/skip"
guided_apply_plan "$PWD/parsed" "$PWD/skip" skip
test ! -e "$PWD/skip/cf/conf/freesense/provision-pending/fleet_seed"
seed s >seed.json
mkdir -m 0700 manual
guided_preview_seed "$PWD/seed.json" "$PWD/manual"
test "$(cat manual/transport)" = tailscale
test ! -e manual/target_id
seed d >seed.json
mkdir -m 0700 direct
guided_preview_seed "$PWD/seed.json" "$PWD/direct"
test "$(cat direct/transport)" = direct
mkdir -m 0700 attended_pin unattended
sealed='{"salt":"AAAAAAAAAAAAAAAAAAAAAA==","nonce":"AAAAAAAAAAAAAAAA","tag":"AAAAAAAAAAAAAAAAAAAAAA==","data":"YQ=="}'
printf '{"version":1,"mode":"pin","link":"%s.t","sealed_key":%s}' "$link" "$sealed" >seed.json
guided_preview_seed "$PWD/seed.json" "$PWD/attended_pin"
test "$(cat attended_pin/fleet_mode)" = pin
synthetic_key=$(/usr/libexec/flua - <<'LUA'
local data=string.rep("A", 19)
local symbols="0123456789ABCDEFGHJKMNPQRSTVWXYZ"
local c=1+math.floor(require("hash").sha256(data):digest():byte()/8)
print(data..symbols:sub(c,c))
LUA
)
printf '{"version":1,"mode":"unattended","link":"%s.d","key":"%s"}' "$link" "$synthetic_key" >seed.json
guided_preview_seed "$PWD/seed.json" "$PWD/unattended"
test "$(cat unattended/fleet_mode)" = unattended
printf '{"version":1,"mode":"pin","link":"%s.t","sealed_key":[]}' "$link" >bad.json
must_reject guided_preview_seed "$PWD/bad.json"
new_target "$PWD/nothing"
guided_apply_plan "$PWD/direct" "$PWD/nothing" skip
test ! -e "$PWD/nothing/cf"
for extra in 'typo=ignored' 'action=install' 'lan=em0;date' 'lan=-x' 'version=2' 'target_id=other'; do
	{ plan; printf '%s\n' "$extra"; } >bad.conf
	must_reject guided_parse_plan "$PWD/bad.conf"
done
for machine in 00000000-0000-0000-0000-000000000000 ffffffff-ffff-ffff-ffff-ffffffffffff partial; do
	plan | sed "s/$uuid/$machine/" >bad.conf
	must_reject guided_parse_plan "$PWD/bad.conf"
done
printf '%s' 'this must never run' >bad.conf
must_reject guided_parse_plan "$PWD/bad.conf"
plan | sed 's/TEST-DISK-01/GEOM-da0/' >bad.conf
must_reject guided_parse_plan "$PWD/bad.conf"
ln -s "$PWD/plan.conf" bad.link
must_reject guided_parse_plan "$PWD/bad.link"
dd if=/dev/zero of=bad.conf bs=70000 count=1 2>/dev/null
must_reject guided_parse_plan "$PWD/bad.conf"
for expr in 's/"version":1/"version":1.1/' 's/"attended"/"unattended"/' 's/"attended"/"pin"/' 's/https:/http:/' 's/}/,"expires":1}/'; do
	seed t | sed "$expr" >bad.json
	must_reject guided_preview_seed "$PWD/bad.json"
done
printf 'unsigned media preview; no target identity\n' >bad.json
must_reject guided_preview_seed "$PWD/bad.json"
# An optional bcrypt hash for the administrator travels in the handoff; a plaintext or low-cost value never does.
# shellcheck disable=SC2016 # a literal bcrypt hash
hash='$2y$10$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234'
{ plan; printf 'lan=em0
admin_password_hash=%s
' "$hash"; } >hash.conf
mkdir -m 0700 hashed
guided_parse_plan "$PWD/hash.conf" "$PWD/hashed"
test "$(cat hashed/admin_password_hash)" = "$hash"
new_target "$PWD/hashtarget"
guided_apply_plan "$PWD/hashed" "$PWD/hashtarget" skip
hp="$PWD/hashtarget/cf/conf/freesense/provision-pending"
cmp "$hp/admin_password_hash" hashed/admin_password_hash
test "$(stat -f %Lp "$hp/admin_password_hash")" = 600
{ plan; printf 'admin_password_hash=%s
' "$hash"; } >hashonly.conf
mkdir -m 0700 hashonly
guided_parse_plan "$PWD/hashonly.conf" "$PWD/hashonly"
new_target "$PWD/hashonlytarget"
guided_apply_plan "$PWD/hashonly" "$PWD/hashonlytarget" skip
test -f "$PWD/hashonlytarget/cf/conf/freesense/provision-pending/admin_password_hash"
test ! -e "$PWD/hashonlytarget/cf/conf/freesense/provision-pending/lan"
# shellcheck disable=SC2016 # literal bcrypt text and sed patterns
for bad in 'hunter2hunter2' "$(printf '%s' "$hash" | sed 's/\$10\$/$04$/')" "${hash}x" '$6$salt$abcdefghijklmnopqrstuvwxyz'; do
	{ plan; printf 'admin_password_hash=%s
' "$bad"; } >bad.conf
	must_reject guided_parse_plan "$PWD/bad.conf"
done
echo 'Provision reader, redaction and handoff checks passed.'
