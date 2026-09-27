"""The component farm layout per build host."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from component_farm import farm_matrix  # noqa: E402


class FarmMatrixTests(unittest.TestCase):
    def test_dedicated_host_builds_each_component_whole(self):
        for stage in ("system", "packages"):
            with self.subTest(stage=stage):
                self.assertEqual(farm_matrix(stage, "dedicated"), [{"part": "full", "shard": "0"}])

    def test_hosted_runners_keep_the_parallel_farm(self):
        system = farm_matrix("system", "github-amd64")
        self.assertEqual(system[0], {"part": "core", "shard": "0"})
        self.assertEqual([part["shard"] for part in system[1:]], [str(index) for index in range(8)])
        self.assertTrue(all(part["part"] == "shard" for part in system[1:]))
        self.assertEqual(len(farm_matrix("packages", "github-arm64")), 8)

    def test_whole_part_skips_finalize_and_uses_default_coordinates(self):
        farm = (ROOT / ".github/workflows/component-farm.yml").read_text(encoding="utf-8")
        self.assertIn("system_shard_count: ${{ matrix.part == 'full' && '1' || '8' }}", farm)
        self.assertIn("if: fromJSON(inputs.plan).build_host != 'dedicated'", farm)

    def test_worker_and_stages_accept_a_whole_delta_part(self):
        common = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        self.assertIn("system:full", common)
        self.assertIn("packages:full", common)
        self.assertIn("a whole delta build requires default shard coordinates", common)
        system = (ROOT / "scripts/runner/stages/system.sh").read_text(encoding="utf-8")
        self.assertIn('[ "${roots_mode}" != full ] || [ "${SYSTEM_PART}" = full ]', system)
        packages = (ROOT / "scripts/runner/stages/packages.sh").read_text(encoding="utf-8")
        self.assertIn('elif [ "${SYSTEM_PART}" = full ]; then', packages)


if __name__ == "__main__":
    unittest.main()
