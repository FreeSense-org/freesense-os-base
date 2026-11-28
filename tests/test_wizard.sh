#!/bin/sh
# shellcheck disable=SC1091
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
# Run from the guided scripts directory using only the in-memory rehearsal adapter.
set -eu
. ./backend.sh
. ./rehearsal.sh
. ./wizard.sh
export TERM=dumb
guided_preflight
(
	if guided_wizard <<'INPUT'
start
next
da1
back
da0
standalone
later
confirm
wrong line
back
back
back
back
back
exit
INPUT
	then exit 1; fi
	[ "$FS_INSTALL_STATE" = cancelled ] && [ -z "$FS_INSTALL_TRACE" ]
)
guided_wizard <<'INPUT'
start
next
da1
fleet
em1
confirm
INSTALL /dev/da1 | DEMO-002 | Demonstration disk B | 17179869184
INPUT
[ "$FS_INSTALL_STATE" = 'done' ] && [ "$FS_INSTALL_USE_SEED" = yes ] &&
	[ "$FS_INSTALL_LAN" = em1 ] && [ "$FS_INSTALL_TRACE" = 'verify blank-da1 write-da1 readback-da1 handoff-da1' ]
