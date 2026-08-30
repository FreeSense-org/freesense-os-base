from __future__ import annotations

from pathlib import Path
import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_platform  # noqa: E402


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


staged_closure = load_script("freesense_staged_closure", "scripts/staged_system_closure.py")


class BuildPlatformTests(unittest.TestCase):
    def test_canonical_targets_and_profiles(self):
        policy = build_platform.load_policy(ROOT / "config/build-policy.json")
        amd64 = build_platform.target(policy, "amd64")
        arm64 = build_platform.target(policy, "arm64")

        self.assertEqual((amd64["package_arch"], amd64["poudriere_arch"]),
                         ("amd64", "amd64.amd64"))
        self.assertEqual((arm64["freebsd_target"], arm64["freebsd_target_arch"]),
                         ("arm64", "aarch64"))
        self.assertEqual((arm64["package_arch"], arm64["abi"], arm64["altabi"]),
                         ("aarch64", "FreeBSD:16:aarch64", "freebsd:16:aarch64:64"))
        self.assertEqual(amd64["kernel"], "FreeSense")
        self.assertEqual(arm64["kernel"], "FreeSense")
        self.assertEqual(build_platform.manifest_name(amd64, legacy=True),
                         "repos.manifest.json")
        self.assertEqual(build_platform.manifest_name(amd64),
                         "repos.amd64.manifest.json")
        self.assertEqual(build_platform.manifest_name(arm64),
                         "repos.aarch64.manifest.json")

        profile = build_platform.image_profile(policy, None, "arm64")
        self.assertEqual(profile["name"], "generic-arm64-uefi")
        self.assertEqual(profile["firmware"], ["uefi"])
        self.assertEqual(profile["installer"], "img")
        self.assertFalse(profile["capabilities"]["bios"])
        self.assertFalse(profile["capabilities"]["cloud_init"])
        self.assertEqual(profile["filesystems"], [])
        self.assertEqual(profile["formats"], [])
        self.assertEqual(profile["variants"], {})
        profiles = build_platform.release_profiles(policy, "arm64")
        self.assertEqual([item["name"] for item in profiles], [
            "generic-arm64-uefi", "arm64-rpi4b", "arm64-rpi5-d0",
        ])
        self.assertEqual(profiles[1]["boot_partition"]["filesystem"], "fat16")
        self.assertEqual(profiles[2]["boot_partition"]["filesystem"], "fat32")
        self.assertEqual(profiles[2]["minimum_eeprom_date"], "2025-06-09")
        self.assertEqual(profiles[2]["boot_inputs"]["archive_sha256"],
                         "c4fbbec9cd0d1115c9adab884923061b960de42b4ca6d65ba5f08cb6b46c6fad")

    def test_pi_recipe_change_isolated_from_unrelated_artifacts(self):
        planner = load_script("freesense_plan_fingerprint", "scripts/plan.py")
        closure = {"system": "a" * 64, "packages": "b" * 64}
        generic = {"name": "generic-arm64-uefi"}
        pi4 = {"name": "arm64-rpi4b", "boot_inputs": {"ports": "c" * 40}}
        installer_before = planner.release_artifact_fingerprint(
            "installer", closure, generic, "d" * 64)
        pi_before = planner.release_artifact_fingerprint(
            "appliance", closure, pi4, "e" * 64, boot_inputs=pi4["boot_inputs"])
        changed = {**pi4, "boot_inputs": {"ports": "f" * 40}}
        installer_after = planner.release_artifact_fingerprint(
            "installer", closure, generic, "d" * 64)
        pi_after = planner.release_artifact_fingerprint(
            "appliance", closure, changed, "e" * 64, boot_inputs=changed["boot_inputs"])
        self.assertEqual(installer_before, installer_after)
        self.assertNotEqual(pi_before, pi_after)

    def test_profile_cannot_cross_targets(self):
        policy = build_platform.load_policy(ROOT / "config/build-policy.json")
        with self.assertRaises(SystemExit):
            build_platform.image_profile(policy, "generic-amd64", "arm64")

    def test_target_rejects_pre_rebrand_kernel_name(self):
        policy = json.loads((ROOT / "config/build-policy.json").read_text())
        policy["targets"]["arm64"]["kernel"] = "pfSense"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "post-rebrand"):
                build_platform.load_policy(path)

    def test_staged_arm_pair_produces_a_valid_unsigned_channel_payload(self):
        system_id, packages_id = "a" * 64, "b" * 64
        jail_sha = "c" * 64
        signing_key = hashlib.sha256(
            (ROOT / "config/channel-signing-public.pem").read_bytes()
        ).hexdigest()
        common = {
            "architecture": "arm64", "package_arch": "aarch64",
            "platform": "generic-arm64-uefi", "generation": 7,
        }
        system_marker = {
            **common, "schema_version": "freesense.artifact/v1",
            "stage": "system", "fingerprint": system_id,
            "inputs": {
                "system": system_id, "platform": "d" * 64,
                "source": "1" * 40, "system_ports": "2" * 40,
                "os_definition": "3" * 40, "freebsd": "4" * 40,
                "ports": "5" * 40, "worker_image": "e" * 64,
                "worker_tools": "f" * 64, "freebsd_pin_id": "8" * 64,
                "signing_public_key": signing_key, "package_train": "1.1",
                "jail_object": "inputs/sha256/" + jail_sha,
            },
        }
        packages_marker = {
            **common, "schema_version": "freesense.artifact/v1",
            "stage": "packages", "fingerprint": packages_id, "generation": 8,
            "inputs": {
                "system": system_id, "built_against_system": system_id,
                "freebsd_pin_id": "8" * 64, "package_train": "1.1",
                "packages": "6" * 40,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system_path, packages_path, output = (
                root / "system.json", root / "packages.json", root / "closure.json"
            )
            system_path.write_text(json.dumps(system_marker), encoding="utf-8")
            packages_path.write_text(json.dumps(packages_marker), encoding="utf-8")
            argv = ["staged_system_closure.py", "--target", "arm64",
                    "--marker", str(system_path), "--packages-marker", str(packages_path),
                    "--output", str(output)]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                staged_closure, "pin_target",
                return_value={"ready": True, "jail_seed": {"sha256": jail_sha}},
            ):
                staged_closure.main()
            closure = json.loads(output.read_text(encoding="utf-8"))
        payload = json.loads(base64.b64decode(closure["payload_base64"]))
        channel = payload["channels"]["devel"]
        self.assertEqual((channel["architecture"], channel["package_arch"]),
                         ("arm64", "aarch64"))
        self.assertEqual(channel["system"]["fingerprint"], system_id)
        self.assertEqual(channel["packages"]["fingerprint"], packages_id)
        self.assertEqual(closure["signature_base64"], "")


if __name__ == "__main__":
    unittest.main()
