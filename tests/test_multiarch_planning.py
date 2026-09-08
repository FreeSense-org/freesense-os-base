"""Run the component planner with frozen v4 inputs and no network resolution."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from test_multiarch import pin_fixture
from test_planning import plan, ROOT
from multiarch_plan import planning_closure


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 9, 7, tzinfo=timezone.utc)
        return instant if tz is None else instant.astimezone(tz)


class FrozenPlanningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        for name in ("build-policy.json", "channel-signing-public.pem"):
            (self.root / "config" / name).write_bytes((ROOT / "config" / name).read_bytes())
        pin = pin_fixture()
        pin["freebsd_source"]["osversion"] = 1600019
        (self.root / "config/freebsd-16.json").write_text(json.dumps(pin))
        self.resolved = {"source": "1" * 40, "system_ports": "2" * 40, "packages": "3" * 40}

    def run_plan(self, component, arch, host, closure=None, recipe="a" * 64):
        resolved = self.root / "resolved.json"
        resolved.write_text(json.dumps(self.resolved))
        argv = ["plan.py", component, "--target", arch, "--build-host", host,
                "--resolved-inputs", str(resolved), "--immutable-only", "--os-base-sha", "4" * 40]
        if closure is not None:
            path = self.root / "closure.json"
            path.write_text(json.dumps(closure))
            argv.extend(["--system-closure", str(path)])
        with mock.patch.object(plan, "ROOT", self.root), mock.patch.object(plan, "datetime", Clock), \
                mock.patch.object(plan, "recipe_digest", return_value="b" * 64), \
                mock.patch.object(plan, "remote_recipe_digest", return_value=recipe), \
                mock.patch.object(plan, "remote_sha", side_effect=AssertionError("moving branch lookup")), \
                mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as rendered:
            self.assertEqual(plan.main(), 0)
        return json.loads(rendered.getvalue())

    def test_system_only_changes_preserve_optional_fingerprint_on_both_architectures(self):
        for arch, host in (("amd64", "github-amd64"), ("arm64", "github-arm64"), ("arm64", "dedicated")):
            with self.subTest(arch=arch, host=host):
                before = self.run_plan("system", arch, host)
                optional = self.run_plan("packages", arch, host, planning_closure(before))
                self.resolved["source"] = "5" * 40
                self.resolved["system_ports"] = "6" * 40
                after = self.run_plan("system", arch, host)
                changed = self.run_plan("packages", arch, host, planning_closure(after))
                self.assertNotEqual(before["system"], after["system"])
                self.assertEqual(optional["packages"], changed["packages"])
                self.resolved.update(source="1" * 40, system_ports="2" * 40)

    def test_optional_source_and_build_configuration_invalidate_optional(self):
        system = self.run_plan("system", "amd64", "github-amd64")
        closure = planning_closure(system)
        before = self.run_plan("packages", "amd64", "github-amd64", closure)
        self.resolved["packages"] = "7" * 40
        after = self.run_plan("packages", "amd64", "github-amd64", closure)
        self.assertNotEqual(before["packages"], after["packages"])
        changed = self.run_plan("packages", "amd64", "github-amd64", closure, recipe="8" * 64)
        self.assertNotEqual(after["packages"], changed["packages"])

    def test_cannot_switch_executor_after_system_planning(self):
        native = self.run_plan("system", "arm64", "github-arm64")
        with self.assertRaisesRegex(SystemExit, "frozen v4 pin or executor"):
            self.run_plan("packages", "arm64", "dedicated", planning_closure(native))
        fallback = self.run_plan("system", "arm64", "dedicated")
        self.assertNotEqual(native["system"], fallback["system"])
        with self.assertRaisesRegex(SystemExit, "frozen v4 pin or executor"):
            self.run_plan("packages", "arm64", "github-arm64", planning_closure(fallback))

    def test_cannot_change_pinned_worker_in_staged_closure(self):
        system = self.run_plan("system", "amd64", "github-amd64")
        closure = planning_closure(system)
        closure["artifact_worker_tools_sha256"] = "9" * 64
        with self.assertRaisesRegex(SystemExit, "frozen v4 pin or executor"):
            self.run_plan("packages", "amd64", "github-amd64", closure)
