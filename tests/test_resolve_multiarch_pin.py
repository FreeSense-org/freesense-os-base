import importlib.util
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("resolve_multiarch_pin", ROOT / "scripts" / "resolve_multiarch_pin.py")
module = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(module)


class ResolvePinTests(unittest.TestCase):
    def test_extracts_exact_manifest_and_architecture_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "MANIFEST"
            manifest.write_text("base.txz\t" + "a" * 64 + "\t1\n")
            self.assertEqual(module.manifest_sha(manifest, "base.txz"), "a" * 64)
            checksums = root / "CHECKSUM.SHA256"
            filename = "FreeBSD-16.0-CURRENT-arm64-aarch64-BASIC-CLOUDINIT-20260907-abcdef123456-1-ufs.qcow2.xz"
            checksums.write_text(f"SHA256 ({filename}) = {'b' * 64}\n")
            result = module.image(checksums, "https://example.invalid/Latest", "arm64", "aarch64", "20260907", "abcdef123456")
            self.assertEqual(result, {"url": "https://example.invalid/Latest/" + filename, "sha256": "b" * 64})

    def test_rejects_ambiguous_upstream_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MANIFEST").write_text("base.txz\t" + "a" * 64 + "\t1\nbase.txz\t" + "b" * 64 + "\t1\n")
            with self.assertRaises(ValueError): module.manifest_sha(root / "MANIFEST", "base.txz")


if __name__ == "__main__": unittest.main()
