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

    def test_resolve_populates_catalog_sha_and_records(self):
        from unittest.mock import patch
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for arch, dist in [("amd64", "amd64"), ("arm64", "arm64")]:
                arch_dir = root / arch
                arch_dir.mkdir()
                (arch_dir / "MANIFEST").write_text("base.txz\t" + "a" * 64 + "\t1\n")
                (arch_dir / "packagesite.pkg").write_bytes(b"dummy package catalog")
                fn = f"FreeBSD-16.0-CURRENT-{dist}-BASIC-CLOUDINIT-20260907-abcdef123456-1-ufs.qcow2.xz" if arch == "amd64" else f"FreeBSD-16.0-CURRENT-{dist}-aarch64-BASIC-CLOUDINIT-20260907-abcdef123456-1-ufs.qcow2.xz"
                (arch_dir / "CHECKSUM.SHA256").write_text(f"SHA256 ({fn}) = {'c' * 64}\n")

            prev = {"valid_until": "2026-09-07T00:00:00Z"}
            meta = {
                "revision": "abcdef123456", "build_date": "20260907", "source_commit": "1" * 40,
                "osversion": 1600020, "trusted_key_sha256": "d" * 64,
                "dist_urls": {"amd64": "https://example.invalid/dist/amd64", "arm64": "https://example.invalid/dist/arm64"},
                "vm_urls": {"amd64": "https://example.invalid/vm/amd64", "arm64": "https://example.invalid/vm/arm64"},
            }
            sources = {"freesense": "2" * 40, "system_ports": "3" * 40, "packages": "4" * 40, "os_base": "5" * 40}

            with patch.object(module, "verify_catalogue", return_value=[{"name": "test"}]), \
                 patch.object(module, "resolve_worker_tools", return_value={"ports_sha": "6" * 40, "osversion": 1600020}):
                res = module.resolve(prev, root, meta, sources, security_rollover=True, now=datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc))
                self.assertEqual(res["schema_version"], "freesense.freebsd-pin-common/v1")
                self.assertEqual(res["freebsd_ports"]["commit"], "6" * 40)
                for arch in ("amd64", "arm64"):
                    self.assertIn("sha256", res["targets"][arch]["package_catalog"])
                    self.assertEqual(len(res["targets"][arch]["package_catalog"]["sha256"]), 64)


if __name__ == "__main__": unittest.main()
