"""tools/lab_accept.py: the parts that can be checked without the lab."""
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.

from pathlib import Path, PureWindowsPath
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab_accept


class LabAcceptance(unittest.TestCase):
    def test_ssh_paths_survive_the_jump_shell(self):
        # ssh passes -F on to the jump host through a shell, which eats backslashes.
        converted = lab_accept.key_file(Path(__file__))
        self.assertNotIn("\\", converted)
        self.assertTrue(converted.startswith("/"))
        if isinstance(Path(__file__), type(Path())) and PureWindowsPath(__file__).drive:
            self.assertEqual("/" + PureWindowsPath(__file__).drive[0].lower(), converted[:2])

    def test_only_the_two_lab_machines_are_driven(self):
        self.assertEqual({150: "fs2-a", 152: "fs2-client"}, lab_accept.NAMES)
        self.assertEqual({150, 152}, set(lab_accept.BRIDGES))
        # The firewall's LAN and the test networks are never on the home network.
        self.assertEqual(["vmbr2", "vmbr2"], [lab_accept.BRIDGES[150][nic] for nic in ("net1", "net2")])


if __name__ == "__main__":
    unittest.main()
