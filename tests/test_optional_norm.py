"""net/libzmq4 is built without NORM on amd64, so net/norm must not be built there."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (ROOT / "scripts/runner/stages/packages.sh").read_text(encoding="utf-8")


class OptionalNormTests(unittest.TestCase):
    def test_the_option_is_set_in_the_global_make_conf_for_amd64_only(self) -> None:
        guarded = (
            'if [ "${PACKAGE_ARCH}" = amd64 ]; then\n'
            "  echo 'net_libzmq4_UNSET_FORCE=NORM' >>/usr/local/etc/poudriere.d/make.conf\n"
            "fi\n"
        )
        self.assertIn(guarded, PACKAGES)

    def test_norm_is_dropped_from_the_roots_after_the_plan_writes_them(self) -> None:
        roots = PACKAGES.index("write_delta_bulk optional")
        drop = PACKAGES.index("awk '$0 != \"net/norm\"'")
        self.assertLess(roots, drop)
        self.assertLess(drop, PACKAGES.index("policy=/root/freesense-packages/architecture-policy.json"))
        # the removal sits inside an amd64-only block
        opening = PACKAGES.rindex('if [ "${PACKAGE_ARCH}" = amd64 ]; then', 0, drop)
        self.assertNotIn("\nfi\n", PACKAGES[opening:drop])


if __name__ == "__main__":
    unittest.main()
