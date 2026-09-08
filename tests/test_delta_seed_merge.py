"""Execute the real shell merge logic against disposable package fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeltaSeedMergeTests(unittest.TestCase):
    def run_merge(self, policy, conflicting=True, name="example"):
        shell = shutil.which("bash") or shutil.which("sh")
        if shell is None and os.name == "nt":
            candidate = Path("C:/Program Files/Git/bin/bash.exe")
            if candidate.exists():
                shell = str(candidate)
        if shell is None:
            self.skipTest("POSIX shell unavailable")
        common = (ROOT / "scripts/runner/worker-common.sh").read_text()
        function = common[common.index("merge_package() {"):common.index("publish_system_checkpoint() {")]
        script = """set -eu
package_metadata() { head -n 1 "$1"; }
sha256() { sha256sum "$2" | cut -d ' ' -f 1; }
""" + function + f"""
: >inventory
merge_package first/{name}.pkg destination inventory {policy}
merge_package second/{name}.pkg destination inventory {policy}
merge_package first/{name}.pkg destination inventory {policy}
merge_package first/unrelated.pkg destination inventory {policy}
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for part in ("first", "second", "destination"):
                (root / part).mkdir()
            first = f"{name}|1.0|devel/{name}||\npackage-one\n"
            second = f"{name}|2.0|devel/{name}||\npackage-two\n" if conflicting else first
            (root / "first" / f"{name}.pkg").write_text(first)
            (root / "second" / f"{name}.pkg").write_text(second)
            (root / "first/unrelated.pkg").write_text("unrelated|1.0|devel/unrelated||\nother\n")
            result = subprocess.run([shell, "-c", script], cwd=root, capture_output=True, text=True)
            files = {file.name for file in (root / "destination").iterdir()}
            return result, files

    def test_conflicting_variants_are_discarded_and_cannot_return_from_later_shards(self):
        result, files = self.run_merge("rebuild")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files, {"unrelated.pkg"})
        self.assertIn("authoritative rebuild", result.stdout)

    def test_identical_seed_bytes_are_preserved(self):
        result, files = self.run_merge("rebuild", conflicting=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files, {"example.pkg", "unrelated.pkg"})

    def test_sealed_repository_composition_remains_strict(self):
        result, _ = self.run_merge("identical")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting package", result.stderr)

    def test_conflicting_pkg_bootstrap_fails_closed(self):
        result, _ = self.run_merge("rebuild", name="pkg")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap", result.stderr)
