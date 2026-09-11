from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import freesense_reuse
import batch_checkpoint
from resolve_multiarch_pin import score_candidates


class CandidateScoringTests(unittest.TestCase):
    def test_maximin_then_total_then_newest(self):
        base = "0" * 39
        candidates = [
            {"commit": base + "1", "committed_at": "2026-09-01T00:00:00Z", "accepted": {"amd64": 10, "arm64": 8}, "rejected": {}},
            {"commit": base + "2", "committed_at": "2026-09-02T00:00:00Z", "accepted": {"amd64": 9, "arm64": 9}, "rejected": {}},
            {"commit": base + "3", "committed_at": "2026-09-03T00:00:00Z", "accepted": {"amd64": 9, "arm64": 9}, "rejected": {"arm64": {"x": "OPTIONS mismatch"}}},
        ]
        self.assertEqual(score_candidates(candidates)["commit"], base + "3")


class PreviousReuseTests(unittest.TestCase):
    def package(self):
        return {"name": "curl", "abi": "FreeBSD:16:amd64", "osversion": 1600001,
                "origin": "ftp/curl", "version": "1", "options": {"TLS": True},
                "dependencies": {}, **{name: "a" * 64 for name in
                ("port_directory_sha256", "patches_sha256", "mk_sha256",
                 "make_configuration_sha256", "architecture_policy_sha256")}}

    def test_every_input_and_pin_must_match(self):
        item = self.package()
        self.assertEqual(freesense_reuse.reusable(item, dict(item), pin_unchanged=True),
                         (True, "exact provenance match"))
        changed = dict(item); changed["mk_sha256"] = "b" * 64
        self.assertEqual(freesense_reuse.reusable(item, changed, pin_unchanged=True)[0], False)
        self.assertEqual(freesense_reuse.reusable(item, item, pin_unchanged=False)[0], False)

    def test_unmodified_php_is_reusable_with_matching_provenance(self):
        item = self.package()
        item["name"] = "php85"
        item["origin"] = "lang/php85"
        self.assertEqual(freesense_reuse.reusable(item, dict(item), pin_unchanged=True),
                         (True, "exact provenance match"))

    def test_seed_conflicts_are_rebuilt(self):
        base = {"name": "curl", "version": "1", "origin": "ftp/curl",
                "abi": "FreeBSD:16:amd64", "sha256": "a" * 64}
        merged = freesense_reuse.merge_seeds([base], [{**base, "sha256": "b" * 64}])
        self.assertEqual(merged["packages"], [])
        self.assertIn("curl", merged["rejected"])


class CheckpointTests(unittest.TestCase):
    def test_namespace_and_cumulative_bytes_are_bound(self):
        key = batch_checkpoint.identity("arm64", "a" * 64, "b" * 64,
                                        "dependency-cost-v2", 8, 2, 1)
        self.assertIn("arm64", key)
        first = {"schema_version": batch_checkpoint.SCHEMA, "batch": 0,
                 "roots": ["devel/a"], "packages": [{"name": "a", "sha256": "c" * 64}]}
        second = {"schema_version": batch_checkpoint.SCHEMA, "batch": 1,
                  "roots": ["devel/a", "devel/b"], "packages": [
                      {"name": "a", "sha256": "c" * 64}, {"name": "b", "sha256": "d" * 64}]}
        batch_checkpoint.validate(first, first["roots"])
        batch_checkpoint.validate(second, second["roots"], first)
        second["packages"][0]["sha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "changed"):
            batch_checkpoint.validate(second, second["roots"], first)
