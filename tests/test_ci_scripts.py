"""The 2.0 automation helpers: workflow rules, layout mapping, change list, stages. No network."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

COMPONENT = Path(__file__).resolve().parents[1]
CI = COMPONENT / "scripts/ci"
sys.dont_write_bytecode = True
sys.path.insert(0, str(CI))
import change_list  # noqa: E402
import commit_messages  # noqa: E402
import driver_stages  # noqa: E402
import pin_report  # noqa: E402
import workflow_lint  # noqa: E402

spec = spec_from_file_location("assemble_workspace", COMPONENT / "scripts/assemble-workspace.py")
assemble = module_from_spec(spec)
spec.loader.exec_module(assemble)

WORKFLOWS = COMPONENT / ".github/workflows"
CLEAN = """on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - run: echo ok
"""


class WorkflowRules(unittest.TestCase):
    def assertRefused(self, text: str, signing: bool = False):
        self.assertTrue(workflow_lint.lint(text, signing=signing), text)

    def test_the_committed_workflows_pass(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            self.assertEqual([], workflow_lint.lint(path.read_text(encoding="utf-8"), signing=path.name == "v2-build.yml"), path.name)

    def test_publication_and_credentials_are_refused(self):
        self.assertEqual([], workflow_lint.lint(CLEAN))
        for line in ("      - run: echo ${{ secrets.TOKEN }}", "    environment: production", "      - run: rclone copy a b",
                     "      - run: gh release create v2", "      - run: git push origin 2.0",
                     "      - uses: actions/checkout@v4", "      - run: echo ${{ inputs.ref }}"):
            self.assertRefused(CLEAN + line + "\n")
        self.assertRefused(CLEAN.replace("contents: read", "contents: write"))
        self.assertRefused(CLEAN.replace("contents: read", "id-token: write"))
        self.assertRefused(CLEAN.replace("ubuntu-24.04", "[self-hosted, build-runner]"))
        self.assertRefused(CLEAN.replace("ubuntu-24.04", "my-runner"))

    def test_an_image_artifact_needs_the_request(self):
        step = ("      - uses: actions/upload-artifact@" + "a" * 40 + "\n        with:\n          path: out/*.iso\n")
        self.assertRefused(CLEAN + step)
        self.assertEqual([], workflow_lint.lint(CLEAN + step + "        if: inputs.upload_iso\n"))

    def test_only_the_signing_job_names_the_key(self):
        build = (WORKFLOWS / "v2-build.yml").read_text(encoding="utf-8")
        self.assertEqual(1, build.count("secrets."))
        self.assertEqual(1, build.count("environment: signing-2.0"))
        # The same text in another file, another job, with another secret or environment, or without its limits.
        self.assertRefused(build)
        for old, new in (("\n  sign:\n", "\n  other:\n"), ("FREESENSE_2_PKG_SIGNING_KEY_B64 }}", "OTHER }}"),
                         ("environment: signing-2.0", "environment: production"),
                         ("github.ref == 'refs/heads/2.0' && github.event_name", "github.event_name"),
                         ("  workflow_dispatch:\n", "  pull_request:\n  workflow_dispatch:\n")):
            self.assertIn(old, build)
            self.assertRefused(build.replace(old, new), signing=True)
        self.assertRefused(build + "      - run: echo ${{ secrets.FREESENSE_2_PKG_SIGNING_KEY_B64 }}\n", signing=True)

    def test_the_dispatcher_may_only_start_runs(self):
        text = CLEAN.replace("contents: read", "contents: read\n  actions: write")
        self.assertRefused(text)
        self.assertEqual([], workflow_lint.lint(text, dispatcher=True))


class Layout(unittest.TestCase):
    def test_repository_and_workspace_paths_are_the_inverse_of_each_other(self):
        for path in ("freesense/src/etc/rc", "freesense-native/sysinfo/a.c", "freesense-os-base/build/build",
                     "tools/check.py", "planning/ledger.md", "AGENTS.md", ".gitattributes",
                     "freesense-system-ports/security/x/Makefile", "freesense-packages/Mk/a.mk", "freesense.org/lib/a.js",
                     "freesense.org/.github/workflows/v2-ci.yml", "freesense-docs/.github/workflows/v2-ci.yml"):
            repository, inner = assemble.to_repository(path)
            self.assertEqual(path, assemble.to_workspace(repository, inner), path)

    def test_split_repositories(self):
        self.assertEqual(("freesense", "native/sysinfo/a.c"), assemble.to_repository("freesense-native/sysinfo/a.c"))
        self.assertEqual(("freesense-os-base", "workspace/tools/check.py"), assemble.to_repository("tools/check.py"))
        self.assertEqual(("freesense.org", "v2/lib/a.js"), assemble.to_repository("freesense.org/lib/a.js"))
        # A website repository's own pages stay out of the workspace.
        self.assertIsNone(assemble.to_workspace("freesense.org", "src/pages/index.astro"))


class Changes(unittest.TestCase):
    def test_notes_and_rules(self):
        self.assertEqual("WebUI: a new page for something", commit_messages.note_line("WebUI: a new page for something"))
        self.assertIsNone(commit_messages.note_line("Build: internal work on scripts"))
        self.assertIsNone(commit_messages.note_line("WebUI: internal rework\n\nRelease-Note: skip"))
        self.assertEqual([], commit_messages.problems("Firewall: keep the rule order"))
        self.assertTrue(commit_messages.problems("fixed stuff"))

    def test_change_list_between_two_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "freesense"
            repository.mkdir()

            def git(*arguments: str) -> str:
                return subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                                       "-c", "commit.gpgsign=false", *arguments], check=True, capture_output=True,
                                      text=True).stdout.strip()

            git("init", "-q")
            git("commit", "-q", "--allow-empty", "-m", "WebUI: the first thing here")
            before = git("rev-parse", "HEAD")
            git("commit", "-q", "--allow-empty", "-m", "Firewall: the second thing here")
            current = change_list.record({"freesense": repository}, "2.0.0-dev.2")
            text = change_list.change_list(Path(tmp), current, {"build_id": "2.0.0-dev.1", "repositories": {"freesense": before}})
            self.assertIn("- Firewall: the second thing here", text)
            self.assertNotIn("the first thing", text)


class Stages(unittest.TestCase):
    def test_jobs_follow_the_driver(self):
        self.assertEqual({"base_stage": "source", "repo_stage": "", "complete": "false"}, driver_stages.plan(("check", "source")))
        full = ("check", "source", "base", "deps", "freesense", "repo", "iso", "smoke")
        self.assertEqual({"base_stage": "base", "repo_stage": "repo", "complete": "true"}, driver_stages.plan(full))

    def test_the_driver_in_the_tree_can_be_read(self):
        self.assertIn("check", driver_stages.stages_of(COMPONENT / "build/build"))

    def test_pin_report_changes_nothing(self):
        lock = {"freebsd_source": {"url": "u", "commit": "a" * 40}, "builder_image": {"url": "https://example.test/image"}}
        lines = pin_report.report(lock, head=lambda url: "b" * 40, available=lambda url: False)
        self.assertIn("newer", "\n".join(lines))
        self.assertIn("GONE", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
