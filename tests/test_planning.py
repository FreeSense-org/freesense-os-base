from __future__ import annotations

import base64
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = load_script("freesense_plan", "scripts/plan.py")
channel = load_script("freesense_channel", "scripts/channel.py")
FINGERPRINT = "a" * 64


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def signed_envelope(component: str = "system", *, system_fingerprint: str | None = None):
    artifact_path = f"{component}/{FINGERPRINT}"
    if component == "packages":
        artifact_path = f"packages/1.1/{FINGERPRINT}"
    selected = {
        "fingerprint": FINGERPRINT,
        "url": f"https://pkg.freesense.org/v1/artifacts/{artifact_path}/amd64",
        "generation": 7,
        "published_at": "2026-07-21T00:00:00Z",
        "verified": False,
    }
    if system_fingerprint is not None:
        selected["system_fingerprint"] = system_fingerprint
    components = {component: selected}
    if component == "packages" and system_fingerprint is not None:
        components["system"] = {
            "fingerprint": system_fingerprint,
            "url": f"https://pkg.freesense.org/v1/artifacts/system/{system_fingerprint}/amd64",
            "generation": 6,
            "published_at": "2026-07-21T00:00:00Z",
            "verified": True,
        }
    payload = {
        "schema_version": "freesense.channels/v1",
        "channels": {
            "devel": {
                "name": "devel",
                "description": "Development version",
                "package_train": "1.1",
                "abi": "FreeBSD:16:amd64",
                "altabi": "freebsd:16:x86:64",
                "default": True,
                **components,
            }
        },
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    envelope = {
        "schema_version": "freesense.repositories/v3",
        "payload": base64.b64encode(payload_bytes).decode(),
        "signature": base64.b64encode(b"test-signature").decode(),
    }
    return json.dumps(envelope).encode(), payload_bytes


def system_closure(*, channel_name: str = "devel"):
    payload = b'{"schema_version":"freesense.channels/v1"}'
    return {
        "fingerprint": "a" * 64,
        "channel": channel_name,
        "generation": 7,
        "package_train": "1.1",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_base64": base64.b64encode(payload).decode(),
        "signature_base64": base64.b64encode(b"test-signature").decode(),
        "artifact_platform": "c" * 64,
        "artifact_source_sha": "1" * 40,
        "artifact_system_sha": "2" * 40,
        "artifact_os_base_sha": "3" * 40,
        "artifact_freebsd_sha": "4" * 40,
        "artifact_ports_sha": "5" * 40,
        "artifact_image_sha256": "6" * 64,
        "artifact_worker_tools_sha256": "9" * 64,
        "artifact_jail_object": "inputs/sha256/" + "7" * 64,
        "artifact_signing_public_key_sha256": hashlib.sha256(
            (ROOT / "config/channel-signing-public.pem").read_bytes()
        ).hexdigest(),
        "packages_fingerprint": "",
    }


class PlannerChannelTests(unittest.TestCase):
    def test_system_plan_uses_the_pinned_worker_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "patches").mkdir()
            (root / "config/freebsd-16.json").write_text(json.dumps({
                "schema_version": "freesense.freebsd-pin/v2",
                "ready": True,
                "freebsd_source": {"commit": "1" * 40},
                "freebsd_ports": {"commit": "2" * 40},
                "jail_seed": {
                    "object": "inputs/sha256/" + "3" * 64,
                    "sha256": "3" * 64,
                },
                "worker_image": {"sha256": "4" * 64},
                "worker_tools": {
                    "object": "inputs/sha256/" + "5" * 64,
                    "sha256": "5" * 64,
                },
            }))
            (root / "config/build-policy.json").write_text(json.dumps({
                "package_train": "1.1",
                "abi": "FreeBSD:16:amd64",
                "altabi": "freebsd:16:x86:64",
                "public_base_url": "https://pkg.freesense.org/v1",
                "runner": {"vcpus": 12, "memory_mib": 32768, "disk_gib": 160},
            }))
            (root / "config/channel-signing-public.pem").write_bytes(b"test-key")
            argv = ["plan.py", "system", "--os-base-sha", "6" * 40]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(plan, "ROOT", root), \
                    mock.patch.object(plan, "recipe_digest", return_value="7" * 64), \
                    mock.patch.object(plan, "remote_sha", side_effect=("8" * 40, "9" * 40)), \
                    mock.patch.object(plan, "current_component", return_value=""), \
                    redirect_stdout(io.StringIO()) as rendered:
                self.assertEqual(plan.main(), 0)
        values = json.loads(rendered.getvalue())
        self.assertEqual(values["worker_tools_sha256"], "5" * 64)
        self.assertEqual(values["image_sha256"], "4" * 64)
        self.assertTrue(values["needed"])

    def test_current_component_uses_named_client_and_verifies(self):
        envelope, _ = signed_envelope()
        requests = []

        def open_request(request, timeout):
            requests.append((request, timeout))
            return Response(envelope)

        with mock.patch.object(plan.urllib.request, "urlopen", side_effect=open_request), \
                mock.patch.object(plan.subprocess, "run") as verify:
            self.assertEqual(
                plan.current_component("https://pkg.freesense.org/v1/repos.manifest.json", "system"),
                FINGERPRINT,
            )
        self.assertEqual(requests[0][0].get_header("User-agent"), plan.USER_AGENT)
        self.assertEqual(requests[0][1], 15)
        verify.assert_called_once()

    def test_current_component_allows_only_not_found_as_empty(self):
        missing = urllib.error.HTTPError("https://example.invalid", 404, "missing", {}, None)
        forbidden = urllib.error.HTTPError("https://example.invalid", 403, "forbidden", {}, None)
        with mock.patch.object(plan.urllib.request, "urlopen", side_effect=missing):
            self.assertEqual(plan.current_component("https://example.invalid", "system"), "")
        with mock.patch.object(plan.urllib.request, "urlopen", side_effect=forbidden):
            with self.assertRaisesRegex(SystemExit, "HTTP 403"):
                plan.current_component("https://example.invalid", "system")

    def test_channel_reader_exports_payload_identity_and_system_binding(self):
        envelope, payload = signed_envelope("packages", system_fingerprint="b" * 64)
        requests = []

        def open_request(request, timeout):
            requests.append((request, timeout))
            return Response(envelope)

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory, "channel.pem")
            output = Path(directory, "output")
            key.write_text("test")
            argv = [
                "channel.py", "--url", "https://pkg.freesense.org/v1/repos.manifest.json",
                "--public-key", str(key), "--channel", "devel", "--component", "packages",
                "--github-output", str(output),
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(channel.urllib.request, "urlopen", side_effect=open_request), \
                    mock.patch.object(subprocess, "run"), redirect_stdout(io.StringIO()):
                self.assertEqual(channel.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(requests[0][0].get_header("User-agent"), channel.USER_AGENT)
        self.assertEqual(values["payload_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(base64.b64decode(values["payload_base64"]), payload)
        self.assertEqual(base64.b64decode(values["signature_base64"]), b"test-signature")
        self.assertEqual(values["system_fingerprint"], "b" * 64)

    def test_system_channel_reader_validates_and_exports_exact_closure(self):
        envelope, _ = signed_envelope("system")
        marker = {
            "schema_version": "freesense.artifact/v1",
            "stage": "system",
            "fingerprint": FINGERPRINT,
            "generation": 7,
            "inputs": {
                "platform": "b" * 64,
                "system": FINGERPRINT,
                "source": "1" * 40,
                "system_ports": "2" * 40,
                "freebsd": "3" * 40,
                "ports": "4" * 40,
                "package_train": "1.1",
                "os_definition": "5" * 40,
                "worker_image": "c" * 64,
                "worker_tools": "f" * 64,
                "jail_object": "inputs/sha256/" + "d" * 64,
                "signing_public_key": "e" * 64,
            },
        }
        requests = []
        responses = iter((envelope, json.dumps(marker).encode()))

        def open_request(request, timeout):
            requests.append((request, timeout))
            return Response(next(responses))

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory, "channel.pem")
            output = Path(directory, "output")
            key.write_text("test")
            argv = [
                "channel.py", "--public-key", str(key), "--channel", "devel",
                "--component", "system", "--github-output", str(output),
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(channel.urllib.request, "urlopen", side_effect=open_request), \
                    mock.patch.object(subprocess, "run"), redirect_stdout(io.StringIO()):
                self.assertEqual(channel.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.get_header("User-agent") == channel.USER_AGENT for request, _ in requests))
        self.assertEqual(values["artifact_platform"], "b" * 64)
        self.assertEqual(values["artifact_source_sha"], "1" * 40)
        self.assertEqual(values["artifact_jail_object"], "inputs/sha256/" + "d" * 64)
        self.assertEqual(values["artifact_worker_tools_sha256"], "f" * 64)
        self.assertEqual(values["packages_fingerprint"], "")

    def test_iso_plan_uses_selected_system_closure_without_remote_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            closure = Path(directory, "system.json")
            closure.write_text(json.dumps(system_closure()))
            argv = [
                "plan.py", "iso", "--system-closure", str(closure),
                "--github-output", str(output),
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(plan, "remote_sha", side_effect=AssertionError("unexpected remote resolution")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(plan.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(values["platform"], "c" * 64)
        self.assertEqual(values["system"], "a" * 64)
        self.assertEqual(values["source_sha"], "1" * 40)
        self.assertEqual(values["os_base_sha"], "3" * 40)
        self.assertEqual(values["image_sha256"], "6" * 64)
        self.assertEqual(values["worker_tools_sha256"], "9" * 64)

    def test_packages_inherit_published_system_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "output")
            closure = system_closure()
            closure["artifact_platform"] = "b" * 64
            closure_path = Path(directory, "system.json")
            closure_path.write_text(json.dumps(closure))
            argv = [
                "plan.py", "packages", "--system-closure", str(closure_path),
                "--github-output", str(output),
            ]

            resolved = []

            def resolve(repository):
                resolved.append(repository)
                return "8" * 40

            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(plan, "remote_sha", side_effect=resolve), \
                    mock.patch.object(plan, "current_component", side_effect=AssertionError("unexpected channel refetch")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(plan.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(resolved, ["FreeSense-org/freesense-packages"])
        self.assertEqual(values["system"], "a" * 64)
        self.assertEqual(values["platform"], "b" * 64)
        self.assertEqual(values["os_base_sha"], "3" * 40)
        self.assertEqual(values["packages_sha"], "8" * 40)


if __name__ == "__main__":
    unittest.main()
