import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import observe_catalogue

COMMIT = "e26b1e4bc8ec94e73586539808ef21695232e8aa"


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.catalog = self.directory / "packagesite.pkg"
        self.catalog.write_bytes(b"signed-catalogue-bytes")

    def observe(self, records=None, worker=None, architecture="amd64"):
        records = records if records is not None else [{"name": "pkg"}, {"name": "curl"}]
        worker = worker or {"ports_sha": COMMIT, "osversion": 1600022}
        with mock.patch.object(observe_catalogue, "verify_catalogue", return_value=records) as verify, \
             mock.patch.object(observe_catalogue, "resolve_worker_tools", return_value=worker):
            document = observe_catalogue.observe(self.catalog, "b" * 64, architecture)
        return document, verify

    def test_it_reports_what_the_signed_catalogue_says(self):
        document, _ = self.observe()
        self.assertEqual(document["ports_commit"], COMMIT)
        self.assertEqual(document["osversion"], 1600022)
        self.assertEqual(document["abi"], "FreeBSD:16:amd64")
        self.assertEqual(document["package_count"], 2)
        self.assertTrue(document["signature_verified"])

    def test_the_catalogue_digest_is_taken_from_the_file_itself(self):
        document, _ = self.observe()
        self.assertEqual(document["catalog_sha256"],
                         hashlib.sha256(b"signed-catalogue-bytes").hexdigest())

    def test_the_signature_is_verified_against_the_pinned_trust_root(self):
        _, verify = self.observe()
        verify.assert_called_once_with(self.catalog, "b" * 64)

    def test_an_unverifiable_catalogue_stops_the_observation(self):
        with mock.patch.object(observe_catalogue, "verify_catalogue",
                               side_effect=ValueError("catalogue signer differs")):
            with self.assertRaisesRegex(ValueError, "signer differs"):
                observe_catalogue.observe(self.catalog, "b" * 64, "amd64")

    def test_each_architecture_reports_its_own_abi(self):
        document, _ = self.observe(architecture="arm64")
        self.assertEqual(document["abi"], "FreeBSD:16:aarch64")
        self.assertEqual(document["architecture"], "arm64")


if __name__ == "__main__":
    unittest.main()
