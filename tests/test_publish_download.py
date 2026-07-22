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
BASE_URL = "https://pkg.freesense.org/v1"
DOWNLOAD_BASE_URL = "https://downloads.freesense.org/v1"


def marker(channel="stable", generation=2, fingerprint=FINGERPRINT):
    version = "1.0.0" if channel == "stable" else "1.1.0"
    file = (f"FreeSense-{version}-amd64.iso" if channel == "stable"
            else f"FreeSense-{version}-g{generation}-amd64.iso")
    return {
        "schema_version": "freesense.iso/v1",
        "fingerprint": fingerprint,
        "system": SYSTEM,
        "generation": generation,
        "file": file,
        "sha256": ISO_SHA,
        "size": 1024,
        "inputs": {"channel": channel},
    }


def release(channel="stable", generation=2, fingerprint=FINGERPRINT, legacy=False):
    version = "1.0.0" if channel == "stable" else "1.1.0"
    item = marker(channel, generation, fingerprint)
    artifact = f"{BASE_URL}/artifacts/iso/{fingerprint}"
    return {
        "schema_version": publish.DOWNLOAD_SCHEMA,
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
            "source": SHA, "ports": SHA, "os_definition": SHA, "freebsd": SHA,
        },
    }


def publisher_argv(output: Path, channel="stable", generation=2, fingerprint=FINGERPRINT):
    version = "1.0.0" if channel == "stable" else "1.1.0"
    return [
        "publish_download.py", "--channel", channel, "--version", version,
        "--fingerprint", fingerprint, "--system", SYSTEM,
        "--generation", str(generation), "--source", SHA, "--ports", SHA,
        "--os-definition", SHA, "--freebsd", SHA, "--output", str(output),
    ]


class PublishDownloadTests(unittest.TestCase):
    def test_publishes_one_independent_stable_document(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "stable.json")
            responses = iter((marker(), None))
            with mock.patch.object(sys, "argv", publisher_argv(output)), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(publish.main(), 0)
            value = json.loads(output.read_text())

        self.assertEqual(value["schema_version"], "freesense.download/v1")
        self.assertEqual(value["channel"], "stable")
        self.assertEqual(value["version"], "1.0.0")
        self.assertEqual(
            value["url"],
            "https://downloads.freesense.org/v1/releases/stable/1.0.0/"
            "FreeSense-1.0.0-amd64.iso",
        )
        self.assertNotIn("channels", value)

    def test_idempotent_publication_preserves_timestamp(self):
        existing = release()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "stable.json")
            responses = iter((marker(), existing))
            with mock.patch.object(sys, "argv", publisher_argv(output)), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)):
                self.assertEqual(publish.main(), 0)
            value = json.loads(output.read_text())
        self.assertEqual(value["published_at"], existing["published_at"])

    def test_immutable_stable_version_cannot_be_rewritten(self):
        existing = release(fingerprint="e" * 64)
        with tempfile.TemporaryDirectory() as directory:
            responses = iter((marker(), existing))
            with mock.patch.object(sys, "argv", publisher_argv(Path(directory, "stable.json"))), \
                    mock.patch.object(publish, "fetch_json", side_effect=lambda *_args, **_kwargs: next(responses)):
                with self.assertRaisesRegex(SystemExit, "cannot be rewritten"):
                    publish.main()

    def test_development_generation_cannot_move_backwards(self):
        existing = release("devel", generation=8)
        with tempfile.TemporaryDirectory() as directory:
            responses = iter((marker("devel", generation=7), existing))
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
