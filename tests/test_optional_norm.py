"""net/libzmq4 is built without NORM only when System does not already supply it."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (ROOT / "scripts/runner/stages/packages.sh").read_text(encoding="utf-8")


class OptionalNormTests(unittest.TestCase):
    def guard(self) -> int:
        return PACKAGES.index(
            "if ! awk -F '|' '$1 == \"libzmq4\" { found=1 } END { exit !found }' \"${inventory}\"; then\n"
        )

    def test_the_option_is_set_only_when_the_seed_lacks_libzmq4(self) -> None:
        guarded = (
            "then\n"
            "  echo 'net_libzmq4_UNSET_FORCE=NORM' >>/usr/local/etc/poudriere.d/make.conf\n"
        )
        self.assertEqual(PACKAGES.count("net_libzmq4_UNSET_FORCE=NORM"), 1)
        self.assertEqual(PACKAGES.index(guarded, self.guard()), PACKAGES.index(guarded))

    def test_the_guard_follows_the_seed_merge_and_precedes_sharding(self) -> None:
        merge = PACKAGES.index('merge_package "${package}" "${combined}/All" "${inventory}" identical')
        self.assertLess(PACKAGES.index("write_delta_bulk optional"), merge)
        self.assertLess(merge, self.guard())
        self.assertLess(self.guard(), PACKAGES.index("partition_roots.py"))
        self.assertLess(self.guard(), PACKAGES.index('seed_poudriere_repository "${combined}"'))

    def test_norm_is_dropped_from_the_roots_inside_the_same_guard(self) -> None:
        drop = PACKAGES.index("awk '$0 != \"net/norm\"'")
        self.assertLess(self.guard(), drop)
        self.assertNotIn("\nfi\n", PACKAGES[self.guard():drop])
        self.assertNotIn('if [ "${PACKAGE_ARCH}" = amd64 ]', PACKAGES[self.guard() - 200:drop])


if __name__ == "__main__":
    unittest.main()
