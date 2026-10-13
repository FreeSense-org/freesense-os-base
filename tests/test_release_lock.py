from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_lock = load_script("freesense_release_lock", "scripts/release_lock.py")
plan = load_script("freesense_plan_for_locks", "scripts/plan.py")
with mock.patch.dict(sys.modules, {"plan": plan}):
    stable_farm_plan = load_script("freesense_stable_farm_plan", "scripts/stable_farm_plan.py")

POLICY = {"release": {"stable_train": "1.1"}}


def lock_document(**changes) -> dict:
    document = {
        "schema_version": "freesense.release-lock/v1", "release": "1.1.0", "product_version": "1.1.0-RELEASE",
        "package_train": "1.1", "sealed": True,
        "source_sha": "1" * 40, "system_ports_sha": "2" * 40, "packages_sha": "3" * 40,
        "freebsd_source_sha": "4" * 40, "freebsd_ports_sha": "5" * 40, "freebsd_osversion": 1600027,
        "jail_object": "inputs/sha256/" + "6" * 64,
        "worker_image_sha256": "7" * 64, "worker_tools_sha256": "8" * 64,
    }
    document.update(changes)
    return document


def write_lock(root: Path, document: dict, release: str = "1.1.0") -> None:
    (root / "config/releases").mkdir(parents=True, exist_ok=True)
    (root / "config/releases" / f"{release}.json").write_text(json.dumps(document), encoding="utf-8")


class ReleaseLockTests(unittest.TestCase):
    def load(self, document: dict, release: str = "1.1.0") -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_lock(root, document, release)
            return release_lock.load(root, POLICY, release)

    def test_a_sealed_lock_of_the_stable_train_loads(self):
        lock = self.load(lock_document())
        self.assertEqual(release_lock.sources(lock), {"source": "1" * 40, "system_ports": "2" * 40, "packages": "3" * 40})
        policy = release_lock.policy_for({"package_train": "1.2", "release": {"development_version": "1.2.0"}}, lock)
        self.assertEqual((policy["package_train"], policy["release"]["development_version"]), ("1.1", "1.1.0"))

    def test_policy_override_does_not_mutate_the_rolling_policy(self):
        rolling = {"package_train": "1.2", "release": {"development_version": "1.2.0"}}
        release_lock.policy_for(rolling, lock_document())
        self.assertEqual(rolling, {"package_train": "1.2", "release": {"development_version": "1.2.0"}})

    def test_a_lock_that_is_not_exactly_sealed_for_the_train_is_refused(self):
        for name, document, release in (
            ("unsealed", lock_document(sealed=False), "1.1.0"),
            ("other train", lock_document(release="1.0.7", product_version="1.0.7-RELEASE", package_train="1.0"), "1.0.7"),
            ("development label", lock_document(product_version="1.1.0-DEVELOPMENT"), "1.1.0"),
            ("train mismatch", lock_document(package_train="1.2"), "1.1.0"),
            ("short commit", lock_document(source_sha="1" * 39), "1.1.0"),
            ("bad worker hash", lock_document(worker_tools_sha256="z" * 64), "1.1.0"),
            ("bad jail", lock_document(jail_object="inputs/sha256/short"), "1.1.0"),
            ("no osversion", lock_document(freebsd_osversion="1600027"), "1.1.0"),
        ):
            with self.subTest(name), self.assertRaises(SystemExit):
                self.load(document, release)

    def test_the_lock_must_agree_with_the_active_pin(self):
        lock = lock_document()
        pin = {"freebsd_source": {"commit": "4" * 40}, "freebsd_ports": {"commit": "5" * 40}}
        same = dict(osversion=1600027, jail_object="inputs/sha256/" + "6" * 64,
                    worker_image_sha256="7" * 64, worker_tools_sha256="8" * 64)
        release_lock.check_pin(lock, pin, **same)
        for field, value in (("osversion", 1600028), ("jail_object", "inputs/sha256/" + "0" * 64),
                             ("worker_image_sha256", "0" * 64), ("worker_tools_sha256", "0" * 64)):
            with self.subTest(field), self.assertRaises(SystemExit):
                release_lock.check_pin(lock, pin, **{**same, field: value})
        with self.assertRaises(SystemExit):
            release_lock.check_pin(lock, {**pin, "freebsd_source": {"commit": "0" * 40}}, **same)


class PinWindowDateTime(plan.datetime):
    @classmethod
    def now(cls, tz=None):
        from datetime import datetime, timezone
        moment = datetime(2026, 7, 29, 6, tzinfo=timezone.utc)
        return moment.replace(tzinfo=None) if tz is None else moment.astimezone(tz)


class SealedReleasePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patchers = [
            mock.patch.object(plan, "datetime", PinWindowDateTime),
            mock.patch.object(plan, "source_tree_digest",
                              side_effect=lambda repository, commit: __import__("hashlib").sha256(f"{repository}@{commit}".encode()).hexdigest()),
            mock.patch.object(plan, "recipe_digest", return_value="7" * 64),
        ]
        for patcher in cls.patchers:
            patcher.start()

    @classmethod
    def tearDownClass(cls):
        for patcher in cls.patchers:
            patcher.stop()

    def tree(self, directory: str, lock: dict | None) -> Path:
        root = Path(directory)
        (root / "config").mkdir()
        (root / "patches").mkdir()
        (root / "config/freebsd-16.json").write_text(json.dumps({
            "schema_version": "freesense.freebsd-pin/v2", "ready": True,
            "valid_from": "2026-07-22T06:00:00Z", "valid_until": "2026-08-05T06:00:00Z",
            "freebsd_source": {"commit": "4" * 40, "osversion": 1600027},
            "freebsd_ports": {"commit": "5" * 40},
            "jail_seed": {"object": "inputs/sha256/" + "6" * 64, "sha256": "6" * 64},
            "worker_image": {"sha256": "7" * 64},
            "worker_tools": {"object": "inputs/sha256/" + "8" * 64, "sha256": "8" * 64},
        }))
        (root / "config/build-policy.json").write_text(json.dumps({
            "package_train": "1.2", "abi": "FreeBSD:16:amd64", "altabi": "freebsd:16:x86:64",
            "public_base_url": "https://pkg.freesense.org/v1",
            "runner": {"vcpus": 12, "memory_mib": 32768, "disk_gib": 160},
            "release": {"stable_train": "1.1", "development_train": "1.2", "development_version": "1.2.0",
                        "stable_lifecycle": "supported", "development_lifecycle": "experimental"},
            "cloud": {"architecture": "amd64", "formats": ["qcow2", "raw"],
                      "variants": {"ufs": {"virtual_size_gib": 16, "root_growth": True},
                                   "zfs": {"virtual_size_gib": 32, "root_growth": True}}},
        }))
        (root / "config/channel-signing-public.pem").write_bytes(b"test-key")
        if lock is not None:
            write_lock(root, lock)
        return root

    def run_plan(self, root: Path, *extra: str) -> dict:
        argv = ["plan.py", "system", "--os-base-sha", "9" * 40, *extra]
        sentinel = AssertionError("a sealed release must not resolve moving branches")
        with mock.patch.object(sys, "argv", argv), mock.patch.object(plan, "ROOT", root), \
                mock.patch.object(plan, "remote_sha", side_effect=("a" * 40, "b" * 40) if not extra else sentinel), \
                mock.patch.object(plan, "current_component", return_value=""), \
                redirect_stdout(io.StringIO()) as rendered:
            self.assertEqual(plan.main(), 0)
        return json.loads(rendered.getvalue())

    def test_a_sealed_release_plans_its_own_commits_train_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, lock_document())
            values = self.run_plan(root, "--release-lock", "1.1.0")
        self.assertEqual((values["source_sha"], values["system_sha"]), ("1" * 40, "2" * 40))
        self.assertEqual((values["package_train"], values["release_version"], values["product_version"]),
                         ("1.1", "1.1.0", "1.1.0-RELEASE"))

    def test_a_sealed_release_is_not_the_development_build_of_the_same_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, lock_document(source_sha="a" * 40, system_ports_sha="b" * 40))
            sealed = self.run_plan(root, "--release-lock", "1.1.0")
            rolling = self.run_plan(root)
        self.assertEqual((rolling["source_sha"], rolling["system_sha"]), ("a" * 40, "b" * 40))
        self.assertEqual(rolling["product_version"], "1.2.0-DEVELOPMENT")
        self.assertNotEqual(sealed["system"], rolling["system"])
        self.assertNotEqual(sealed["platform"], rolling["platform"])

    def test_a_lock_sealed_against_another_pin_cannot_be_planned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, lock_document(jail_object="inputs/sha256/" + "0" * 64))
            with self.assertRaises(SystemExit) as raised:
                self.run_plan(root, "--release-lock", "1.1.0")
        self.assertIn("jail seed", str(raised.exception))

    def test_release_lock_is_for_system_and_optional_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.tree(directory, lock_document())
            with mock.patch.object(sys, "argv", ["plan.py", "iso", "--release-lock", "1.1.0"]), \
                    mock.patch.object(plan, "ROOT", root), self.assertRaises(SystemExit):
                plan.main()


class StableWorkflowContractTests(unittest.TestCase):
    def test_every_plan_output_the_workflow_reads_is_produced(self):
        system = {k: "x" for k in ("system", "platform", "freebsd_pin_id", "source_sha", "system_sha", "os_base_sha",
                                   "freebsd_sha", "ports_sha", "image_sha256", "worker_tools_sha256", "jail_object",
                                   "package_train", "product_version", "abi", "altabi", "osversion",
                                   "public_base_url", "build_host")}
        packages = {"packages": "y", "packages_sha": "z"}
        produced = set(stable_farm_plan.outputs({"lock": {"release": "1.1.0"}, "system": system, "packages": packages}))
        workflow = (ROOT / ".github/workflows/stable.yml").read_text(encoding="utf-8")
        planned = {name for name in re.findall(r"steps\.plan\.outputs\.(\w+)", workflow)}
        self.assertTrue(planned)
        self.assertEqual(planned - produced, set())

    def test_stable_never_calls_the_retired_single_job_build(self):
        workflow = (ROOT / ".github/workflows/stable.yml").read_text(encoding="utf-8")
        self.assertNotIn("runner-build.yml", workflow)
        self.assertIn("farm_layout", (ROOT / ".github/workflows/component-farm.yml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
