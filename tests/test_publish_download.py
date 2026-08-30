from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publish = load_script("publish_download", "scripts/publish_download.py")
migrate = load_script("migrate_downloads", "scripts/migrate_downloads.py")

FINGERPRINT = "a" * 64
SYSTEM = "b" * 64
SHA = "c" * 40
ISO_SHA = "d" * 64
PACKAGES_FINGERPRINT = "e" * 64
CLOUD_FINGERPRINT = "f" * 64
ZFS_CLOUD_FINGERPRINT = "6" * 64
BUNDLE_FINGERPRINT = "1" * 64
BASE_URL = "https://pkg.freesense.org/v1"
DOWNLOAD_BASE_URL = "https://downloads.freesense.org/v1"


def marker(channel="stable", generation=2, fingerprint=FINGERPRINT,
           packages=PACKAGES_FINGERPRINT, legacy=False):
    version = "1.0.0" if channel == "stable" else "1.1.0"
    file = (f"FreeSense-{version}-amd64.iso" if channel == "stable"
            else f"FreeSense-{version}-g{generation}-amd64.iso")
    value = {
        "schema_version": "freesense.iso/v1" if legacy else "freesense.iso/v2",
        "fingerprint": fingerprint,
        "bundle_fingerprint": BUNDLE_FINGERPRINT,
        "system": SYSTEM,
        "generation": generation,
        "file": file,
        "sha256": ISO_SHA,
        "size": 1024,
        "inputs": {"channel": channel},
    }
    if not legacy:
        value["inputs"]["packages"] = packages
    return value


def cloud_marker(channel="stable", generation=2, filesystem="ufs",
                 fingerprint=None, bundle=BUNDLE_FINGERPRINT):
    fingerprint = fingerprint or (
        CLOUD_FINGERPRINT if filesystem == "ufs" else ZFS_CLOUD_FINGERPRINT
    )
    version = "1.0.0" if channel == "stable" else "1.1.0"
    prefix = f"FreeSense-{version}" if channel == "stable" else f"FreeSense-{version}-g{generation}"
    virtual_size = (16 if filesystem == "ufs" else 32) * 1024**3
    return {
        "schema_version": "freesense.cloud-image/v1",
        "fingerprint": fingerprint,
        "bundle_fingerprint": bundle,
        "generation": generation,
        "channel": channel,
        "filesystem": filesystem,
        "disk": {"virtual_size": virtual_size},
        "inputs": {"system": SYSTEM, "packages": PACKAGES_FINGERPRINT},
        "files": [
            {"kind": "cloud", "format": "qcow2", "file": f"{prefix}-amd64-{filesystem}.qcow2.xz",
             "sha256": ("2" if filesystem == "ufs" else "7") * 64,
             "size": 2048, "virtual_size": virtual_size},
            {"kind": "cloud", "format": "raw", "file": f"{prefix}-amd64-{filesystem}.raw.xz",
             "sha256": ("3" if filesystem == "ufs" else "8") * 64,
             "size": 3072, "virtual_size": virtual_size},
        ],
    }


