"""Exercise the joint canary gate against a complete in-memory object store."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_multiarch_canary as canary


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def checksum(value):
    return hashlib.sha256(value).hexdigest()


def fixture():
    policy = canary.load_policy()
    base = policy["public_base_url"]
    plan = {"pair_fingerprint": "a" * 64, "freebsd_pin": "b" * 64, "generation": 1, "targets": {}}
    documents, objects = {}, {}
    for arch, abi in canary.ARCHES.items():
        target = {}
        for component in ("system", "packages"):
            fingerprint = checksum(f"{arch}-{component}".encode())
            target[component] = {
                component: fingerprint, "platform": checksum(arch.encode()),
                "source_sha": "c" * 40, "ports_sha": "d" * 40,
                "os_base_sha": "e" * 40, "freebsd_sha": "f" * 40,
                "package_train": "1.1", "freebsd_pin_id": checksum(arch.encode()),
                "package_arch": "amd64" if arch == "amd64" else "aarch64",
                "abi": abi, "binary_seed_object": "inputs/sha256/" + "1" * 64,
                "signing_public_key_sha256": "2" * 64,
            }
            suffix = "system" if component == "system" else "packages/1.1"
            url = f"{base}/artifacts/{suffix}/{fingerprint}"
            body = f"official-rust-{arch}".encode()
            provenance = {"abi": abi, "binary_seed": target[component]["binary_seed_object"],
                          "packages": [{"name": "rust", "version": "1.0", "origin": "lang/rust", "abi": abi, "file": "All/rust.pkg",
                                        "sha256": checksum(body), "size": len(body)}]}
            raw = encoded(provenance)
            objects[url + f"/{target[component]['package_arch']}/upstream-provenance.json"] = raw
            objects[url + f"/{target[component]['package_arch']}/All/rust.pkg"] = body
            objects[url + f"/{target[component]['package_arch']}/packagesite.pkg"] = encoded([
                {"name": "rust", "version": "1.0", "origin": "lang/rust", "arch": abi,
                 "repopath": "All/rust.pkg", "sum": checksum(body), "deps": {}}
            ])
            objects[url + "/complete.json"] = encoded({
                "stage": component, "fingerprint": fingerprint, "architecture": arch,
                "inputs": {"freebsd_pin_id": target[component]["freebsd_pin_id"],
                           "upstream_provenance_sha256": checksum(raw)},
            })
        plan["targets"][arch] = target
        release = {"schema_version": "freesense.download/v4", "channel": "devel",
                   "version": "1.1.0", "release_id": "1.1.0-g1", "generation": 1,
                   "bundle_fingerprint": checksum(arch.encode()), "system": target["system"]["system"],
                   "architecture": arch, "package_arch": target["system"]["package_arch"],
                   "published_at": "2026-09-07T00:00:00Z", "artifacts": [],
                   "provenance": {"source": "c" * 40, "ports": "d" * 40,
                                  "os_definition": "e" * 40, "freebsd": "f" * 40}}
        # The public contract requires the installer first.
        identities = sorted(canary.expected_artifacts(policy, arch), key=lambda x: (x[0] != "installer", str(x)))
        for kind, platform, filesystem, fmt in identities:
            fingerprint = checksum(str((arch, kind, platform, filesystem)).encode())
            stage = {"installer": "iso", "cloud": "cloud", "appliance": "appliance"}[kind]
            url = f"{base}/artifacts/{stage}/{fingerprint}/complete.json"
            body = (fingerprint + fmt).encode()
            filename = f"{arch}-{platform}-{filesystem}.{fmt}"
            item = {"kind": kind, "platform": platform, "filesystem": filesystem, "format": fmt,
                    "compression": "none" if kind == "installer" else "xz",
                    "target_models": [platform], "partition_scheme": "mbr" if kind == "appliance" else "gpt",
                    "firmware": ["uefi"], "capabilities": {}, "boot_inputs": {},
                    "hardware_verification": "unverified", "artifact_fingerprint": fingerprint,
                    "sha256": checksum(body), "size": len(body), "file": filename, "marker_url": url,
                    "url": f"https://downloads.freesense.org/v1/releases/devel/1.1.0-g1/{filename}"}
            marker = {"schema_version": {"cloud": "freesense.cloud-image/v1", "appliance": "freesense.appliance/v1",
                                         "iso": "freesense.iso/v2" if arch == "amd64" else "freesense.installer/v1"}[stage],
                      "fingerprint": fingerprint, "architecture": arch,
                      "bundle_fingerprint": release["bundle_fingerprint"],
                      "inputs": {"system": target["system"]["system"], "packages": target["packages"]["packages"],
                                 "platform": target["system"]["platform"]},
                      "hardware_verification": "unverified"}
            file_record = {key: item[key] for key in ("file", "sha256", "size")}
            if kind == "cloud":
                marker["files"] = json.loads(objects[url])["files"] + [file_record] if url in objects else [file_record]
            else:
                marker.update(file_record)
            objects[url] = encoded(marker)
            objects[url.removesuffix("complete.json") + filename] = body
            release["artifacts"].append(item)
        documents[arch] = encoded(release)
    return plan, documents, policy, objects


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.documents, self.policy, self.objects = fixture()
        self.original = copy.deepcopy(self.objects)

    def verify(self):
        def check(url, expected_sha, expected_size, *, catalog_checksum=None):
            body = self.objects[url]
            if len(body) != expected_size or checksum(body) != expected_sha:
                raise ValueError("immutable artifact byte verification failed")
            if catalog_checksum is not None and catalog_checksum != checksum(body):
                raise ValueError("signed catalogue checksum mismatch")
        return canary.verify(self.plan, self.documents, self.policy, read=self.objects.__getitem__, check_file=check,
                             check_signature=lambda raw, key: json.loads(raw))

    def test_complete_pair_reports_every_image_and_upstream_reuse_without_writes(self):
        result = self.verify()
        self.assertFalse(result["publication_enabled"])
        self.assertEqual(len(result["architectures"]["amd64"]["artifact_documents"]), 3)
        self.assertEqual(len(result["architectures"]["arm64"]["artifact_documents"]), 3)
        for target in result["architectures"].values():
            self.assertEqual(target["reused_packages"], {"system": 1, "packages": 1})
        self.assertEqual(self.objects, self.original)

    def test_missing_architecture_and_missing_pi_fail(self):
        del self.documents["arm64"]
        with self.assertRaisesRegex(ValueError, "both complete"):
            self.verify()
        self.setUp()
        document = json.loads(self.documents["arm64"])
        document["artifacts"].pop()
        self.documents["arm64"] = encoded(document)
        with self.assertRaisesRegex(ValueError, "missing artifacts"):
            self.verify()

    def test_wrong_pair_and_wrong_architecture_markers_fail(self):
        for field in ("architecture", "packages", "platform"):
            with self.subTest(field=field):
                self.setUp()
                url = json.loads(self.documents["arm64"])["artifacts"][0]["marker_url"]
                marker = json.loads(self.objects[url])
                (marker if field == "architecture" else marker["inputs"])[field] = "wrong"
                self.objects[url] = encoded(marker)
                with self.assertRaisesRegex(ValueError, "selected complete pair"):
                    self.verify()

    def test_release_documents_must_share_the_reserved_generation(self):
        document = json.loads(self.documents["arm64"])
        document["generation"] = 2
        document["release_id"] = "1.1.0-g2"
        for item in document["artifacts"]:
            item["url"] = item["url"].replace("1.1.0-g1", "1.1.0-g2")
        self.documents["arm64"] = encoded(document)
        with self.assertRaisesRegex(ValueError, "shared pair generation"):
            self.verify()

    def test_reused_artifact_can_keep_its_original_bundle_identity(self):
        url = json.loads(self.documents["arm64"])["artifacts"][0]["marker_url"]
        marker = json.loads(self.objects[url])
        marker["bundle_fingerprint"] = "0" * 64
        self.objects[url] = encoded(marker)
        # A change to another image recipe changes the release bundle while
        # this immutable image still binds the exact selected component pair.
        self.verify()

    def test_corrupt_image_package_or_provenance_fails_without_writes(self):
        for suffix in (".img", "All/rust.pkg", "upstream-provenance.json"):
            with self.subTest(suffix=suffix):
                self.setUp()
                key = next(key for key in self.objects if key.endswith(suffix))
                self.objects[key] = b"corrupted"
                before = copy.deepcopy(self.objects)
                with self.assertRaises(ValueError):
                    self.verify()
                self.assertEqual(self.objects, before)

    def test_filename_cannot_escape_artifact_directory(self):
        document = json.loads(self.documents["arm64"])
        artifact = document["artifacts"][0]
        artifact["file"] = "../escape.img"
        artifact["url"] = "https://downloads.freesense.org/v1/releases/devel/1.1.0-g1/../escape.img"
        self.documents["arm64"] = encoded(document)
        with self.assertRaisesRegex(ValueError, "filename"):
            self.verify()

    def test_stream_checks_hash_and_length(self):
        body = b"image bytes"
        for expected_sha, expected_size, passes in ((checksum(body), len(body), True),
                                                   ("0" * 64, len(body), False),
                                                   (checksum(body), len(body) - 1, False),
                                                   (checksum(body), len(body) + 1, False)):
            with self.subTest(sha=expected_sha, size=expected_size):
                with mock.patch.object(canary.urllib.request, "urlopen", return_value=io.BytesIO(body)):
                    if passes:
                        canary.verify_file("https://example.invalid/image", expected_sha, expected_size)
                    else:
                        with self.assertRaises(ValueError):
                            canary.verify_file("https://example.invalid/image", expected_sha, expected_size)

    def test_stream_verifies_signed_blake_checksum_independently_of_provenance(self):
        body = b"official package bytes"
        for kind, value in ((1, hashlib.sha256(body).hexdigest()),
                            (2, canary.zbase32(hashlib.blake2b(body, digest_size=64).digest())),
                            (5, canary.zbase32(hashlib.blake2s(body, digest_size=32).digest()))):
            with self.subTest(kind=kind):
                with mock.patch.object(canary.urllib.request, "urlopen", return_value=io.BytesIO(body)):
                    canary.verify_file("https://example.invalid/pkg", checksum(body), len(body), catalog_checksum=f"{kind}${value}")
        with mock.patch.object(canary.urllib.request, "urlopen", return_value=io.BytesIO(body)):
            with self.assertRaisesRegex(ValueError, "signed catalogue"):
                canary.verify_file("https://example.invalid/pkg", checksum(body), len(body), catalog_checksum="0" * 64)
