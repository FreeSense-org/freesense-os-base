import base64
import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "r2_retention", ROOT / "scripts" / "r2_retention.py"
)
assert SPEC and SPEC.loader
retention = importlib.util.module_from_spec(SPEC)
sys.modules["r2_retention"] = retention
SPEC.loader.exec_module(retention)


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
OLD = "2026-07-01T00:00:00Z"
RECENT = "2026-07-25T10:00:00Z"


def fingerprint(number: int) -> str:
    return f"{number:064x}"


def object_record(key: str, modified: str = OLD, size: int = 100) -> dict:
    return {
        "key": key,
        "size": size,
        "last_modified": modified,
        "etag": f'"{fingerprint(size)}"',
    }


def system_marker(number: int, generation: int, train: str = "1.1") -> dict:
    value = fingerprint(number)
    return {
        "schema_version": "freesense.artifact/v1",
        "stage": "system",
        "fingerprint": value,
        "generation": generation,
        "inputs": {
            "system": value,
            "package_train": train,
            "worker_image": fingerprint(900),
        },
    }


def packages_marker(
    number: int, generation: int, system: str, train: str = "1.1"
) -> dict:
    return {
        "schema_version": "freesense.artifact/v1",
        "stage": "packages",
        "fingerprint": fingerprint(number),
        "generation": generation,
        "inputs": {
            "package_train": train,
            "system": system,
            "built_against_system": system,
        },
    }


def iso_marker(
    number: int,
    generation: int,
    system: str,
    channel: str = "devel",
    packages: str | None = None,
    legacy: bool = False,
) -> dict:
    marker = {
        "schema_version": "freesense.iso/v1" if legacy else "freesense.iso/v2",
        "fingerprint": fingerprint(number),
        "generation": generation,
        "system": system,
        "sha256": fingerprint(number + 1000),
        "size": 1024,
        "file": "FreeSense.iso",
        "inputs": {"channel": channel, "package_train": "1.0" if channel == "stable" else "1.1"},
    }
    if not legacy:
        marker["inputs"]["packages"] = packages or fingerprint(number + 2000)
    return marker


def cloud_marker(number: int, generation: int, system: str, packages: str) -> dict:
    return {
        "schema_version": "freesense.cloud-image/v1",
        "fingerprint": fingerprint(number),
        "bundle_fingerprint": fingerprint(number + 3000),
        "generation": generation,
        "channel": "devel",
        "inputs": {
            "system": system, "packages": packages, "package_train": "1.1"
        },
        "files": [
            {"format": "qcow2"}, {"format": "raw"},
        ],
    }


def add_artifact(inventory: dict, prefix: str, marker: dict) -> None:
    marker_key = prefix + "/complete.json"
    inventory["objects"].extend(
        [
            object_record(prefix + "/payload.pkg"),
            object_record(marker_key, size=200),
        ]
    )
    inventory["documents"][marker_key] = marker


def inventory(kind: str, bucket: str) -> dict:
    return {
        "schema_version": retention.INVENTORY_SCHEMA,
        "kind": kind,
        "bucket": bucket,
        "captured_at": NOW.isoformat(),
        "objects": [],
        "documents": {},
    }


