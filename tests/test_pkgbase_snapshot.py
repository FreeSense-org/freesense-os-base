"""Exercise workflow selection when upstream retains multiple dated snapshots."""
import os
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SnapshotSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        source = (ROOT / ".github/workflows/pin.yml").read_text()
        start = source.index("          pkgbase_snapshot() {")
        end = source.index("\n          amd_checksum=", start)
        cls.function = textwrap.dedent(source[start:end])

    def selection(self, index, listing):
        if not self.bash:
            self.skipTest("requires bash")
        script = "set -euo pipefail\nretry=()\n" + self.function + "\n" + r'''
curl() {
  if [[ $1 == https://example.test/ ]]; then
    printf '%s' "$INDEX"
  elif [[ $1 == https://example.test/20261117/ ]]; then
    printf '%s' "$LISTING"
  else
    return 7
  fi
}
pkgbase_snapshot https://example.test
'''
        return subprocess.run([self.bash, "-c", script], capture_output=True, text=True,
                              env={**os.environ, "INDEX": index, "LISTING": listing})

    def test_retained_snapshots_select_newest_dated_directory(self):
        name = "CHECKSUM.SHA256-20261117-abcdef123456-1234"
        result = self.selection('<a href="20261109/">old</a><a href="Latest/">alias</a>'
                                '<a href="20261117/">new</a>', name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), name)

    def test_duplicate_links_do_not_make_snapshot_ambiguous(self):
        name = "CHECKSUM.SHA256-20261117-abcdef123456-1234"
        result = self.selection('<a href="20261117/"></a><a href="20261117/"></a>',
                                name + " " + name)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_or_ambiguous_snapshot_is_rejected(self):
        cases = [('<a href="Latest/">alias</a>', ''),
                 ('<a href="20261117/">new</a>', ''),
                 ('<a href="20261117/">new</a>',
                  'CHECKSUM.SHA256-20261117-abcdef123456-1234 '
                  'CHECKSUM.SHA256-20261117-123456abcdef-1234')]
        for index, listing in cases:
            with self.subTest(index=index, listing=listing):
                self.assertNotEqual(self.selection(index, listing).returncode, 0)
