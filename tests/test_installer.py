"""Verify the local installer's input checks using synthetic files only."""

import hashlib
import importlib.util
import lzma
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "scripts/installer"


sys.path.insert(0, str(INSTALLER))


class InstallerChecks(unittest.TestCase):
    def test_compressed_and_raw_identity(self):
        spec = importlib.util.spec_from_file_location("build_iso", INSTALLER / "build_iso.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        raw = b"fixture" * 512
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "fixture.xz"
            compressed = lzma.compress(raw)
            image.write_bytes(compressed)
            manifest = {"raw_bytes": len(raw), "xz_bytes": len(compressed), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "xz_sha256": hashlib.sha256(compressed).hexdigest()}
            builder.validate_payload(image, manifest)
            for key in manifest:
                with self.subTest(field=key), self.assertRaises(ValueError):
                    builder.validate_payload(image, {**manifest, key: 1 if key.endswith("bytes") else "0" * 64})

    def test_whole_disk_blank_guard(self):
        # Only read ordinary fixture files. Never call the disk writer from this test.
        script = r"""
set -eu
. ./freebsd.sh
if ! command -v sha256 >/dev/null; then
    sha256() { sha256sum | cut -d ' ' -f 1; }
fi
fs_is_blank zeros 8192
if fs_is_blank late-data 8192 || fs_is_blank zeros 16384; then exit 1; fi
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            shutil.copyfile(INSTALLER / "guided/freebsd.sh", path / "freebsd.sh")
            (path / "zeros").write_bytes(bytes(8192))
            (path / "late-data").write_bytes(bytes(8191) + b"x")
            if shell := shutil.which("sh"):
                command = [shell, "-c", script]
            else:
                command = ["docker", "run", "--rm", "--pull=never", "--network", "none", "-v", f"{path}:/fixture:ro",
                           "-w", "/fixture", "php:8.6.0-cli", "sh", "-c", script]
            subprocess.run(command, cwd=path, check=True)
