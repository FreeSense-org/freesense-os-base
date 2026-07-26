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


def cloud_marker(channel="stable", generation=2,
                 fingerprint=CLOUD_FINGERPRINT, bundle=BUNDLE_FINGERPRINT):
    version = "1.0.0" if channel == "stable" else "1.1.0"
    prefix = f"FreeSense-{version}" if channel == "stable" else f"FreeSense-{version}-g{generation}"
    return {
        "schema_version": "freesense.cloud-image/v1",
        "fingerprint": fingerprint,
        "bundle_fingerprint": bundle,
        "generation": generation,
        "channel": channel,
        "inputs": {"system": SYSTEM, "packages": PACKAGES_FINGERPRINT},
        "files": [
            {"kind": "cloud", "format": "qcow2", "file": f"{prefix}-amd64-ufs.qcow2.xz",
             "sha256": "2" * 64, "size": 2048, "virtual_size": 16 * 1024**3},
            {"kind": "cloud", "format": "raw", "file": f"{prefix}-amd64-ufs.raw.xz",
             "sha256": "3" * 64, "size": 3072, "virtual_size": 16 * 1024**3},
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
            "schema_version": publish.DOWNLOAD_SCHEMA, "version": version,
            "release_id": release_id, "display_name": "test",
            "support_tier": "supported" if channel == "stable" else "development",
            "channel": channel, "generation": generation,
            "bundle_fingerprint": fingerprint, "system": SYSTEM,
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
        "--cloud-fingerprint", CLOUD_FINGERPRINT,
        "--generation", str(generation), "--source", SHA,
        "--system-ports", SHA, "--packages", SHA, "--ports", SHA,
        "--os-definition", SHA, "--freebsd", SHA, "--output", str(output),
        "--packages-fingerprint", PACKAGES_FINGERPRINT,
    ]


class PublishDownloadTests(unittest.TestCase):
    def test_legacy_v1_document_remains_readable(self):
        publish.validate_download(
            release(legacy=True), "stable", BASE_URL, DOWNLOAD_BASE_URL,
            allow_legacy_url=True,
        )

    def test_v2_iso_marker_must_match_selected_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            responses = iter((marker(packages="f" * 64), cloud_marker(), None))
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
            responses = iter((marker(), cloud_marker(), None))
            with mock.patch.object(sys, "argv", publisher_argv(output)), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(publish.main(), 0)
            value = json.loads(output.read_text())

        self.assertEqual(value["schema_version"], "freesense.download/v2")
        self.assertEqual(value["channel"], "stable")
        self.assertEqual(value["version"], "1.0.0")
        self.assertEqual(
            value["artifacts"][0]["url"],
            "https://downloads.freesense.org/v1/releases/stable/1.0.0/"
            "FreeSense-1.0.0-amd64.iso",
        )
        self.assertNotIn("channels", value)

    def test_idempotent_publication_preserves_timestamp(self):
        existing = release()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "stable.json")
            responses = iter((marker(), cloud_marker(), existing))
            with mock.patch.object(sys, "argv", publisher_argv(output)), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)):
                self.assertEqual(publish.main(), 0)
            value = json.loads(output.read_text())
        self.assertEqual(value["published_at"], existing["published_at"])
        self.assertEqual(value["changes"], existing["changes"])

    def test_development_document_contains_repository_changes(self):
        existing = release("devel", generation=7, fingerprint="e" * 64)
        existing["provenance"]["source"] = "1" * 40
        compared = [{"type": "fix", "title": "Fix ZFS configuration recovery"}]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "devel.json")
            responses = iter((marker("devel", generation=8), cloud_marker("devel", 8), existing))
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

    def test_immutable_stable_version_cannot_be_rewritten(self):
        existing = release(fingerprint="e" * 64)
        with tempfile.TemporaryDirectory() as directory:
            responses = iter((marker(), cloud_marker(), existing))
            with mock.patch.object(sys, "argv", publisher_argv(Path(directory, "stable.json"))), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)):
                with self.assertRaisesRegex(SystemExit, "cannot be rewritten"):
                    publish.main()

    def test_development_generation_cannot_move_backwards(self):
        existing = release("devel", generation=8)
        with tempfile.TemporaryDirectory() as directory:
            responses = iter((marker("devel", generation=7), cloud_marker("devel", 7), existing))
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
