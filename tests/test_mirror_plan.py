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
SUFFIX = "-fs"


def upstream(name, *, version="1.0", origin=None, size=1024):
    return {"name": name, "version": version, "origin": origin or f"devel/{name}",
            "repopath": f"All/{name}-{version}.pkg", "sum": "0" * 64, "pkgsize": size}


def delta(build=(), take=("pkg", "alpha"), churn=None, collisions=(),
          components=None):
    build = list(build)
    if components is None:
        components = {"system": build, "optional": []}
    return {
        "schema_version": "freesense.delta-closure/v1", "abi": ABI,
        "build": [{"name": name, "origin": f"devel/{name}", "cause": "overlay"} for name in build],
        "take": sorted(take),
        "components": components,
        "collisions": [{"name": name, "origin": f"devel/{name}"} for name in collisions],
        "churn": churn or {},
        "counts": {"records": len(build) + len(take), "customized": len(build), "cascade": 0,
                   "build": len(build), "take": len(take), "causes": {}},
    }


def make(document, catalogue, **kwargs):
    options = {"architecture": "amd64", "catalog_sha256": CATALOG,
               "ports_commit": COMMIT, "ceilings": CEILINGS, "suffix": SUFFIX}
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

    def test_the_checked_in_policy_names_the_suffix_the_renaming_uses(self):
        document = json.loads((ROOT / "config" / "mirror-policy.json").read_text())
        mirror_plan.policy(document)
        self.assertRegex(document["delta_suffix"], r"^-[a-z0-9]+$")

    def test_a_policy_without_a_usable_suffix_is_rejected(self):
        document = json.loads((ROOT / "config" / "mirror-policy.json").read_text())
        for value in (None, "", "fs", "-FS", "-", "-fs!"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "delta suffix"):
                mirror_plan.policy(dict(document, delta_suffix=value))

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


class RenamingTests(unittest.TestCase):
    """The suffix only separates the layers if both shapes of the name are free."""

    def test_a_plan_is_refused_when_upstream_already_publishes_the_renamed_name(self):
        catalogue = [upstream("pkg"), upstream("alpha"), upstream("unbound"), upstream("unbound-fs")]
        with self.assertRaisesRegex(ValueError, r"already publishes the renamed packages.*unbound"):
            make(delta(build=["unbound"], take=["pkg", "alpha"]), catalogue)

    def test_a_collision_that_is_not_built_is_checked_too(self):
        # Cascade members are renamed by the same make.conf region, so an
        # upstream name for one of them is just as ambiguous.
        catalogue = [upstream("pkg"), upstream("alpha"), upstream("libgd-fs")]
        with self.assertRaisesRegex(ValueError, "already publishes the renamed packages"):
            make(delta(build=["ours"], take=["pkg", "alpha"], collisions=["libgd"]), catalogue)

    def test_a_plan_is_refused_when_upstream_publishes_a_freesense_name(self):
        # FreeSense's own ports stay unsuffixed because exact names are
        # load-bearing across the product, so upstream must not claim any.
        catalogue = [upstream("pkg"), upstream("alpha"), upstream("FreeSense-pkg-bind")]
        with self.assertRaisesRegex(ValueError, "FreeSense-named packages"):
            make(delta(build=["ours"], take=["pkg", "alpha"]), catalogue)

    def test_the_ordinary_catalogue_claims_neither_shape(self):
        catalogue = [upstream("pkg"), upstream("alpha"), upstream("unbound")]
        document = make(delta(build=["unbound"], take=["pkg", "alpha"]), catalogue)
        self.assertEqual(document["counts"]["delta"], 1)


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


