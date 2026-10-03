"""Execute verify_mirror_plan_current from worker-common.sh against sample overlays.

A port an overlay changes is built from source. If the pinned mirror plan still
lists it as a prebuilt mirror package, the plan predates the overlay and the
build would fail hours later composing the repositories (softflowd, runs
37123254611 and 37131607890). The guard must refuse such a plan up front and
accept a plan that already treats every overlaid port as source-built.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MirrorPlanCurrentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("sh") or shutil.which("bash")
        if cls.shell is None and os.name == "nt":
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            if candidate.exists():
                cls.shell = str(candidate)
        if shutil.which("jq") is None:
            cls.shell = None
        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        start = source.index("verify_mirror_plan_current() {")
        end = source.index("\n}\n", start) + 3
        cls.fragment = "phase() { :; }\n" + source[start:end]

    def setUp(self) -> None:
        if self.shell is None:
            self.skipTest("needs sh and jq")
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def overlay(self, name: str, origins: list[str]) -> Path:
        root = self.work / name
        for origin in origins:
            (root / origin).mkdir(parents=True)
            (root / origin / "Makefile").write_text("PORTNAME=x\n", encoding="utf-8")
        # Files that are not ports must not count as overlaid ports.
        (root / "Mk").mkdir(parents=True, exist_ok=True)
        (root / "Mk" / "bsd.freesense-package.mk").write_text("", encoding="utf-8")
        return root

    def run_guard(self, mirror_origins: list[str], *overlays: Path) -> subprocess.CompletedProcess:
        plan = self.work / "mirror-plan.json"
        plan.write_text(json.dumps({"packages": [{"origin": o} for o in mirror_origins]}), encoding="utf-8")
        script = self.fragment + "verify_mirror_plan_current " + " ".join(
            f"'{p.as_posix()}'" for p in overlays) + "\n"
        return subprocess.run([self.shell, "-c", script], capture_output=True, text=True,
                              env={**os.environ, "MIRROR_PLAN_FILE": plan.as_posix()})

    def test_overlay_already_source_built_passes(self) -> None:
        packages = self.overlay("packages", ["net/haproxy", "net/FreeSense-pkg-haproxy"])
        result = self.run_guard(["net-mgmt/softflowd", "www/nginx"], packages)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overlaid_port_still_mirrored_is_refused(self) -> None:
        system = self.overlay("system", ["sysutils/filterlog"])
        packages = self.overlay("packages", ["net-mgmt/softflowd"])
        result = self.run_guard(["net-mgmt/softflowd", "www/nginx"], system, packages)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("net-mgmt/softflowd", result.stderr)
        self.assertIn("recut", result.stderr)
        self.assertNotIn("www/nginx", result.stderr)

    def test_flavoured_mirror_origin_counts(self) -> None:
        packages = self.overlay("packages", ["emulators/open-vm-tools"])
        result = self.run_guard(["emulators/open-vm-tools@nox11"], packages)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("emulators/open-vm-tools", result.stderr)

    def test_missing_overlay_directory_is_ignored(self) -> None:
        result = self.run_guard(["net-mgmt/softflowd"], self.work / "absent")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