def release(channel="stable", generation=2, fingerprint=BUNDLE_FINGERPRINT, legacy=False):
    version = "1.0.0" if channel == "stable" else "1.1.0"
    item = marker(channel, generation, fingerprint)
    artifact = f"{BASE_URL}/artifacts/iso/{FINGERPRINT}"
    if not legacy:
        release_id = version if channel == "stable" else f"{version}-g{generation}"
        cloud = cloud_marker(channel, generation)
        artifacts = [{
            "kind": "installer", "format": "iso", "filesystem": None,
            "compression": "none", "file": item["file"],
            "marker_url": artifact + "/complete.json",
            "url": f"{DOWNLOAD_BASE_URL}/releases/{channel}/{release_id}/{item['file']}",
            "sha256": item["sha256"], "size": item["size"],
            "build_fingerprint": FINGERPRINT,
        }]
        for cloud_file in cloud["files"]:
            artifacts.append({
                "kind": "cloud", "format": cloud_file["format"], "filesystem": "ufs",
                "compression": "xz", "file": cloud_file["file"],
                "marker_url": f"{BASE_URL}/artifacts/cloud/{CLOUD_FINGERPRINT}/complete.json",
                "url": f"{DOWNLOAD_BASE_URL}/releases/{channel}/{release_id}/{cloud_file['file']}",
                "sha256": cloud_file["sha256"], "size": cloud_file["size"],
                "virtual_size": cloud_file["virtual_size"],
                "build_fingerprint": CLOUD_FINGERPRINT,
            })
        return {
            "schema_version": publish.V3_DOWNLOAD_SCHEMA, "version": version,
            "release_id": release_id, "display_name": "test",
            "support_tier": "supported" if channel == "stable" else "development",
            "channel": channel, "generation": generation,
            "bundle_fingerprint": fingerprint, "system": SYSTEM,
            "architecture": "amd64", "package_arch": "amd64",
            "platform": "generic-amd64", "firmware": ["bios", "uefi"],
            "capabilities": {"bios": True, "uefi": True, "iso": True,
                             "installer_img": False, "cloud_init": True},
            "artifacts": artifacts, "published_at": "2026-07-22T22:09:10Z",
            "provenance": {
                "source": SHA, "system_ports": SHA, "packages": SHA,
                "ports": SHA, "os_definition": SHA, "freebsd": SHA,
            }, "changes": [],
        }
    if fingerprint == BUNDLE_FINGERPRINT:
        fingerprint = FINGERPRINT
        item["fingerprint"] = fingerprint
    value = {
        "schema_version": publish.LEGACY_DOWNLOAD_SCHEMA,
        "version": version,
        "release_id": version if channel == "stable" else f"{version}-g{generation}",
        "display_name": "test",
        "support_tier": "supported" if channel == "stable" else "development",
        "channel": channel,
        "generation": generation,
        "fingerprint": fingerprint,
        "system": SYSTEM,
        "iso": item["file"],
        "marker_url": artifact + "/complete.json",
        "url": (artifact + "/" + item["file"] if legacy else
                f"{DOWNLOAD_BASE_URL}/releases/{channel}/"
                f"{version if channel == 'stable' else f'{version}-g{generation}'}/"
                f"{item['file']}"),
        "size": item["size"],
        "sha256": item["sha256"],
        "published_at": "2026-07-22T22:09:10Z",
        "provenance": {
            "source": SHA, "system_ports": SHA, "packages": SHA,
            "ports": SHA, "os_definition": SHA, "freebsd": SHA,
        },
        "changes": [],
    }
    return value


def publisher_argv(output: Path, channel="stable", generation=2, fingerprint=FINGERPRINT):
    version = "1.0.0" if channel == "stable" else "1.1.0"
    return [
        "publish_download.py", "--channel", channel, "--version", version,
        "--fingerprint", fingerprint, "--system", SYSTEM,
        "--bundle-fingerprint", BUNDLE_FINGERPRINT,
        "--cloud-ufs-fingerprint", CLOUD_FINGERPRINT,
        "--cloud-zfs-fingerprint", ZFS_CLOUD_FINGERPRINT,
        "--generation", str(generation), "--source", SHA,
        "--system-ports", SHA, "--packages", SHA, "--ports", SHA,
        "--os-definition", SHA, "--freebsd", SHA, "--output", str(output),
        "--packages-fingerprint", PACKAGES_FINGERPRINT,
    ]


