import gzip
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import delta_closure

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "delta-closure"
ABI = "FreeBSD:16:amd64"
MAKE_CONF = "dns_dnsmasq_UNSET_FORCE=\tDNSSEC\nnet_nss_ldap_SET_FORCE=\tKERBEROS\n"


def record(name, origin, *, version="1.0", deps=(), **flags):
    value = {
        "name": name, "origin": origin, "version": version, "abi": ABI,
        "options": {}, "deps": {dependency: {"origin": "misc/" + dependency, "version": "1.0"}
                                for dependency in deps},
        "overlay": False, "custom_patches": False, "non_options_knobs": False,
        "kernel_sensitive": False, "base_package": False,
    }
    value.update(flags)
    return value


def requirements(packages, *, system=None, packages_roots=None):
    return {
        "schema_version": "freesense.package-requirements/v1", "abi": ABI,
        "roots": {"system": list(system or [packages[0]["name"]]),
                  "packages": list(packages_roots or [packages[-1]["name"]])},
        "packages": packages,
    }


def catalogue(packages, *, absent=(), versions=None, options=None):
    return [{"name": package["name"],
             "version": (versions or {}).get(package["name"], package["version"]),
             "options": (options or {}).get(package["name"], package["options"])}
            for package in packages if package["name"] not in absent]


class OverrideParsingTests(unittest.TestCase):
    def test_option_knobs_become_origins_and_other_assignments_do_not(self):
        text = ("net-mgmt_net-snmp_UNSET_FORCE=\tPERL\n"
                "net_nss_ldap_SET_FORCE=\tKERBEROS\n"
                "mail_pear-Mail_SET_FORCE=\tDOCS\n"
                "DEFAULT_VERSIONS=\tphp=8.5\n"
                "OPTIONS_UNSET=\tX11\n")
        self.assertEqual(delta_closure.overrides(text),
                         {"net-mgmt/net-snmp", "net/nss_ldap", "mail/pear-Mail"})

    def test_port_directory_keeps_underscores_after_the_category(self):
        # A category never contains an underscore, so net_nss_ldap is net/nss_ldap
        # and not net/nss with a stray suffix.
        self.assertEqual(delta_closure.overrides("net_nss_ldap_UNSET_FORCE=\tX\n"), {"net/nss_ldap"})

    def test_unaudited_assignment_fails_closed(self):
        with self.assertRaises(ValueError):
            delta_closure.overrides("SOMETHING_ARBITRARY=\tyes\n")


class ClassificationTests(unittest.TestCase):
    def test_overlaid_leaf_is_ours_and_forces_nothing(self):
        packages = [record("alpha", "devel/alpha"), record("lua54", "lang/lua54", overlay=True)]
        document = delta_closure.plan(requirements(packages), catalogue(packages), MAKE_CONF)
        self.assertEqual([entry["name"] for entry in document["build"]], ["lua54"])
        self.assertEqual(document["counts"]["cascade"], 0)

    def test_overlaid_library_drags_every_dependent_into_our_layer(self):
        packages = [
            record("lua54", "lang/lua54", overlay=True),
            record("texlive-base", "print/texlive-base", deps=["lua54"]),
            record("tex-formats", "print/tex-formats", deps=["texlive-base"]),
            record("unrelated", "devel/unrelated"),
        ]
        document = delta_closure.plan(requirements(packages), catalogue(packages), MAKE_CONF)
        self.assertEqual([entry["name"] for entry in document["build"]],
                         ["lua54", "tex-formats", "texlive-base"])
        self.assertEqual({entry["name"]: entry["cause"] for entry in document["build"]}["tex-formats"],
                         "reverse-dependency")
        self.assertEqual(document["counts"], {
            "records": 4, "customized": 1, "cascade": 2, "build": 3, "take": 1,
            "causes": {"overlay": 1},
        })

    def test_options_override_without_any_flag_is_ours(self):
        packages = [record("dnsmasq", "dns/dnsmasq"), record("alpha", "devel/alpha")]
        document = delta_closure.plan(requirements(packages), catalogue(packages), MAKE_CONF)
        self.assertEqual({entry["name"]: entry["cause"] for entry in document["build"]},
                         {"dnsmasq": "options-override"})

    def test_package_upstream_does_not_publish_is_ours(self):
        packages = [record("alpha", "devel/alpha"), record("kea", "net/kea")]
        document = delta_closure.plan(requirements(packages), catalogue(packages, absent={"kea"}), MAKE_CONF)
        self.assertEqual({entry["name"]: entry["cause"] for entry in document["build"]},
                         {"kea": "absent-upstream"})

    def test_upstream_own_extra_patches_do_not_make_a_package_ours(self):
        packages = [record("alpha", "devel/alpha"), record("arj", "archivers/arj", custom_patches=True)]
        document = delta_closure.plan(requirements(packages), catalogue(packages), MAKE_CONF)
        self.assertEqual(document["build"], [])

    def test_measured_option_divergence_is_caught_without_a_declared_override(self):
        packages = [record("alpha", "devel/alpha"), record("curl", "ftp/curl")]
        packages[1]["options"] = {"GSSAPI_BASE": True}
        published = catalogue(packages, options={"curl": {"GSSAPI_BASE": False}})
        document = delta_closure.plan(requirements(packages), published, MAKE_CONF)
        self.assertEqual({entry["name"]: entry["cause"] for entry in document["build"]},
                         {"curl": "options-divergence"})

    def test_option_divergence_across_a_churned_version_is_not_a_divergence(self):
        # Option sets move between releases, so comparing ours at 8.22 against
        # upstream's at 8.21 says nothing about whether we customized anything.
        packages = [record("alpha", "devel/alpha"), record("curl", "ftp/curl", version="8.22.0")]
        packages[1]["options"] = {"GSSAPI_BASE": True}
        published = catalogue(packages, versions={"curl": "8.21.0"},
                              options={"curl": {"GSSAPI_BASE": False}})
        document = delta_closure.plan(requirements(packages), published, MAKE_CONF)
        self.assertEqual(document["build"], [])
        self.assertEqual(set(document["churn"]), {"curl"})

    def test_ambiguous_upstream_name_counts_as_unpublished(self):
        packages = [record("alpha", "devel/alpha"), record("sevenzip", "archivers/7-zip")]
        duplicated = catalogue(packages) + [{"name": "sevenzip", "version": "9.9"}]
        document = delta_closure.plan(requirements(packages), duplicated, MAKE_CONF)
        self.assertEqual({entry["name"] for entry in document["build"]}, {"sevenzip"})


