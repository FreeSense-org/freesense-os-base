from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mirror_worker_env

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "5052095ee6f48f63c4fd38dd9ffb2632513036d3"
OBJECT = "inputs/sha256/" + "a" * 64


def policy():
    return json.loads((ROOT / "config" / "build-policy.json").read_text(encoding="utf-8"))


def pin(architecture="amd64"):
    target = {
        "abi": "FreeBSD:16:amd64" if architecture == "amd64" else "FreeBSD:16:aarch64",
        "worker_image": {"sha256": "b" * 64},
        "worker_tools": {"sha256": "c" * 64},
        "jail_seed": {"object": "inputs/sha256/" + "d" * 64},
    }
    return {
        "schema_version": "freesense.freebsd-pin/v4",
        "freebsd_source": {"commit": "e" * 40},
        "bootstrap_snapshot": {"osversion": 1600022},
        "targets": {"amd64": dict(target, abi="FreeBSD:16:amd64"),
                    "arm64": dict(target, abi="FreeBSD:16:aarch64")},
    }


def plan(architecture="amd64"):
    return {
        "schema_version": "freesense.mirror-plan/v1",
        "abi": "FreeBSD:16:amd64" if architecture == "amd64" else "FreeBSD:16:aarch64",
        "architecture": architecture, "ports_commit": COMMIT,
        "fingerprint": "f" * 64, "packages": [],
    }


def build(architecture="amd64", **overrides):
    options = {"architecture": architecture, "plan_object": OBJECT,
               "source_sha": "1" * 40, "os_base_sha": "2" * 40, "generation": 1}
    options.update(overrides)
    return mirror_worker_env.worker_env(pin(architecture), policy(), plan(architecture), **options)


class CoverageTests(unittest.TestCase):
    def test_every_worker_input_is_supplied_except_the_credentials(self):
        renderer = runpy.run_path(str(ROOT / "scripts" / "render-worker.py"))
        required = set(renderer["FIELDS"]) - set(mirror_worker_env.CREDENTIAL_FIELDS)
        self.assertEqual(required - set(build()), set())

    def test_no_credential_or_key_material_is_emitted(self):
        for name in mirror_worker_env.CREDENTIAL_FIELDS:
            self.assertNotIn(name, build())

    def test_the_pinned_worker_image_and_tools_come_from_the_pin(self):
        fields = build()
        self.assertEqual(fields["IMAGE_SHA256"], "b" * 64)
        self.assertEqual(fields["WORKER_TOOLS_SHA256"], "c" * 64)
        self.assertEqual(fields["FREEBSD_SHA"], "e" * 40)
        self.assertEqual(fields["PORTS_SHA"], COMMIT)
        self.assertEqual(fields["FINGERPRINT"], "f" * 64)

    def test_the_target_description_comes_from_the_build_policy(self):
        fields = build("arm64")
        self.assertEqual(fields["ABI"], "FreeBSD:16:aarch64")
        self.assertEqual(fields["PACKAGE_ARCH"], "aarch64")
        self.assertEqual(fields["POUDRIERE_ARCH"], "arm64.aarch64")

    def test_the_pin_identity_is_the_digest_of_the_whole_pin(self):
        from multiarch_pin import digest
        self.assertEqual(build()["FREEBSD_PIN_ID"], digest(pin()))


class ContractTests(unittest.TestCase):
    def test_a_plan_for_another_architecture_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not describe this target"):
            mirror_worker_env.worker_env(pin("amd64"), policy(), plan("arm64"),
                                         architecture="amd64", plan_object=OBJECT,
                                         source_sha="1" * 40, os_base_sha="2" * 40, generation=1)

    def test_an_unpinned_plan_object_is_rejected(self):
        for value in ("mirror-plan.json", "inputs/sha256/short", "https://example/plan.json"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "content-addressed"):
                build(plan_object=value)

    def test_an_inexact_revision_is_rejected(self):
        for field in ("source_sha", "os_base_sha"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "exact commit"):
                build(**{field: "main"})

    def test_an_invalid_generation_is_rejected(self):
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build(generation=value)


class WorkerValidationTests(unittest.TestCase):
    """Run worker-common's own startup checks against the emitted inputs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("sh") or shutil.which("bash")
        if cls.shell is None and os.name == "nt":
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            if candidate.exists():
                cls.shell = str(candidate)
        source = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        start = source.index('case "${STAGE}" in system|')
        cls.fragment = source[start:source.index("\nPREFIX=v1", start)]

    def validate(self, fields) -> subprocess.CompletedProcess[str]:
        if self.shell is None:
            self.skipTest("POSIX shell is unavailable")
        return subprocess.run([self.shell, "-eu", "-c", self.fragment], text=True,
                              capture_output=True, check=False, env={**os.environ, **fields})

    def test_the_emitted_inputs_satisfy_the_worker_contract(self):
        for architecture in ("amd64", "arm64"):
            with self.subTest(architecture=architecture):
                result = self.validate(build(architecture))
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_worker_would_reject_a_mismatched_product_train(self):
        fields = dict(build(), PRODUCT_VERSION="9.9.9-DEVELOPMENT")
        result = self.validate(fields)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match package train", result.stderr)


if __name__ == "__main__":
    unittest.main()
