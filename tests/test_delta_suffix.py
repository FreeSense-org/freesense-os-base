from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import delta_suffix
import package_requirements


def collisions(*origins):
    return [{"name": origin.split("/")[1], "origin": origin} for origin in origins]


class RegionTests(unittest.TestCase):
    def test_one_conditional_block_per_port_in_origin_order(self):
        rendered = delta_suffix.region(collisions("www/squid", "dns/unbound"), "-fs")
        self.assertEqual(rendered, delta_suffix.BEGIN + "\n"
                         '.if ${.CURDIR:N*/dns/unbound}==""\n'
                         "PKGNAMESUFFIX+=\t-fs\n"
                         ".endif\n"
                         '.if ${.CURDIR:N*/www/squid}==""\n'
                         "PKGNAMESUFFIX+=\t-fs\n"
                         ".endif\n"
                         + delta_suffix.END + "\n")

    def test_a_shorter_origin_cannot_match_a_longer_one(self):
        # net/haproxy must not suffix net/haproxy-devel: the pattern has no
        # trailing wildcard, so it has to match the directory exactly.
        rendered = delta_suffix.region(collisions("net/haproxy"), "-fs")
        self.assertIn('${.CURDIR:N*/net/haproxy}==""', rendered)
        self.assertNotIn("haproxy-devel", rendered)

    def test_duplicate_origins_collapse(self):
        entries = collisions("www/squid") + collisions("www/squid")
        self.assertEqual(delta_suffix.region(entries, "-fs").count("PKGNAMESUFFIX"), 1)

    def test_an_empty_delta_still_renders_a_well_formed_region(self):
        rendered = delta_suffix.region([], "-fs")
        self.assertEqual(rendered, delta_suffix.BEGIN + "\n" + delta_suffix.END + "\n")

    def test_unsafe_suffix_and_origin_are_rejected(self):
        for suffix in ("fs", "-FS", "-f s", "-fs;rm -rf /", ""):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                delta_suffix.region(collisions("www/squid"), suffix)
        for origin in ("../etc/passwd", "www/squid ${X}", "squid"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                delta_suffix.region([{"name": "squid", "origin": origin}], "-fs")


class ApplyTests(unittest.TestCase):
    def test_region_is_appended_once_and_re_rendering_replaces_it(self):
        base = "DEFAULT_VERSIONS=\tphp=8.5\n"
        first = delta_suffix.apply(base, delta_suffix.region(collisions("www/squid"), "-fs"))
        second = delta_suffix.apply(first, delta_suffix.region(collisions("dns/unbound"), "-fs"))
        self.assertEqual(second.count(delta_suffix.BEGIN), 1)
        self.assertIn("DEFAULT_VERSIONS", second)
        self.assertIn("dns/unbound", second)
        self.assertNotIn("www/squid", second)

    def test_a_make_conf_without_a_trailing_newline_stays_parseable(self):
        applied = delta_suffix.apply("DEFAULT_VERSIONS=\tphp=8.5",
                                     delta_suffix.region(collisions("www/squid"), "-fs"))
        self.assertIn("php=8.5\n", applied)

    def test_the_rendered_region_survives_the_make_conf_audit(self):
        # package_requirements fails closed on any assignment it has not audited,
        # so the generated region has to be expressible in that vocabulary.
        applied = delta_suffix.apply((Path(__file__).resolve().parent / "fixtures" /
                                      "delta-closure" / "make.conf").read_text(encoding="utf-8"),
                                     delta_suffix.region(collisions("www/squid", "dns/unbound"), "-fs"))
        self.assertIn("PKGNAMESUFFIX", package_requirements.audit_make_conf(applied))


class ConflictTests(unittest.TestCase):
    def test_a_port_that_assigns_the_suffix_itself_is_reported(self):
        entries = collisions("www/squid", "net/haproxy-devel")
        evaluated = {"squid": "squid-fs", "haproxy-devel": "haproxy-devel"}
        self.assertEqual(delta_suffix.conflicts(entries, evaluated, "-fs"), ["haproxy-devel"])

    def test_a_fully_applied_suffix_reports_nothing(self):
        entries = collisions("www/squid", "dns/unbound")
        evaluated = {"squid": "squid-fs", "unbound": "unbound-fs"}
        self.assertEqual(delta_suffix.conflicts(entries, evaluated, "-fs"), [])

    def test_a_port_missing_from_the_evaluation_is_reported(self):
        self.assertEqual(delta_suffix.conflicts(collisions("www/squid"), {}, "-fs"), ["squid"])


if __name__ == "__main__":
    unittest.main()
