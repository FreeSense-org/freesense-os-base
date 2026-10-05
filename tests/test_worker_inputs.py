"""Input acquisition stays out of the component fingerprints, and only it does."""
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "scripts/runner/worker-inputs.sh"

# Anything that shapes, composes, signs or publishes packages changes what is
# built and must stay in a fingerprinted file.
BUILD_SHAPING = re.compile(
    r"poudriere|make\.conf|build\.conf|build\.sh|pkg repo|sign_repository|configure_signing|"
    r"publish_|upload_immutable|rclone copyto --immutable|complete\.json\" \"R2|"
    r"/root/sign|FREESENSE_REPO_SIGNING_KEY|create_jail|bulk")


class WorkerInputsTests(unittest.TestCase):
    def test_the_rendered_worker_includes_the_inputs_before_the_common_runtime(self):
        render = (ROOT / "scripts/render-worker.py").read_text(encoding="utf-8")
        order = [render.index(part) for part in (
            "scripts/runner/install-worker-tools.sh", "scripts/runner/worker-inputs.sh", "args.common")]
        self.assertEqual(order, sorted(order))

    def test_input_acquisition_is_not_part_of_any_recipe(self):
        plan = (ROOT / "scripts/plan.py").read_text(encoding="utf-8")
        self.assertNotIn('ROOT / "scripts/runner/worker-inputs.sh"', plan)
        self.assertNotIn('ROOT / "scripts/runner/install-worker-tools.sh"', plan)
        # The parts that do shape the build stay fingerprinted.
        for part in ("worker-common.sh", "stages/system.sh", "stages/packages.sh",
                     "assembly-common.sh", "render-worker.py"):
            self.assertIn(part, plan)

    def test_the_inputs_file_only_acquires_inputs(self):
        source = INPUTS.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertIsNone(BUILD_SHAPING.search(code), BUILD_SHAPING.search(code) and BUILD_SHAPING.search(code).group(0))
        defined = re.findall(r"^([a-z_]+)\(\) \{", source, re.M)
        self.assertEqual(defined, ["clone_exact", "restore_upstream", "fetch_input",
                                   "fetch_repository", "acquire_sources"])
        # Every acquisition verifies what arrived against its pin.
        for check in ('rev-parse HEAD)" = "${commit}"', "sha256 -q", "refs/heads/main)\" = \"${upstream_commit}\"",
                      "verify_repository"):
            self.assertIn(check, source)

    def test_the_moved_functions_are_defined_once(self):
        runner = [*sorted((ROOT / "scripts/runner").glob("*.sh")), *sorted((ROOT / "scripts/runner/stages").glob("*.sh"))]
        for name in ("clone_exact", "restore_upstream", "fetch_input", "fetch_repository", "acquire_sources"):
            owners = [p.name for p in runner if re.search(rf"^{name}\(\) \{{", p.read_text(encoding="utf-8"), re.M)]
            self.assertEqual(owners, ["worker-inputs.sh"], name)


if __name__ == "__main__":
    unittest.main()
