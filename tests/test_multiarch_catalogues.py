import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_multiarch_catalogues as catalogues


def record(name="rust", deps=None):
    return {"name": name, "version": "1.0", "origin": "lang/" + name,
            "arch": "FreeBSD:16:amd64", "repopath": f"All/{name}.pkg", "sum": "a" * 64,
            "deps": deps or {}}


class CatalogueTests(unittest.TestCase):
    def test_checks_combined_dependency_closure(self):
        system = catalogues.inventory([record()], "FreeBSD:16:amd64")
        optional = catalogues.inventory([record("app", {"rust": {"version": "1.0", "origin": "lang/rust"}})],
                                        "FreeBSD:16:amd64")
        catalogues.verify_closure(system, optional)
        for modified in ({}, {"rust": {**record(), "version": "2.0"}}):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                catalogues.verify_closure(modified, optional)

    def test_rejects_foreign_abi_duplicate_names_and_escaping_paths(self):
        for records in ([record(), record()], [{**record(), "arch": "FreeBSD:16:aarch64"}],
                        [{**record(), "repopath": "All/../rust.pkg"}], []):
            with self.subTest(records=records), self.assertRaises(ValueError):
                catalogues.inventory(records, "FreeBSD:16:amd64")

    def test_rejects_conflicting_component_packages(self):
        with self.assertRaisesRegex(ValueError, "disagree"):
            catalogues.verify_closure({"rust": record()}, {"rust": {**record(), "version": "2.0"}})

    def test_signature_uses_frozen_key_and_catalogue_digest(self):
        data = b'{"name":"rust"}\n'
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "public.pem"
            key.write_bytes(b"trusted-public-key")
            trusted = hashlib.sha256(key.read_bytes()).hexdigest()
            with mock.patch.object(catalogues.subprocess, "check_output", side_effect=[data, b"signature"]), \
                    mock.patch.object(catalogues.subprocess, "run") as verify:
                self.assertEqual(catalogues.verify_signature(b"archive", trusted, key), [{"name": "rust"}])
                self.assertEqual(verify.call_args.kwargs["input"], hashlib.sha256(data).hexdigest().encode())
                self.assertTrue(verify.call_args.kwargs["check"])
                self.assertIn(str(key), verify.call_args.args[0])
            with mock.patch.object(catalogues.subprocess, "check_output") as extract:
                with self.assertRaisesRegex(ValueError, "frozen"):
                    catalogues.verify_signature(b"archive", "0" * 64, key)
                extract.assert_not_called()
            with mock.patch.object(catalogues.subprocess, "check_output", side_effect=[data, b"bad-signature"]), \
                    mock.patch.object(catalogues.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "openssl")):
                with self.assertRaises(subprocess.CalledProcessError):
                    catalogues.verify_signature(b"archive", trusted, key)
