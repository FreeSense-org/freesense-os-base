import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mirror_plan

ROOT = Path(__file__).resolve().parents[1]
ABI = "FreeBSD:16:amd64"
COMMIT = "5052095ee6f48f63c4fd38dd9ffb2632513036d3"
CATALOG = "a" * 64
CEILINGS = {"amd64": {"max_delta": 10, "min_mirror": 2, "max_churn": 5},
            "arm64": {"max_delta": 10, "min_mirror": 2, "max_churn": 5}}


def upstream(name, *, version="1.0", origin=None, size=1024):
    return {"name": name, "version": version, "origin": origin or f"devel/{name}",
            "repopath": f"All/{name}-{version}.pkg", "sum": "0" * 64, "pkgsize": size}


def delta(build=(), take=("pkg", "alpha"), churn=None, collisions=()):
    return {
        "schema_version": "freesense.delta-closure/v1", "abi": ABI,
        "build": [{"name": name, "origin": f"devel/{name}", "cause": "overlay"} for name in build],
        "take": sorted(take),
        "components": {"system": [], "optional": []},
        "collisions": [{"name": name, "origin": f"devel/{name}"} for name in collisions],
        "churn": churn or {},
        "counts": {"records": len(build) + len(take), "customized": len(build), "cascade": 0,
                   "build": len(build), "take": len(take), "causes": {}},
    }


def make(document, catalogue, **kwargs):
    options = {"architecture": "amd64", "catalog_sha256": CATALOG,
               "ports_commit": COMMIT, "ceilings": CEILINGS}
    options.update(kwargs)
    return mirror_plan.plan(document, catalogue, **options)


class PolicyTests(unittest.TestCase):
    def test_the_checked_in_policy_is_valid_and_covers_both_architectures(self):
        ceilings = mirror_plan.policy(json.loads((ROOT / "config" / "mirror-policy.json").read_text()))
        self.assertEqual(set(ceilings), {"amd64", "arm64"})
        # The measured delta must sit under the ceiling with real headroom, or
        # the tripwire fires on ordinary upstream movement.
        for architecture, measured in (("amd64", 92), ("arm64", 95)):
            self.assertGreater(ceilings[architecture]["max_delta"], measured)

    def test_incomplete_policy_is_rejected(self):
        for document in (
            {"schema_version": "wrong", "ceilings": {}},
            {"schema_version": "freesense.mirror-policy/v1", "ceilings": {"amd64": {}}},
            {"schema_version": "freesense.mirror-policy/v1",
             "ceilings": {"amd64": {"max_delta": 0, "min_mirror": 1, "max_churn": 1},
                          "arm64": {"max_delta": 1, "min_mirror": 1, "max_churn": 1}}},
        ):
            with self.subTest(document=document), self.assertRaises(ValueError):
                mirror_plan.policy(document)


class MembershipTests(unittest.TestCase):
    def test_the_mirror_is_exactly_the_closure_we_do_not_build(self):
        catalogue = [upstream("pkg"), upstream("alpha"), upstream("unrelated")]
        document = make(delta(build=["ours"], take=["pkg", "alpha"]), catalogue)
        self.assertEqual([item["name"] for item in document["packages"]], ["alpha", "pkg"])
        self.assertEqual(document["counts"], {"mirror": 2, "delta": 1, "churn": 0})

    def test_each_entry_carries_what_a_worker_needs_to_fetch_and_verify_it(self):
        document = make(delta(take=["pkg", "alpha"]), [upstream("pkg"), upstream("alpha", size=4096)])
        alpha = next(item for item in document["packages"] if item["name"] == "alpha")
        self.assertEqual(alpha, {
            "name": "alpha", "version": "1.0", "origin": "devel/alpha", "abi": ABI,
            "file": "All/alpha-1.0.pkg", "upstream_path": "All/alpha-1.0.pkg",
            "upstream_checksum": "0" * 64, "size": 4096, "catalog_sha256": CATALOG,
        })

    def test_a_package_on_both_layers_is_a_contradiction(self):
        with self.assertRaisesRegex(ValueError, "both layers"):
            make(delta(build=["alpha"], take=["pkg", "alpha"]), [upstream("pkg"), upstream("alpha")])

    def test_closure_package_upstream_does_not_publish_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "does not publish"):
            make(delta(take=["pkg", "ghost"]), [upstream("pkg")])

    def test_ambiguous_upstream_name_is_not_mirrored(self):
        catalogue = [upstream("pkg"), upstream("alpha"), upstream("alpha", version="2.0")]
        with self.assertRaisesRegex(ValueError, "does not publish"):
            make(delta(take=["pkg", "alpha"]), catalogue)

    def test_mirror_without_pkg_cannot_start_a_build(self):
        with self.assertRaisesRegex(ValueError, "cannot start without"):
            make(delta(take=["alpha", "beta"]), [upstream("alpha"), upstream("beta")])


class CeilingTests(unittest.TestCase):
    def test_an_oversized_delta_stops_the_pin(self):
        build = [f"p{index}" for index in range(11)]
        catalogue = [upstream("pkg"), upstream("alpha")]
        with self.assertRaisesRegex(ValueError, "exceeds the 10 ceiling"):
            make(delta(build=build), catalogue)

    def test_an_undersized_mirror_stops_the_pin(self):
        with self.assertRaisesRegex(ValueError, "below the 2 floor"):
            make(delta(take=["pkg"]), [upstream("pkg")])

    def test_excessive_upstream_drift_stops_the_pin(self):
        churn = {f"p{index}": {"ports": "2.0", "mirror": "1.0"} for index in range(6)}
        with self.assertRaisesRegex(ValueError, "drift"):
            make(delta(churn=churn), [upstream("pkg"), upstream("alpha")])


class IdentityTests(unittest.TestCase):
    def test_the_same_inputs_always_produce_the_same_fingerprint(self):
        catalogue = [upstream("pkg"), upstream("alpha")]
        first = make(delta(), catalogue)
        second = make(delta(), list(reversed(catalogue)))
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertRegex(first["fingerprint"], r"^[0-9a-f]{64}$")

    def test_a_different_upstream_version_is_a_different_mirror(self):
        first = make(delta(), [upstream("pkg"), upstream("alpha")])
        second = make(delta(), [upstream("pkg"), upstream("alpha", version="1.1")])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_the_delta_is_bound_into_the_mirror_identity(self):
        catalogue = [upstream("pkg"), upstream("alpha")]
        first = make(delta(collisions=["net-snmp"]), catalogue)
        second = make(delta(collisions=["net-snmp", "libgd"]), catalogue)
        self.assertNotEqual(first["delta_sha256"], second["delta_sha256"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_a_mismatched_architecture_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "different architecture"):
            make(delta(), [upstream("pkg"), upstream("alpha")], architecture="arm64")


if __name__ == "__main__":
    unittest.main()
