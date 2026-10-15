"""The Testing line plans RELENG heads as the next Stable-train patch."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("freesense_testing_line", ROOT / "scripts/stable_farm_plan.py")
stable_farm_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stable_farm_plan)
import stable_refresh  # noqa: E402

HEADS = {"freesense": "1" * 40, "freesense-system-ports": "2" * 40, "freesense-packages": "3" * 40}


class TestingLineTests(unittest.TestCase):
    def resolve(self, heads=HEADS, only_amd64=False):
        planned = []

        def resolve_target(policy, release, os_base_sha, build_host, architecture, testing_inputs=None):
            planned.append((architecture, release, json.loads(Path(testing_inputs).read_text())))
            return {"system": {}, "packages": {}}

        with mock.patch.object(stable_refresh, "head_commit", lambda repository, branch: heads.get(repository)), \
                mock.patch.object(stable_farm_plan, "resolve_target", resolve_target):
            result = stable_farm_plan.resolve_testing("4" * 40, "dedicated", only_amd64)
        return result, planned

    def test_plans_the_releng_heads_as_the_next_patch(self):
        policy = json.loads((ROOT / "config/build-policy.json").read_text(encoding="utf-8"))
        train = policy["release"]["stable_train"]
        newest, _ = stable_refresh.latest_lock(ROOT, train)
        result, planned = self.resolve()
        self.assertEqual(result["lock"], {"release": stable_refresh.next_version(newest)})
        self.assertEqual(planned[0][2], {"source": "1" * 40, "system_ports": "2" * 40, "packages": "3" * 40})
        self.assertEqual({arch for arch, _, _ in planned},
                         {"amd64", "arm64"} if policy["targets"]["arm64"]["build_enabled"] else {"amd64"})

    def test_only_amd64_skips_arm64(self):
        _, planned = self.resolve(only_amd64=True)
        self.assertEqual([arch for arch, _, _ in planned], ["amd64"])

    def test_a_missing_maintenance_branch_stops_the_plan(self):
        with self.assertRaises(SystemExit):
            self.resolve(heads={**HEADS, "freesense-packages": None})

    def test_testing_seeds_from_testing_first(self):
        calls = []

        def current(manifest, component, channel="devel"):
            calls.append(channel)
            return {}
        with mock.patch.object(stable_farm_plan, "current_component_record", current):
            stable_farm_plan.stable_seed("m", "system", {"freebsd_pin_id": "c" * 64},
                                         stable_farm_plan.TESTING_SEED_CHANNELS)
        self.assertEqual(calls, ["testing", "stable", "devel"])


class LineVersionArgumentTests(unittest.TestCase):
    def plan(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/plan.py"), *args],
                              capture_output=True, text=True, cwd=ROOT)

    def test_line_version_must_belong_to_the_stable_train(self):
        result = self.plan("system", "--line-version", "1.2.0", "--os-base-sha", "4" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--line-version must be an exact", result.stderr)

    def test_line_version_excludes_a_release_lock_and_images(self):
        self.assertIn("plans only Testing", self.plan("system", "--line-version", "1.1.6",
                                                     "--release-lock", "1.1.5").stderr)
        self.assertIn("plans only Testing", self.plan("iso", "--line-version", "1.1.6").stderr)


if __name__ == "__main__":
    unittest.main()