class ChurnTests(unittest.TestCase):
    def test_version_drift_alone_never_widens_the_delta(self):
        packages = [record("alpha", "devel/alpha", version="1.0"),
                    record("beta", "devel/beta", version="2.0")]
        document = delta_closure.plan(
            requirements(packages), catalogue(packages, versions={"beta": "2.1"}), MAKE_CONF)
        self.assertEqual(document["build"], [])
        self.assertEqual(document["churn"], {"beta": {"ports": "2.0", "mirror": "2.1"}})

    def test_churn_is_not_reported_for_packages_we_build(self):
        packages = [record("alpha", "devel/alpha"),
                    record("lua54", "lang/lua54", version="5.4", overlay=True)]
        document = delta_closure.plan(
            requirements(packages), catalogue(packages, versions={"lua54": "5.5"}), MAKE_CONF)
        self.assertEqual(document["churn"], {})


class ComponentTests(unittest.TestCase):
    def test_build_set_is_split_across_the_system_and_optional_closures(self):
        packages = [
            record("system-root", "security/FreeSense-system", deps=["shared"]),
            record("optional-root", "net/FreeSense-pkg-Avahi", deps=["shared", "optional-only"]),
            record("shared", "devel/shared", overlay=True),
            record("optional-only", "net/optional-only", overlay=True),
        ]
        document = delta_closure.plan(
            requirements(packages, system=["system-root"], packages_roots=["optional-root"]),
            catalogue(packages), MAKE_CONF)
        self.assertEqual(document["components"]["system"], ["shared", "system-root"])
        self.assertEqual(document["components"]["optional"],
                         ["optional-only", "optional-root", "shared"])


class ContractTests(unittest.TestCase):
    def test_missing_eligibility_flag_fails_closed(self):
        package = record("alpha", "devel/alpha")
        del package["custom_patches"]
        with self.assertRaises(ValueError):
            delta_closure.plan(requirements([package]), catalogue([package]), MAKE_CONF)

    def test_incomplete_dependency_closure_is_rejected(self):
        packages = [record("alpha", "devel/alpha", deps=["absent"])]
        with self.assertRaisesRegex(ValueError, "closure"):
            delta_closure.plan(requirements(packages), catalogue(packages), MAKE_CONF)

    def test_cross_architecture_requirement_is_rejected(self):
        packages = [record("alpha", "devel/alpha")]
        packages[0]["abi"] = "FreeBSD:16:aarch64"
        with self.assertRaisesRegex(ValueError, "architecture"):
            delta_closure.plan(requirements(packages), catalogue(packages), MAKE_CONF)

    def test_root_outside_the_evaluated_requirements_is_rejected(self):
        packages = [record("alpha", "devel/alpha")]
        document = requirements(packages, system=["ghost"])
        with self.assertRaisesRegex(ValueError, "root"):
            delta_closure.plan(document, catalogue(packages), MAKE_CONF)

    def test_plan_is_deterministic_regardless_of_record_order(self):
        packages = [
            record("lua54", "lang/lua54", overlay=True),
            record("texlive-base", "print/texlive-base", deps=["lua54"]),
            record("alpha", "devel/alpha"),
        ]
        first = delta_closure.plan(requirements(packages), catalogue(packages), MAKE_CONF)
        reversed_packages = list(reversed(packages))
        second = delta_closure.plan(
            requirements(reversed_packages, system=["lua54"], packages_roots=["alpha"]),
            catalogue(reversed_packages), MAKE_CONF)
        self.assertEqual(first["build"], second["build"])


