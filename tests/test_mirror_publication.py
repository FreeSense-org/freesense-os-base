"""Verify isolated mirror publications retain the observed package identity."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from multiarch_pin import digest
from mirror_publication import publication_plan
import mirror_fetch


def observed():
    payload = b"verified upstream package"
    data = {"schema_version": "freesense.mirror-plan/v1", "abi": "FreeBSD:16:amd64",
            "architecture": "amd64", "ports_commit": "a" * 40, "catalog_sha256": "b" * 64,
            "packages": [{"name": "pkg", "version": "1.0", "origin": "ports-mgmt/pkg",
                          "abi": "FreeBSD:16:amd64", "file": "All/pkg-1.0.pkg",
                          "upstream_path": "All/pkg-1.0.pkg", "size": len(payload),
                          "upstream_checksum": hashlib.sha256(payload).hexdigest(),
                          "catalog_sha256": "b" * 64}]}
    data["fingerprint"] = digest(data)
    return data


class PublicationIdentityTests(unittest.TestCase):
    def test_new_runs_and_attempts_use_distinct_immutable_namespaces(self):
        original = observed()
        plans = [publication_plan(original, value) for value in ("100.1", "100.2", "101.1")]
        self.assertEqual(len({p["fingerprint"] for p in plans}), 3)
        self.assertNotIn(original["fingerprint"], {p["fingerprint"] for p in plans})

    def test_same_attempt_is_reproducible_without_mutating_observation(self):
        original = observed()
        before = copy.deepcopy(original)
        published = publication_plan(original, "100.1")
        self.assertEqual(published, publication_plan(original, "100.1"))
        self.assertEqual(original, before)
        self.assertEqual(published["observation_fingerprint"], original["fingerprint"])
        self.assertEqual(published["packages"], original["packages"])

    def test_corrupted_observation_is_rejected(self):
        original = observed()
        original["packages"][0]["version"] = "2.0"
        with self.assertRaisesRegex(ValueError, "identity"):
            publication_plan(original, "100.1")

    def test_resealing_a_publication_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "original observation"):
            publication_plan(publication_plan(observed(), "100.1"), "100.2")

    def test_invalid_publication_ids_are_rejected(self):
        for value in ("", "100", "0.1", "100.0", "100.1/other", "100.1\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publication_plan(observed(), value)

    def test_materialization_still_verifies_observed_package_bytes(self):
        published = publication_plan(observed(), "100.1")

        def fetch_one(url, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"verified upstream package")

        with tempfile.TemporaryDirectory() as directory:
            result = mirror_fetch.fetch(published, Path(directory), fetch_one=fetch_one)
            self.assertEqual(result["plan_fingerprint"], published["fingerprint"])
            self.assertEqual(len(result["packages"]), 1)
