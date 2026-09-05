"""Protect the experimental worker's credential and publication boundaries."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("experimental_iso", ROOT / "scripts/experimental/prepare-iso.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExperimentalIsoTests(unittest.TestCase):
    def values(self):
        values = {key: "a" * 64 for key in (
            "fingerprint", "packages_fingerprint", "payload_sha256", "artifact_image_sha256",
            "artifact_worker_tools_sha256", "artifact_platform")}
        values.update({key: "b" * 40 for key in ("artifact_source_sha", "artifact_freebsd_sha", "artifact_ports_sha")})
        values.update(verified="true", packages_verified="true", abi="FreeBSD:16:amd64",
                      altabi="freebsd:16:x86:64", package_train="1.1", release_version="1.1.0",
                      generation=1, osversion=1600020, payload_base64="e30=", signature_base64="c2ln",
                      artifact_jail_object="inputs/sha256/" + "a" * 64)
        return values

    def test_unverified_pair_is_rejected(self):
        for key in ("verified", "packages_verified"):
            values = self.values()
            values[key] = "false"
            with self.assertRaises(ValueError):
                module.render(values)

    def test_no_storage_writer_or_private_key_material(self):
        worker = module.render(self.values())
        self.assertNotIn("rclone", worker)
        self.assertNotIn("AWS_", worker)
        self.assertNotIn("FREESENSE_REPO_SIGNING_KEY", worker)
        self.assertNotIn("sign_repository()", worker)
        self.assertIn("verify_repository()", worker)
        self.assertIn("verify_release_channel", worker)
        self.assertIn("pkg checksum -q -c", worker)
        self.assertIn('OSVERSION="${required_osversion}"', worker)
        self.assertIn("export OSVERSION=1600020", worker)
        self.assertIn("/root/experiment-output", worker)


if __name__ == "__main__":
    unittest.main()