class ComponentRootTests(unittest.TestCase):
    """The bulk list is addressed by origin; the closure records names."""

    def test_each_stage_gets_its_own_roots_resolved_to_origins(self):
        document = make(delta(build=["ours", "theirs"],
                              components={"system": ["ours"], "optional": ["theirs"]}),
                        [upstream("pkg"), upstream("alpha")])
        self.assertEqual(document["component_roots"],
                         {"system": ["devel/ours"], "optional": ["devel/theirs"]})

    def test_a_package_in_both_closures_is_a_root_of_both(self):
        # System builds it; Optional takes it from the System repository. The
        # split records the truth rather than picking a side.
        document = make(delta(build=["shared"],
                              components={"system": ["shared"], "optional": ["shared"]}),
                        [upstream("pkg"), upstream("alpha")])
        self.assertEqual(document["component_roots"]["system"], ["devel/shared"])
        self.assertEqual(document["component_roots"]["optional"], ["devel/shared"])

    def test_the_cascade_is_deliberately_nobody_s_root(self):
        # doxygen is in our layer only because something we customize reaches
        # it. Poudriere builds it as a dependency; putting it in a bulk list
        # would make each stage build the other stage's cascade.
        document = make(delta(build=["ours", "cascade"],
                              components={"system": ["ours"], "optional": []}),
                        [upstream("pkg"), upstream("alpha")])
        self.assertEqual(document["component_roots"], {"system": ["devel/ours"], "optional": []})
        self.assertIn("devel/cascade", document["delta_roots"])
        self.assertNotIn("devel/cascade", document["component_roots"]["system"])

    def test_two_packages_sharing_one_origin_are_refused(self):
        # package_requirements strips the flavour from PKGORIGIN, so devel/glib20
        # is the origin of both glib and glib-bootstrap. A bulk list addressed by
        # that origin builds the default flavour only; the sibling is then in
        # neither layer, because the delta claims it and the mirror excludes it.
        document = delta(build=["glib", "glib-bootstrap"],
                         components={"system": ["glib", "glib-bootstrap"], "optional": []})
        for item in document["build"]:
            item["origin"] = "devel/glib20"
        with self.assertRaisesRegex(ValueError, "sharing one flavourless origin"):
            make(document, [upstream("pkg"), upstream("alpha")])

    def test_the_same_origin_in_different_components_is_fine(self):
        # One package legitimately belongs to both closures; that is not a
        # collapse, it is System building it and Optional reusing it.
        document = make(delta(build=["shared"],
                              components={"system": ["shared"], "optional": ["shared"]}),
                        [upstream("pkg"), upstream("alpha")])
        self.assertEqual(document["component_roots"]["system"], ["devel/shared"])
        self.assertEqual(document["component_roots"]["optional"], ["devel/shared"])

    def test_a_component_naming_a_package_outside_the_delta_is_refused(self):
        with self.assertRaisesRegex(ValueError, "outside the delta"):
            make(delta(build=["ours"], components={"system": ["ours", "ghost"], "optional": []}),
                 [upstream("pkg"), upstream("alpha")])

    def test_a_closure_that_names_neither_component_is_refused(self):
        document = delta(build=["ours"])
        document["components"] = {"system": ["ours"]}
        with self.assertRaisesRegex(ValueError, "both components"):
            make(document, [upstream("pkg"), upstream("alpha")])

    def test_an_empty_split_is_refused(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            make(delta(build=["ours"], components={"system": [], "optional": []}),
                 [upstream("pkg"), upstream("alpha")])

    def test_the_measured_closures_split_the_way_the_farm_expects(self):
        import gzip
        import delta_closure
        root = ROOT / "tests/fixtures/delta-closure"
        make_conf = (root / "make.conf").read_text(encoding="utf-8")
        expected = {"amd64": (38, 54, 3), "arm64": (41, 53, 6)}
        for architecture, (system, optional, cascade) in expected.items():
            with self.subTest(architecture=architecture):
                records = json.loads(gzip.open(
                    root / f"requirements-{architecture}.json.gz", "rt", encoding="utf-8").read())
                catalogue = json.loads(gzip.open(
                    root / f"catalogue-{architecture}.json.gz", "rt", encoding="utf-8").read())
                closure = delta_closure.plan(records, catalogue, make_conf)
                roots = mirror_plan.component_roots(closure)
                union = set(roots["system"]) | set(roots["optional"])
                origins = {item["origin"] for item in closure["build"]}
                self.assertEqual(len(roots["system"]), system)
                self.assertEqual(len(roots["optional"]), optional)
                self.assertEqual(len(origins - union), cascade)


if __name__ == "__main__":
    unittest.main()