class RetentionPlanTests(unittest.TestCase):
    def test_latest_four_development_bundles_keep_iso_and_cloud(self):
        build = inventory("build", "builds")
        downloads = inventory("downloads", "downloads")
        system = system_marker(50, 5)
        packages = packages_marker(60, 5, system["fingerprint"])
        add_artifact(build, f"v1/artifacts/system/{system['fingerprint']}", system)
        add_artifact(
            build,
            f"v1/artifacts/packages/1.1/{packages['fingerprint']}",
            packages,
        )
        images = []
        for generation in range(1, 6):
            bundle = fingerprint(5000 + generation)
            iso = iso_marker(
                100 + generation, generation, system["fingerprint"],
                packages=packages["fingerprint"],
            )
            iso["bundle_fingerprint"] = bundle
            cloud_ufs = cloud_marker(
                200 + generation, generation, system["fingerprint"],
                packages["fingerprint"],
            )
            cloud_zfs = cloud_marker(
                300 + generation, generation, system["fingerprint"],
                packages["fingerprint"],
            )
            cloud_ufs["bundle_fingerprint"] = bundle
            cloud_zfs["bundle_fingerprint"] = bundle
            cloud_ufs["filesystem"] = "ufs"
            cloud_zfs["filesystem"] = "zfs"
            add_artifact(build, f"v1/artifacts/iso/{iso['fingerprint']}", iso)
            add_artifact(build, f"v1/artifacts/cloud/{cloud_ufs['fingerprint']}", cloud_ufs)
            add_artifact(build, f"v1/artifacts/cloud/{cloud_zfs['fingerprint']}", cloud_zfs)
            images.append((iso, cloud_ufs, cloud_zfs))
        manifest = {
            "schema_version": "freesense.channels/v3",
            "channels": {
                "devel": {
                    "package_train": "1.1",
                    "system": {"fingerprint": system["fingerprint"]},
                    "packages": {"fingerprint": packages["fingerprint"]},
                }
            },
        }
        report = retention.plan_retention(
            build, downloads, manifest, set(), NOW,
            keep_devel=4, grace=timedelta(days=7),
        )
        candidates = {item["prefix"] for item in report["candidates"]}
        self.assertIn(
            f"v1/artifacts/iso/{images[0][0]['fingerprint']}/", candidates
        )
        self.assertIn(
            f"v1/artifacts/cloud/{images[0][1]['fingerprint']}/", candidates
        )
        self.assertIn(
            f"v1/artifacts/cloud/{images[0][2]['fingerprint']}/", candidates
        )
        for iso, cloud_ufs, cloud_zfs in images[1:]:
            self.assertNotIn(f"v1/artifacts/iso/{iso['fingerprint']}/", candidates)
            self.assertNotIn(f"v1/artifacts/cloud/{cloud_ufs['fingerprint']}/", candidates)
            self.assertNotIn(f"v1/artifacts/cloud/{cloud_zfs['fingerprint']}/", candidates)

    def test_packages_follow_current_channel_and_retained_iso_references(self):
        build = inventory("build", "builds")
        downloads = inventory("downloads", "downloads")
        system = system_marker(100, 100)
        add_artifact(
            build, f"v1/artifacts/system/{system['fingerprint']}", system
        )
        packages = []
        for generation in range(1, 7):
            marker = packages_marker(
                200 + generation, generation, system["fingerprint"]
            )
            packages.append(marker)
            add_artifact(
                build,
                f"v1/artifacts/packages/1.1/{marker['fingerprint']}",
                marker,
            )
        for generation in range(1, 5):
            marker = iso_marker(
                300 + generation,
                generation,
                system["fingerprint"],
                packages=packages[-1]["fingerprint"],
            )
            add_artifact(
                build, f"v1/artifacts/iso/{marker['fingerprint']}", marker
            )
        manifest = {
            "schema_version": "freesense.channels/v3",
            "channels": {
                "devel": {
                    "package_train": "1.1",
                    "system": {"fingerprint": system["fingerprint"]},
                    "packages": {"fingerprint": packages[-1]["fingerprint"]},
                }
            },
        }
        report = retention.plan_retention(
            build,
            downloads,
            manifest,
            set(),
            NOW,
            keep_devel=4,
            grace=timedelta(days=7),
        )
        candidate_prefixes = {item["prefix"] for item in report["candidates"]}
        for marker in packages[:-1]:
            self.assertIn(
                f"v1/artifacts/packages/1.1/{marker['fingerprint']}/",
                candidate_prefixes,
            )
        self.assertNotIn(
            f"v1/artifacts/packages/1.1/{packages[-1]['fingerprint']}/",
            candidate_prefixes,
        )

    def test_legacy_retained_iso_conservatively_protects_all_development_packages(self):
        build = inventory("build", "builds")
        downloads = inventory("downloads", "downloads")
        system = system_marker(10, 10)
        add_artifact(
            build, f"v1/artifacts/system/{system['fingerprint']}", system
        )
        packages = []
        for generation in range(1, 4):
            marker = packages_marker(
                20 + generation, generation, system["fingerprint"]
            )
            packages.append(marker)
            add_artifact(
                build,
                f"v1/artifacts/packages/1.1/{marker['fingerprint']}",
                marker,
            )
        legacy = iso_marker(
            30, 30, system["fingerprint"], legacy=True
        )
        add_artifact(
            build, f"v1/artifacts/iso/{legacy['fingerprint']}", legacy
        )
        manifest = {
            "schema_version": "freesense.channels/v3",
            "channels": {
                "devel": {
                    "package_train": "1.1",
                    "system": {"fingerprint": system["fingerprint"]},
                    "packages": {"fingerprint": packages[-1]["fingerprint"]},
                }
            },
        }
        report = retention.plan_retention(
            build,
            downloads,
            manifest,
            set(),
            NOW,
            keep_devel=4,
            grace=timedelta(days=7),
        )
        package_candidates = [
            item
            for item in report["candidates"]
            if "/artifacts/packages/1.1/" in item["prefix"]
        ]
        self.assertEqual(package_candidates, [])
        self.assertTrue(
            any("legacy Development ISO" in warning for warning in report["warnings"])
        )

    def test_confirmation_requires_matching_observations_twenty_hours_apart(self):
        report = {
            "schema_version": retention.REPORT_SCHEMA,
            "mode": "two-run-confirmation",
            "candidates": [
                {
                    "bucket": "builds",
                    "keys": ["v1/artifacts/system/" + fingerprint(1) + "/file"],
                }
            ],
            "totals": {"candidate_objects": 1, "candidate_bytes": 100},
        }
        first = retention.confirmation(report, None, NOW)
        self.assertFalse(first["ready"])
        too_soon = retention.confirmation(
            report, first["state"], NOW + timedelta(hours=1)
        )
        self.assertFalse(too_soon["ready"])
        self.assertEqual(too_soon["state"]["observations"], 1)
        second = retention.confirmation(
            report, first["state"], NOW + timedelta(hours=24)
        )
        self.assertTrue(second["ready"])
        self.assertEqual(second["state"]["observations"], 2)

        changed = {
            **report,
            "candidates": [
                {
                    "bucket": "builds",
                    "keys": ["v1/artifacts/system/" + fingerprint(2) + "/file"],
                }
            ],
        }
        reset = retention.confirmation(
            changed, first["state"], NOW + timedelta(hours=24)
        )
        self.assertFalse(reset["ready"])
        self.assertEqual(reset["state"]["observations"], 1)

    def test_deletion_keys_enforce_bucket_and_stable_boundaries(self):
        report = {
            "schema_version": retention.REPORT_SCHEMA,
            "mode": "two-run-confirmation",
            "candidates": [
                {
                    "bucket": "builds",
                    "bytes": 100,
                    "keys": [
                        "v1/artifacts/system/" + fingerprint(1) + "/file",
                        "v1/smoke/broker/" + "a" * 40 + ".json",
                    ],
                },
                {
                    "bucket": "downloads",
                    "bytes": 100,
                    "keys": ["v1/releases/devel/1.1.0-g1/FreeSense.iso"],
                },
            ],
            "totals": {"candidate_objects": 3, "candidate_bytes": 200},
        }
        self.assertEqual(
            len(retention.deletion_keys(report, "build", "builds")), 2
        )
        self.assertEqual(
            retention.deletion_keys(report, "downloads", "downloads"),
            ["v1/releases/devel/1.1.0-g1/FreeSense.iso"],
        )
        report["candidates"][1]["keys"] = [
            "v1/releases/stable/1.0.4/FreeSense.iso"
        ]
        with self.assertRaisesRegex(SystemExit, "protected object"):
            retention.deletion_keys(report, "downloads", "downloads")

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required")
    def test_signed_manifest_must_verify_before_planning(self):
        payload = {
            "schema_version": "freesense.channels/v3",
            "channels": {},
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_key = root / "private.pem"
            public_key = root / "public.pem"
            payload_path = root / "payload.json"
            signature_path = root / "signature.bin"
            payload_path.write_bytes(encoded)
            subprocess.run(
                [
                    "openssl",
                    "genpkey",
                    "-algorithm",
                    "RSA",
                    "-pkeyopt",
                    "rsa_keygen_bits:2048",
                    "-out",
                    str(private_key),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "openssl",
                    "pkey",
                    "-in",
                    str(private_key),
                    "-pubout",
                    "-out",
                    str(public_key),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-sign",
                    str(private_key),
                    "-out",
                    str(signature_path),
                    str(payload_path),
                ],
                check=True,
                capture_output=True,
            )
            envelope = {
                "schema_version": "freesense.repositories/v3",
                "payload": base64.b64encode(encoded).decode(),
                "signature": base64.b64encode(signature_path.read_bytes()).decode(),
            }
            self.assertEqual(
                retention.verify_manifest(envelope, public_key), payload
            )
            envelope["signature"] = base64.b64encode(b"invalid").decode()
            with self.assertRaisesRegex(
                SystemExit, "repository manifest signature is invalid"
            ):
                retention.verify_manifest(envelope, public_key)

    def test_stable_is_permanent_and_only_old_development_falls_out(self):
        build = inventory("build", "builds")
        downloads = inventory("downloads", "downloads")

        stable_system = system_marker(100, 100, "1.0")
        stable_packages = packages_marker(
            200, 100, stable_system["fingerprint"], "1.0"
        )
        stable_iso = iso_marker(
            300,
            100,
            stable_system["fingerprint"],
            "stable",
            stable_packages["fingerprint"],
        )
        add_artifact(
            build,
            f"v1/artifacts/system/{stable_system['fingerprint']}",
            stable_system,
        )
        add_artifact(
            build,
            f"v1/artifacts/packages/1.0/{stable_packages['fingerprint']}",
            stable_packages,
        )
        add_artifact(
            build,
            f"v1/artifacts/iso/{stable_iso['fingerprint']}",
            stable_iso,
        )

        systems = {}
        packages = {}
        isos = {}
        for generation in range(1, 7):
            systems[generation] = system_marker(1000 + generation, generation)
            packages[generation] = packages_marker(
                2000 + generation,
                generation,
                systems[max(1, generation - 1)]["fingerprint"],
            )
            isos[generation] = iso_marker(
                3000 + generation,
                generation,
                systems[generation]["fingerprint"],
                packages=packages[generation]["fingerprint"],
            )
            add_artifact(
                build,
                f"v1/artifacts/system/{systems[generation]['fingerprint']}",
                systems[generation],
            )
            add_artifact(
                build,
                f"v1/artifacts/packages/1.1/{packages[generation]['fingerprint']}",
                packages[generation],
            )
            add_artifact(
                build,
                f"v1/artifacts/iso/{isos[generation]['fingerprint']}",
                isos[generation],
            )
            release = f"1.1.0-g{generation}"
            downloads["objects"].append(
                object_record(f"v1/releases/devel/{release}/FreeSense.iso", size=500)
            )

        downloads["objects"].append(
            object_record("v1/releases/stable/1.0.4/FreeSense.iso", size=500)
        )
        old_smoke = "v1/smoke/broker/" + "1" * 40 + ".json"
        new_smoke = "v1/smoke/broker/" + "2" * 40 + ".json"
        for target in (build, downloads):
            target["objects"].extend(
                [
                    object_record(old_smoke),
                    object_record(new_smoke, modified=RECENT),
                ]
            )
        build["documents"]["v1/releases/devel.json"] = {
            "channel": "devel",
            "release_id": "1.1.0-g6",
        }

        protected_input = fingerprint(900)
        unused_input = fingerprint(901)
        build["objects"].extend(
            [
                object_record(f"v1/inputs/sha256/{protected_input}", size=1000),
                object_record(f"v1/inputs/sha256/{unused_input}", size=1000),
                object_record(
                    f"v1/artifacts/system/{fingerprint(9999)}/partial.pkg",
                    size=1000,
                ),
                object_record(
                    f"v1/artifacts/iso/{fingerprint(9998)}/partial.iso",
                    modified=RECENT,
                    size=1000,
                ),
            ]
        )

        unknown = system_marker(7777, 1, "2.0")
        add_artifact(
            build, f"v1/artifacts/system/{unknown['fingerprint']}", unknown
        )

        manifest = {
            "schema_version": "freesense.channels/v3",
            "channels": {
                "stable": {
                    "package_train": "1.0",
                    "system": {"fingerprint": stable_system["fingerprint"]},
                    "packages": {"fingerprint": stable_packages["fingerprint"]},
                },
                "devel": {
                    "package_train": "1.1",
                    "system": {"fingerprint": systems[6]["fingerprint"]},
                    "packages": {"fingerprint": packages[6]["fingerprint"]},
                },
            },
        }
        report = retention.plan_retention(
            build,
            downloads,
            manifest,
            set(),
            NOW,
            keep_devel=4,
            grace=timedelta(days=7),
        )

        candidates = {
            (item["bucket"], item["prefix"]): item["reason"]
            for item in report["candidates"]
        }
        self.assertIn(
            ("builds", f"v1/artifacts/system/{systems[1]['fingerprint']}/"),
            candidates,
        )
        self.assertNotIn(
            ("builds", f"v1/artifacts/system/{systems[2]['fingerprint']}/"),
            candidates,
            "retained Packages generation 3 keeps its build System",
        )
        for generation in (1, 2):
            self.assertIn(
                (
                    "builds",
                    f"v1/artifacts/packages/1.1/{packages[generation]['fingerprint']}/",
                ),
                candidates,
            )
            self.assertIn(
                ("builds", f"v1/artifacts/iso/{isos[generation]['fingerprint']}/"),
                candidates,
            )
            self.assertIn(
                ("downloads", f"v1/releases/devel/1.1.0-g{generation}/"),
                candidates,
            )
        self.assertIn(("builds", f"v1/inputs/sha256/{unused_input}"), candidates)
        self.assertNotIn(
            ("builds", f"v1/inputs/sha256/{protected_input}"), candidates
        )
        self.assertIn(
            ("builds", f"v1/artifacts/system/{fingerprint(9999)}/"), candidates
        )
        self.assertNotIn(
            ("builds", f"v1/artifacts/iso/{fingerprint(9998)}/"), candidates
        )
        self.assertFalse(
            any("stable" in prefix for _, prefix in candidates),
            "Stable downloads must never be candidates",
        )
        self.assertFalse(
            any(
                stable_system["fingerprint"] in prefix
                or stable_packages["fingerprint"] in prefix
                or stable_iso["fingerprint"] in prefix
                for _, prefix in candidates
            ),
            "Stable build artifacts must never be candidates",
        )
        self.assertTrue(
            any("unknown package train" in warning for warning in report["warnings"])
        )
        self.assertEqual(report["mode"], "two-run-confirmation")
        self.assertIn(("builds", old_smoke), candidates)
        self.assertIn(("downloads", old_smoke), candidates)
        self.assertNotIn(("builds", new_smoke), candidates)
        self.assertNotIn(("downloads", new_smoke), candidates)

    def test_invalid_completion_marker_aborts_the_entire_plan(self):
        build = inventory("build", "builds")
        downloads = inventory("downloads", "downloads")
        marker = system_marker(1, 1)
        marker["fingerprint"] = fingerprint(2)
        add_artifact(build, f"v1/artifacts/system/{fingerprint(1)}", marker)
        manifest = {
            "schema_version": "freesense.channels/v3",
            "channels": {},
        }
        with self.assertRaisesRegex(
            SystemExit, "completion marker conflicts with its artifact identity"
        ):
            retention.plan_retention(
                build,
                downloads,
                manifest,
                set(),
                NOW,
                keep_devel=4,
                grace=timedelta(days=7),
            )


if __name__ == "__main__":
    unittest.main()