class PublishDownloadTests(unittest.TestCase):
    def test_v4_accepts_ordered_installer_and_board_appliances(self):
        value = release(channel="devel")
        value.update({
            "schema_version": publish.DOWNLOAD_SCHEMA,
            "architecture": "arm64", "package_arch": "aarch64",
        })
        installer = value["artifacts"][0]
        installer.update({
            "platform": "generic-arm64-uefi", "target_models": [],
            "format": "img", "compression": "xz", "partition_scheme": "gpt",
            "firmware": ["uefi"], "capabilities": {"cloud_init": False},
            "boot_inputs": {}, "artifact_fingerprint": installer["build_fingerprint"],
            "hardware_verification": "verified",
            "file": "FreeSense-1.1.0-g2-arm64-installer.img.xz",
        })
        installer["url"] = publish.public_artifact_url(
            value, "devel", installer["file"], DOWNLOAD_BASE_URL
        )
        appliances = []
        for profile, digit in (("arm64-rpi4b", "7"), ("arm64-rpi5-d0", "8")):
            file = f"FreeSense-1.1.0-g2-{profile}.img.xz"
            appliances.append({
                "kind": "appliance", "platform": profile,
                "target_models": [profile], "filesystem": "ufs", "format": "img",
                "compression": "xz", "partition_scheme": "mbr", "firmware": ["uefi"],
                "capabilities": {"appliance": True, "cloud_init": False},
                "boot_inputs": {"provider": "test"},
                "artifact_fingerprint": digit * 64, "build_fingerprint": digit * 64,
                "sha256": digit * 64, "size": 2048, "file": file,
                "url": publish.public_artifact_url(value, "devel", file, DOWNLOAD_BASE_URL),
                "marker_url": f"{BASE_URL}/artifacts/appliance/{digit * 64}/complete.json",
                "hardware_verification": "unverified",
            })
        value["artifacts"] = [installer, *appliances]
        publish.validate_download(value, "devel", BASE_URL, DOWNLOAD_BASE_URL)

    def test_arm64_installer_only_document_is_valid(self):
        value = release()
        value.update({
            "architecture": "arm64", "package_arch": "aarch64",
            "platform": "generic-arm64-uefi", "firmware": ["uefi"],
            "capabilities": {"bios": False, "uefi": True, "iso": False,
                             "installer_img": True, "cloud_init": False},
        })
        installer = value["artifacts"][0]
        installer.update({
            "format": "img", "compression": "xz",
            "file": "FreeSense-1.0.0-arm64-installer.img.xz",
        })
        installer["url"] = (
            f"{DOWNLOAD_BASE_URL}/releases/stable/1.0.0/{installer['file']}"
        )
        value["artifacts"] = [installer]
        publish.validate_download(value, "stable", BASE_URL, DOWNLOAD_BASE_URL)

    def test_publishes_arm64_installer_without_cloud_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "devel.arm64.json")
            argv = publisher_argv(output, channel="devel")
            for option in ("--cloud-ufs-fingerprint", "--cloud-zfs-fingerprint"):
                index = argv.index(option)
                del argv[index:index + 2]
            argv.extend(("--target", "arm64", "--image-profile", "generic-arm64-uefi"))
            arm_marker = marker(channel="devel")
            arm_marker.update({
                "schema_version": "freesense.installer/v1",
                "file": "FreeSense-1.1.0-g2-arm64-installer.img.xz",
            })
            responses = iter((arm_marker, None))
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(publish.main(), 0)
            value = json.loads(output.read_text())
        self.assertEqual(value["architecture"], "arm64")
        self.assertFalse(value["capabilities"]["cloud_init"])
        self.assertEqual(len(value["artifacts"]), 1)
        self.assertEqual(value["artifacts"][0]["format"], "img")

    def test_legacy_v1_document_remains_readable(self):
        publish.validate_download(
            release(legacy=True), "stable", BASE_URL, DOWNLOAD_BASE_URL,
            allow_legacy_url=True,
        )

    def test_v2_iso_marker_must_match_selected_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            responses = iter((marker(packages="f" * 64), cloud_marker(), cloud_marker(filesystem="zfs"), None))
            with mock.patch.object(
                sys,
                "argv",
                publisher_argv(Path(directory, "stable.json")),
            ), mock.patch.object(
                publish,
                "fetch_json",
                side_effect=lambda *_args, **_kwargs: next(responses),
            ):
                with self.assertRaisesRegex(SystemExit, "does not match"):
                    publish.main()

    def test_publishes_one_independent_stable_document(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "stable.json")
            responses = iter((marker(), cloud_marker(), cloud_marker(filesystem="zfs"), None))
            argv = publisher_argv(output)
            argv.extend(("--appliance-fingerprint", "", "--appliance-fingerprint", ""))
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(publish.main(), 0)
            value = json.loads(output.read_text())

        self.assertEqual(value["schema_version"], "freesense.download/v4")
        self.assertEqual(value["architecture"], "amd64")
        self.assertEqual(value["package_arch"], "amd64")
        self.assertEqual(value["platform"], "generic-amd64")
        self.assertEqual(value["channel"], "stable")
        self.assertEqual(value["version"], "1.0.0")
        self.assertEqual(len(value["artifacts"]), 5)
        self.assertEqual(
            {(item["filesystem"], item["format"]) for item in value["artifacts"]
             if item["kind"] == "cloud"},
            {("ufs", "qcow2"), ("ufs", "raw"), ("zfs", "qcow2"), ("zfs", "raw")},
        )
        self.assertEqual(
            value["artifacts"][0]["url"],
            "https://downloads.freesense.org/v1/releases/stable/1.0.0/"
            "FreeSense-1.0.0-amd64.iso",
        )
        self.assertNotIn("channels", value)
        self.assertEqual(
            value["release_notes"]["schema_version"],
            "freesense.release-notes/v2",
        )
        self.assertIsNone(value["release_notes"]["baseline_release_id"])
        self.assertFalse(value["release_notes"]["platform"]["packages"]["available"])

    def test_idempotent_publication_preserves_timestamp(self):
        existing = release()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "stable.json")
            responses = iter((marker(), cloud_marker(), cloud_marker(filesystem="zfs"), existing))
            with mock.patch.object(sys, "argv", publisher_argv(output)), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)):
                self.assertEqual(publish.main(), 0)
            value = json.loads(output.read_text())
        self.assertEqual(value["published_at"], existing["published_at"])
        self.assertEqual(value["changes"], existing["changes"])

    def test_same_generation_checks_each_cloud_format_with_shared_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "stable.json")
            first_responses = iter((marker(), cloud_marker(), cloud_marker(filesystem="zfs"), None))
            with mock.patch.object(sys, "argv", publisher_argv(output)), mock.patch.object(
                publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(first_responses)
            ):
                self.assertEqual(publish.main(), 0)
            existing = json.loads(output.read_text())
            changed_cloud = cloud_marker()
            changed_cloud["files"][0]["sha256"] = "9" * 64
            second_responses = iter((marker(), changed_cloud, cloud_marker(filesystem="zfs"), existing))
            with mock.patch.object(sys, "argv", publisher_argv(output)), mock.patch.object(
                publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(second_responses)
            ):
                with self.assertRaisesRegex(SystemExit, "only hardware-verification promotion"):
                    publish.main()

    def test_development_document_contains_repository_changes(self):
        existing = release("devel", generation=7, fingerprint="e" * 64)
        existing["provenance"]["source"] = "1" * 40
        compared = [{"type": "fix", "title": "Fix ZFS configuration recovery"}]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "devel.json")
            responses = iter((marker("devel", generation=8), cloud_marker("devel", 8), cloud_marker("devel", 8, "zfs"), existing))
            with mock.patch.object(
                sys, "argv", publisher_argv(output, "devel", 8)
            ), mock.patch.object(
                publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)
            ), mock.patch.object(
                publish, "github_compare", return_value=compared
            ) as compare:
                self.assertEqual(publish.main(), 0)
            value = json.loads(output.read_text())

        compare.assert_called_once_with("FreeSense-org/freesense", "1" * 40, SHA)
        self.assertEqual(value["changes"], [{
            "type": "fix",
            "title": "Fix ZFS configuration recovery",
            "scope": "System",
        }])
        self.assertEqual(value["release_notes"]["freesense"], value["changes"])
        self.assertFalse(value["release_notes"]["platform"]["freebsd"]["changed"])

    def test_build_control_changes_are_not_appliance_release_notes(self):
        existing = release("devel", generation=7, fingerprint="e" * 64)
        existing["provenance"]["os_definition"] = "1" * 40
        notes = publish.build_release_notes(
            existing,
            {
                "source": SHA, "system_ports": SHA, "packages": "2" * 40,
                "ports": SHA, "os_definition": SHA, "freebsd": SHA,
                "fingerprint": BUNDLE_FINGERPRINT,
            },
            SYSTEM,
            BASE_URL,
        )
        self.assertEqual(notes["freesense"], [])

    def test_system_package_catalogs_produce_version_deltas(self):
        existing = release("devel", generation=7, fingerprint="e" * 64)
        existing["system"] = "9" * 64
        before = {
            "openssl": {"version": "3.5.1", "origin": "security/openssl"},
            "old-tool": {"version": "1.0", "origin": "sysutils/old-tool"},
        }
        after = {
            "openssl": {"version": "3.5.2", "origin": "security/openssl"},
            "new-tool": {"version": "2.0", "origin": "sysutils/new-tool"},
        }
        with mock.patch.object(
            publish, "system_package_inventory", side_effect=(before, after)
        ) as inventory:
            changes = publish.package_changes(existing, SYSTEM, BASE_URL)
        self.assertEqual(inventory.call_count, 2)
        self.assertEqual(changes["counts"], {"updated": 1, "added": 1, "removed": 1})
        self.assertEqual(changes["updated"][0]["name"], "openssl")
        self.assertEqual(changes["added"][0]["name"], "new-tool")
        self.assertEqual(changes["removed"][0]["name"], "old-tool")
        self.assertFalse(changes["truncated"])

    def test_signed_system_catalog_is_parsed_as_package_inventory(self):
        catalog = "\n".join((
            json.dumps({"name": "openssl", "version": "3.5.2", "origin": "security/openssl"}),
            json.dumps({"name": "pkg", "version": "2.3.1", "origin": "ports-mgmt/pkg"}),
        ))
        completed = mock.Mock(returncode=0, stdout=catalog, stderr="")
        with mock.patch.object(publish, "fetch_bytes", return_value=b"catalog") as fetch, \
                mock.patch.object(publish.subprocess, "run", return_value=completed):
            inventory = publish.system_package_inventory(BASE_URL, SYSTEM)
        fetch.assert_called_once_with(
            f"{BASE_URL}/artifacts/system/{SYSTEM}/amd64/packagesite.pkg"
        )
        self.assertEqual(inventory["openssl"]["version"], "3.5.2")
        self.assertEqual(inventory["pkg"]["origin"], "ports-mgmt/pkg")

    def test_build_classified_product_commits_are_filtered(self):
        existing = release("devel", generation=7, fingerprint="e" * 64)
        existing["provenance"]["source"] = "1" * 40
        with mock.patch.object(
            publish,
            "github_compare",
            return_value=[{"type": "build", "title": "build: tune CI cache"}],
        ):
            changes = publish.build_changes(existing, {
                **existing["provenance"],
                "source": SHA,
                "fingerprint": BUNDLE_FINGERPRINT,
            })
        self.assertEqual(changes, [])

    def test_immutable_stable_version_cannot_be_rewritten(self):
        existing = release(fingerprint="e" * 64)
        with tempfile.TemporaryDirectory() as directory:
            responses = iter((marker(), cloud_marker(), cloud_marker(filesystem="zfs"), existing))
            with mock.patch.object(sys, "argv", publisher_argv(Path(directory, "stable.json"))), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)):
                with self.assertRaisesRegex(SystemExit, "cannot be rewritten"):
                    publish.main()

    def test_development_generation_cannot_move_backwards(self):
        existing = release("devel", generation=8)
        with tempfile.TemporaryDirectory() as directory:
            responses = iter((marker("devel", generation=7), cloud_marker("devel", 7), cloud_marker("devel", 7, "zfs"), existing))
            with mock.patch.object(
                sys, "argv", publisher_argv(Path(directory, "devel.json"), "devel", 7)
            ), mock.patch.object(
                publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)
            ):
                with self.assertRaisesRegex(SystemExit, "cannot move backwards"):
                    publish.main()

    def test_migrates_only_published_legacy_channels(self):
        legacy_release = release(legacy=True)
        legacy_release.pop("schema_version")
        legacy = {
            "schema_version": "freesense.downloads/v1",
            "generated": legacy_release["published_at"],
            "channels": {"stable": legacy_release, "devel": None},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "split")
            argv = ["migrate_downloads.py", "--output-dir", str(output)]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(migrate, "fetch_json", side_effect=(legacy, None, None)):
                self.assertEqual(migrate.main(), 0)
            stable = json.loads(Path(output, "stable.json").read_text())
            self.assertFalse(Path(output, "devel.json").exists())
        self.assertEqual(stable["schema_version"], "freesense.download/v1")
        self.assertEqual(stable["channel"], "stable")
        self.assertTrue(stable["url"].startswith(DOWNLOAD_BASE_URL))

    def test_migration_is_idempotent_after_download_url_relocation(self):
        split = release()
        legacy_release = release(legacy=True)
        legacy_release.pop("schema_version")
        legacy = {
            "schema_version": "freesense.downloads/v1",
            "channels": {"stable": legacy_release, "devel": None},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "split")
            argv = ["migrate_downloads.py", "--output-dir", str(output)]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                migrate, "fetch_json", side_effect=(legacy, split, None)
            ):
                self.assertEqual(migrate.main(), 0)
            self.assertFalse(Path(output, "stable.json").exists())


if __name__ == "__main__":
    unittest.main()
