from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkerVersionValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("sh") or shutil.which("bash")
        if cls.shell is None and os.name == "nt":
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            if candidate.exists():
                cls.shell = str(candidate)

    def validate(self, version: str, train: str) -> subprocess.CompletedProcess[str]:
        if self.shell is None:
            self.skipTest("POSIX shell is unavailable")
        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        start = source.index("printf '%s\\n' \"${PRODUCT_VERSION}\"")
        end = source.index('if [ "${STAGE}" = iso ]', start)
        fragment = source[start:end]
        return subprocess.run(
            [self.shell, "-eu", "-c", fragment],
            text=True,
            capture_output=True,
            env={**os.environ, "PRODUCT_VERSION": version, "PACKAGE_TRAIN": train},
            check=False,
        )

    def test_accepts_configured_stable_and_development_versions(self) -> None:
        for version, train in (
            ("1.0.5-RELEASE", "1.0"),
            ("1.1.0-DEVELOPMENT", "1.1"),
        ):
            with self.subTest(version=version):
                result = self.validate(version, train)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_mismatched_package_train(self) -> None:
        result = self.validate("1.1.0-DEVELOPMENT", "1.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match package train", result.stderr)
