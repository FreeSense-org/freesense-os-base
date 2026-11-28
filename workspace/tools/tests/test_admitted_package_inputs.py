"""Exercise the package fingerprint CLI against the actual admitted component layout."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import subprocess
import sys
import unittest


class AdmittedInputsTest(unittest.TestCase):
    def test_both_local_architectures_have_independent_system_and_package_inputs(self):
        root = Path(__file__).resolve().parents[2]
        planner = root / "freesense-os-base/scripts/component_inputs.py"
        plans = []
        for architecture in ("amd64", "aarch64"):
            output = subprocess.check_output([
                sys.executable, "-B", str(planner), "--freebsd-pin", "a" * 64, "--architecture", architecture
            ], text=True)
            plan = json.loads(output)
            for name in ("system", "packages"):
                self.assertRegex(plan[name], r"^[0-9a-f]{64}$")
            self.assertNotEqual(plan["system"], plan["packages"])
            plans.append(plan)
        for name in ("system", "packages"):
            self.assertNotEqual(plans[0][name], plans[1][name])
