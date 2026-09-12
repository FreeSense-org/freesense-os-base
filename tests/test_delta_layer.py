from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeltaLayerVerificationTests(unittest.TestCase):
    """Execute verify_delta_layer from worker-common.sh against real name lists."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("sh") or shutil.which("bash")
        if cls.shell is None and os.name == "nt":
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            if candidate.exists():
                cls.shell = str(candidate)
        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        start = source.index("verify_delta_layer() {")
        end = source.index("sign_repository() {", start)
        cls.fragment = "phase() { :; }\n" + source[start:end]

    def verify(self, published, sealed, mirrored) -> subprocess.CompletedProcess[str]:
        if self.shell is None:
            self.skipTest("POSIX shell is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name, names in (("published", published), ("sealed", sealed), ("mirrored", mirrored)):
                path = Path(directory, name)
                path.write_text("".join(f"{value}\n" for value in names), encoding="utf-8")
                paths.append(path.as_posix())
            script = self.fragment + "\nverify_delta_layer " + " ".join(f'"{path}"' for path in paths)
            return subprocess.run([self.shell, "-eu", "-c", script], text=True,
                                  capture_output=True, check=False)

    def test_a_layer_inside_the_seal_and_clear_of_the_mirror_passes(self):
        result = self.verify(["squid-fs", "unbound-fs", "FreeSense"],
                             ["squid-fs", "unbound-fs", "FreeSense", "nginx-fs"],
                             ["squid", "unbound", "pkg", "perl5"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_package_outside_the_sealed_delta_fails(self):
        # Poudriere decided a seeded mirror package was stale and rebuilt it.
        result = self.verify(["squid-fs", "libxml2"], ["squid-fs"], ["pkg"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the sealed delta", result.stderr)
        self.assertIn("libxml2", result.stderr)

    def test_a_package_shadowing_the_mirror_by_name_fails(self):
        # The suffix did not take, so both layers would offer "squid".
        result = self.verify(["squid"], ["squid", "unbound-fs"], ["squid", "pkg"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shadow the mirror", result.stderr)
        self.assertIn("squid", result.stderr)

    def test_an_empty_layer_is_a_failure_not_a_pass(self):
        result = self.verify([], ["squid-fs"], ["pkg"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("published no packages", result.stderr)

    def test_unsorted_and_duplicated_input_is_handled(self):
        result = self.verify(["unbound-fs", "squid-fs", "squid-fs"],
                             ["squid-fs", "unbound-fs"], ["perl5", "pkg"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_escaped_package_is_named_not_just_the_first(self):
        result = self.verify(["alpha", "beta", "squid-fs"], ["squid-fs"], ["pkg"])
        self.assertNotEqual(result.returncode, 0)
        for name in ("alpha", "beta"):
            self.assertIn(name, result.stderr)


if __name__ == "__main__":
    unittest.main()
