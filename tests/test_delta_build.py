"""Execute verify_delta_build from worker-common.sh against real repositories.

The guard answers one question: did this build make anything nobody sanctioned?
Poudriere resolves dependencies itself, so the bulk list does not bound the
output -- a stale seed or a dependency that did not match pulls a port into the
queue and it gets built from source. Nothing fails when that happens; the
repository still signs and publishes. Only this check makes it loud.
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


class DeltaBuildVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("sh") or shutil.which("bash")
        if cls.shell is None and os.name == "nt":
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            if candidate.exists():
                cls.shell = str(candidate)
        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        start = source.index("verify_delta_build() {")
        end = source.index("write_delta_bulk() {", start)
        # package_metadata shells out to pkg(8), which does not exist here. The
        # stub keeps the contract: "name|version" on stdout.
        cls.fragment = (
            "phase() { :; }\n"
            "package_metadata() { printf '%s|1.0\\n' \"$(basename \"$1\" .pkg)\"; }\n"
            + source[start:end]
        )

    def verify(self, built, delta_packages, mirrored):
        if self.shell is None:
            self.skipTest("POSIX shell is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory, "repo", "All")
            repository.mkdir(parents=True)
            for name in built:
                (repository / f"{name}.pkg").write_text("", encoding="utf-8")
            plan = Path(directory, "mirror-plan.json")
            plan.write_text(json.dumps({
                "packages": [{"name": name} for name in mirrored],
                "delta_packages": list(delta_packages),
            }), encoding="utf-8")
            script = (self.fragment
                      + f'\nverify_delta_build "{repository.parent.as_posix()}"'
                      + f' "{plan.as_posix()}"\n')
            return subprocess.run([self.shell, "-eu", "-c", script], text=True,
                                  capture_output=True, check=False)

    def test_a_build_of_only_sanctioned_packages_passes(self):
        result = self.verify(built=["unbound", "dnsmasq", "FreeSense-base"],
                             delta_packages=["unbound", "dnsmasq", "FreeSense-base"],
                             mirrored=["pkg", "perl5", "curl"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_packages_supplied_by_the_mirror_are_allowed_through(self):
        # The published repository keeps the lower layer; seeded packages
        # appear in the Poudriere repository and are not escapes.
        result = self.verify(built=["unbound", "pkg", "curl"],
                             delta_packages=["unbound"],
                             mirrored=["pkg", "perl5", "curl"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_package_nobody_sanctioned_fails_the_build(self):
        # texlive-base is neither ours to build nor in the mirror: Poudriere
        # decided to build it, which is the silent full-rebuild starting.
        result = self.verify(built=["unbound", "texlive-base"],
                             delta_packages=["unbound"],
                             mirrored=["pkg", "curl"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not sanction", result.stderr)
        self.assertIn("texlive-base", result.stderr)

    def test_every_escape_is_named_not_just_the_first(self):
        result = self.verify(built=["unbound", "texlive-base", "doxygen", "graphviz"],
                             delta_packages=["unbound"],
                             mirrored=["pkg"])
        self.assertNotEqual(result.returncode, 0)
        for name in ("texlive-base", "doxygen", "graphviz"):
            with self.subTest(name=name):
                self.assertIn(name, result.stderr)

    def test_a_build_that_produced_nothing_fails(self):
        # An empty repository would otherwise pass vacuously, which is how a
        # build that silently did nothing gets published.
        result = self.verify(built=[], delta_packages=["unbound"], mirrored=["pkg"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("produced no packages", result.stderr)

    def test_a_plan_allowing_nothing_fails(self):
        result = self.verify(built=["unbound"], delta_packages=[], mirrored=[])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allows no packages", result.stderr)


if __name__ == "__main__":
    unittest.main()
