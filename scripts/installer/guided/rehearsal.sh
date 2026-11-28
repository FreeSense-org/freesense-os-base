# shellcheck disable=SC2034
# State variables are shared by the sourced installer modules.
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# Synthetic adapter. It has no device or filesystem operations.
fs_adapter_init() {
	FS_INSTALL_FIRMWARE=UEFI
	FS_INSTALL_TRACE=''
	FS_FIXTURE_TARGETS=$(printf 'da0\tDEMO-001\tDemonstration disk A\t8589934592\nda1\tDEMO-002\tDemonstration disk B\t17179869184')
	FS_FIXTURE_MACHINE=8e2d58c1-0f31-45ac-9d9e-7662eab463ad
	FS_FIXTURE_PLAN=absent
	FS_FIXTURE_PLAN_ID=DEMO-002
	FS_FIXTURE_PLAN_UUID=$FS_FIXTURE_MACHINE
	FS_FIXTURE_SOURCE=fixture-v1
	FS_FIXTURE_SEED=protected
	FS_FIXTURE_FAIL=''
}
fs_adapter_scan() {
	FS_INSTALL_TARGETS=$FS_FIXTURE_TARGETS
	FS_INSTALL_EXCLUSIONS=$(printf 'cd0\tInstaller boot medium\nda2\tRead-only provisioning medium\nda3\tExisting partition table')
	FS_INSTALL_SEED_STATUS=$FS_FIXTURE_SEED
	FS_INSTALL_PLAN_STATUS=$FS_FIXTURE_PLAN
	FS_INSTALL_MACHINE_UUID=$FS_FIXTURE_MACHINE
	FS_INSTALL_PLAN_UUID=$FS_FIXTURE_PLAN_UUID
	FS_INSTALL_PLAN_ID=$FS_FIXTURE_PLAN_ID
	FS_INSTALL_SOURCE_STAMP=$FS_FIXTURE_SOURCE
	FS_INSTALL_FLEET_MODE=attended
	FS_INSTALL_TRANSPORT=tunnel
	FS_INSTALL_INTERFACES='em0 em1'
}
fs_rehearse() {
	FS_INSTALL_TRACE="${FS_INSTALL_TRACE:+$FS_INSTALL_TRACE }$1"
	[ "$FS_FIXTURE_FAIL" != "$1" ]
}
fs_adapter_verify_payload() { fs_rehearse verify; }
fs_adapter_blank() { fs_rehearse "blank-$1"; }
fs_adapter_write() { fs_rehearse "write-$1"; }
fs_adapter_readback() { fs_rehearse "readback-$1"; }
fs_adapter_handoff() { fs_rehearse "handoff-$1"; }
fs_adapter_cleanup() { :; }