class SealedPinTests(unittest.TestCase):
    """Golden classification of a real pin, so a drift in the rules is loud.

    The fixtures are a frozen snapshot: the requirement records both native
    workers produced for ports commit 5052095e in pin run 34669392080, the
    upstream catalogue entries for those names, and the make.conf that was in
    effect. They pin the behaviour of the algorithm, not the current state of
    upstream, so they only need regenerating when a classification rule
    deliberately changes.
    """

    maxDiff = None

    def plan(self, arch):
        def read(kind):
            return json.loads(gzip.decompress((FIXTURES / f"{kind}-{arch}.json.gz").read_bytes()))

        return delta_closure.plan(read("requirements"), read("catalogue"),
                                  (FIXTURES / "make.conf").read_text(encoding="utf-8"))

    def test_amd64_delta_is_ninety_two_packages(self):
        document = self.plan("amd64")
        self.assertEqual(document["counts"], {
            "records": 608, "customized": 84, "cascade": 8, "build": 92, "take": 516,
            "causes": {"freesense-own": 44, "non_options_knobs": 2,
                       "options-override": 19, "overlay": 19},
        })
        self.assertEqual(
            [entry["name"] for entry in document["build"] if entry["cause"] == "reverse-dependency"],
            ["doxygen", "graphviz", "lldpd", "nut", "powerman", "tex-formats",
             "texlive-base", "vnstat"])

    def test_arm64_delta_is_ninety_five_packages(self):
        document = self.plan("arm64")
        self.assertEqual(document["counts"], {
            "records": 553, "customized": 90, "cascade": 5, "build": 95, "take": 458,
            "causes": {"absent-upstream": 11, "freesense-own": 41, "non_options_knobs": 2,
                       "options-override": 18, "overlay": 18},
        })
        self.assertEqual(
            [entry["name"] for entry in document["build"] if entry["cause"] == "absent-upstream"],
            ["bind-tools", "doxygen", "kea", "librdkafka", "log4cplus", "nss", "poppler",
             "protobuf-c", "rapidjson", "tex-formats", "texlive-base"])

    def test_upstream_patched_ports_are_not_mistaken_for_freesense_changes(self):
        # archivers/arj, dns/bind920, net/isc-dhcp44-* and lang/lua54 all set
        # EXTRA_PATCHES in their own upstream Makefiles. FreeBSD applies exactly
        # those patches when it builds them, so they are not a divergence, and
        # none of them appear in the overlay.
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                ours = {entry["name"] for entry in self.plan(arch)["build"]}
                for name in ("arj", "lua54", "isc-dhcp44-client", "isc-dhcp44-relay",
                             "isc-dhcp44-server"):
                    self.assertNotIn(name, ours)

    def test_collisions_are_the_suffix_worklist_and_exclude_freesense_own(self):
        for arch, expected in (("amd64", 32), ("arm64", 24)):
            with self.subTest(arch=arch):
                document = self.plan(arch)
                collisions = document["collisions"]
                self.assertEqual(len(collisions), expected)
                names = {entry["name"] for entry in collisions}
                # Nothing FreeSense owns can collide, so nothing it owns needs
                # renaming -- which is what keeps the product's exact-name
                # assumptions about FreeSense-pkg-* intact.
                self.assertEqual({name for name in names if name.startswith("FreeSense")}, set())
                self.assertTrue(names < {entry["name"] for entry in document["build"]})

    def test_no_unexplained_option_divergence_survives(self):
        # A tripwire, not a classifier: every measurable option divergence should
        # already be a declared override. If this fires, make.conf and the
        # published packages have drifted apart.
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                self.assertEqual(
                    [entry["name"] for entry in self.plan(arch)["build"]
                     if entry["cause"] == "options-divergence"], [])

    def test_neither_architecture_builds_a_package_the_mirror_also_serves(self):
        # The layered design's core invariant: our layer and the mirror are
        # disjoint by name, so pkg can never confuse one for the other.
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                document = self.plan(arch)
                build = {entry["name"] for entry in document["build"]}
                self.assertEqual(build & set(document["churn"]), set())

    def test_rust_is_a_build_dependency_the_mirror_supplies(self):
        # The rust availability veto that drives pin selection today is moot
        # once rust is taken from upstream rather than agreed with it.
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                document = self.plan(arch)
                self.assertNotIn("rust", {entry["name"] for entry in document["build"]})
                self.assertNotIn("rust", document["components"]["system"])
                self.assertNotIn("rust", document["components"]["optional"])

    def test_arm64_lags_upstream_further_than_amd64(self):
        # Version drift is per-architecture and structural, which is why each
        # architecture must pin the ports commit its own catalogue reports.
        self.assertEqual(len(self.plan("amd64")["churn"]), 3)
        self.assertEqual(len(self.plan("arm64")["churn"]), 32)


if __name__ == "__main__":
    unittest.main()
