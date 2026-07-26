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
with mock.patch.dict(sys.modules, {"plan": plan}):
    stable_plan = load_script("freesense_stable_plan", "scripts/stable_plan.py")
FINGERPRINT = "a" * 64
PACKAGES_FINGERPRINT = "b" * 64


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def signed_envelope(
    component: str = "system",
    *,
    system_fingerprint: str | None = None,
    built_against_system: str | None = None,
    include_packages: bool = False,
    verified: bool = False,
    schema: str = "freesense.channels/v1",
):
    artifact_path = f"{component}/{FINGERPRINT}"
    if component == "packages":
        artifact_path = f"packages/1.1/{FINGERPRINT}"
    selected = {
        "fingerprint": FINGERPRINT,
        "url": f"https://pkg.freesense.org/v1/artifacts/{artifact_path}/amd64",
        "generation": 7,
        "published_at": "2026-07-21T00:00:00Z",
        "verified": verified,
    }
    if system_fingerprint is not None:
        selected["system_fingerprint"] = system_fingerprint
    if built_against_system is not None:
        selected["built_against_system"] = built_against_system
    components = {component: selected}
    if component == "system" and include_packages:
        components["packages"] = {
            "fingerprint": PACKAGES_FINGERPRINT,
            "system_fingerprint": FINGERPRINT,
            "url": (
                "https://pkg.freesense.org/v1/artifacts/packages/1.1/"
                f"{PACKAGES_FINGERPRINT}/amd64"
            ),
            "generation": 8,
            "published_at": "2026-07-21T00:05:00Z",
            "verified": verified,
        }
    if component == "packages" and system_fingerprint is not None:
        components["system"] = {
            "fingerprint": system_fingerprint,
            "url": f"https://pkg.freesense.org/v1/artifacts/system/{system_fingerprint}/amd64",
            "generation": 6,
            "published_at": "2026-07-21T00:00:00Z",
            "verified": True,
        }
    if schema == "freesense.channels/v3" and "system" in components:
        components["system"]["osversion"] = 1600019
    payload = {
        "schema_version": schema,
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


def completion_marker(component: str = "system", *, system_fingerprint: str = FINGERPRINT):
    inputs = {
        "platform": "b" * 64,
        "system": system_fingerprint,
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
    }
    if component == "packages":
        inputs["packages"] = "6" * 40
        inputs["built_against_system"] = system_fingerprint
    return {
        "schema_version": "freesense.artifact/v1",
        "stage": component,
        "fingerprint": FINGERPRINT,
        "generation": 7,
        "inputs": inputs,
    }


def system_closure(*, channel_name: str = "devel"):
    payload = b'{"schema_version":"freesense.channels/v1"}'
    return {
        "fingerprint": "a" * 64,
        "channel": channel_name,
        "generation": 7,
        "package_train": "1.1",
        "release_version": "1.1.0",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_base64": base64.b64encode(payload).decode(),
        "signature_base64": base64.b64encode(b"test-signature").decode(),
        "artifact_platform": "c" * 64,
        "artifact_source_sha": "1" * 40,
        "artifact_system_sha": "2" * 40,
        "artifact_packages_sha": "6" * 40,
        "artifact_os_base_sha": "3" * 40,
        "artifact_freebsd_sha": "4" * 40,
        "artifact_ports_sha": "5" * 40,
        "artifact_image_sha256": "6" * 64,
        "artifact_worker_tools_sha256": "9" * 64,
        "artifact_jail_object": "inputs/sha256/" + "7" * 64,
        "artifact_signing_public_key_sha256": hashlib.sha256(
            (ROOT / "config/channel-signing-public.pem").read_bytes()
        ).hexdigest(),
        "artifact_freebsd_pin_id": "8" * 64,
        "packages_fingerprint": PACKAGES_FINGERPRINT,
        "packages_generation": 8,
        "packages_verified": "true",
        "verified": "true",
        "osversion": 1600019,
    }


def stable_values(*, package_build_config: str, system_ports_sha: str = "2" * 40):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "config/releases").mkdir(parents=True)
        (root / "config/build-policy.json").write_text(json.dumps({
            "abi": "FreeBSD:16:amd64",
            "altabi": "freebsd:16:x86:64",
            "public_base_url": "https://pkg.freesense.org/v1",
            "runner": {"vcpus": 12, "memory_mib": 32768, "disk_gib": 160},
            "release": {"stable_train": "1.0"},
        }))
        (root / "config/channel-signing-public.pem").write_bytes(b"test-key")
        (root / "config/releases/1.0.1.json").write_text(json.dumps({
            "schema_version": "freesense.release-lock/v1",
            "release": "1.0.1",
            "product_version": "1.0.1-RELEASE",
            "package_train": "1.0",
            "sealed": True,
            "source_sha": "1" * 40,
            "system_ports_sha": system_ports_sha,
            "packages_sha": "3" * 40,
            "freebsd_source_sha": "4" * 40,
            "freebsd_ports_sha": "5" * 40,
            "jail_object": "inputs/sha256/" + "6" * 64,
            "worker_image_sha256": "7" * 64,
            "worker_tools_sha256": "8" * 64,
            "freebsd_osversion": 1600019,
        }))
        argv = [
            "stable_plan.py",
            "--os-base-sha", "9" * 40,
            "--release", "1.0.1",
        ]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(stable_plan, "ROOT", root), \
                mock.patch.object(stable_plan, "recipe_digest", return_value="a" * 64), \
                mock.patch.object(
                    stable_plan,
                    "remote_recipe_digest",
                    return_value=package_build_config,
                ), redirect_stdout(io.StringIO()) as rendered:
            stable_plan.main()
    return json.loads(rendered.getvalue())


class PlannerChannelTests(unittest.TestCase):
    def test_release_policy_accepts_future_train_and_rejects_mismatch(self):
        policy = {
            "release": {
                "stable_train": "1.1",
                "development_train": "1.2",
                "stable_lifecycle": "supported",
                "development_lifecycle": "experimental",
            }
        }
        self.assertEqual(
            plan.release_policy(policy, "devel", "1.2.0"),
            ("1.2", "experimental"),
        )
        with self.assertRaisesRegex(SystemExit, "does not match configured train"):
            plan.release_policy(policy, "devel", "1.1.9")
        policy["release"]["development_lifecycle"] = "supported"
        with self.assertRaisesRegex(SystemExit, "lifecycle must be experimental"):
            plan.release_policy(policy, "devel", "1.2.0")

    def test_cloud_and_iso_share_one_bundle_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            closure = Path(directory, "system.json")
            closure.write_text(json.dumps(system_closure()))
            argv = ["plan.py", "cloud", "--system-closure", str(closure)]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(
                        plan, "remote_sha",
                        side_effect=AssertionError("unexpected remote resolution"),
                    ), redirect_stdout(io.StringIO()) as rendered:
                self.assertEqual(plan.main(), 0)
            values = json.loads(rendered.getvalue())
        self.assertRegex(values["bundle"], r"^[0-9a-f]{64}$")
        self.assertRegex(values["cloud"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(values["cloud"], values["iso"])

    def test_system_plan_uses_the_pinned_worker_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "patches").mkdir()
            (root / "config/freebsd-16.json").write_text(json.dumps({
                "schema_version": "freesense.freebsd-pin/v2",
                "ready": True,
                "valid_from": "2026-07-22T06:00:00Z",
                "valid_until": "2026-08-05T06:00:00Z",
                "freebsd_source": {"commit": "1" * 40, "osversion": 1600019},
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
                "release": {
                    "stable_train": "1.0", "development_train": "1.1",
                    "development_version": "1.1.0",
                    "stable_lifecycle": "supported",
                    "development_lifecycle": "experimental",
                },
                "cloud": {
                    "architecture": "amd64", "filesystem": "ufs",
                    "virtual_size_gib": 16, "formats": ["qcow2", "raw"],
                },
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
        self.assertEqual(values["osversion"], 1600019)
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

    def test_current_component_accepts_v2_channel_manifest(self):
        envelope, _ = signed_envelope(schema="freesense.channels/v2")
        with mock.patch.object(
            plan.urllib.request, "urlopen", return_value=Response(envelope)
        ), mock.patch.object(plan.subprocess, "run"):
            self.assertEqual(
                plan.current_component(
                    "https://pkg.freesense.org/v1/repos.manifest.json", "system"
                ),
                FINGERPRINT,
            )

    def test_remote_recipe_digest_is_commit_pinned_and_uses_named_client(self):
        requests = []

        def open_request(request, timeout):
            requests.append((request, timeout))
            return Response(b"package-build-options\n")

        with mock.patch.object(
            plan.urllib.request, "urlopen", side_effect=open_request
        ):
            first = plan.remote_recipe_digest(
                "FreeSense-org/freesense",
                "1" * 40,
                ("tools/conf/pfPorts/make.conf",),
            )
        with mock.patch.object(
            plan.urllib.request,
            "urlopen",
            return_value=Response(b"different-package-build-options\n"),
        ):
            second = plan.remote_recipe_digest(
                "FreeSense-org/freesense",
                "1" * 40,
                ("tools/conf/pfPorts/make.conf",),
            )

        self.assertNotEqual(first, second)
        self.assertEqual(
            requests[0][0].full_url,
            "https://raw.githubusercontent.com/FreeSense-org/freesense/"
            + "1" * 40
            + "/tools/conf/pfPorts/make.conf",
        )
        self.assertEqual(requests[0][0].get_header("User-agent"), plan.USER_AGENT)
        self.assertEqual(requests[0][1], 15)

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
        marker = completion_marker("packages", system_fingerprint="b" * 64)
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
                "channel.py", "--url", "https://pkg.freesense.org/v1/repos.manifest.json",
                "--public-key", str(key), "--channel", "devel", "--component", "packages",
                "--github-output", str(output),
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(channel.urllib.request, "urlopen", side_effect=open_request), \
                    mock.patch.object(subprocess, "run"), redirect_stdout(io.StringIO()):
                self.assertEqual(channel.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.get_header("User-agent") == channel.USER_AGENT for request, _ in requests))
        self.assertEqual(values["payload_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(base64.b64decode(values["payload_base64"]), payload)
        self.assertEqual(base64.b64decode(values["signature_base64"]), b"test-signature")
        self.assertEqual(values["system_fingerprint"], "b" * 64)

    def test_packages_channel_reader_rejects_marker_for_another_system(self):
        envelope, _ = signed_envelope("packages", system_fingerprint="b" * 64)
        marker = completion_marker("packages", system_fingerprint="c" * 64)
        responses = iter((envelope, json.dumps(marker).encode()))

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory, "channel.pem")
            key.write_text("test")
            argv = [
                "channel.py", "--public-key", str(key), "--channel", "devel",
                "--component", "packages",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(
                        channel.urllib.request,
                        "urlopen",
                        side_effect=lambda *_args, **_kwargs: Response(next(responses)),
                    ), mock.patch.object(subprocess, "run"), redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, "conflicts with its channel entry"):
                    channel.main()

    def test_packages_channel_reader_accepts_same_pin_system_rebind(self):
        current_system = "b" * 64
        build_system = "c" * 64
        envelope, _ = signed_envelope(
            "packages",
            system_fingerprint=current_system,
            built_against_system=build_system,
        )
        marker = completion_marker("packages", system_fingerprint=build_system)
        responses = iter((envelope, json.dumps(marker).encode()))

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory, "channel.pem")
            output = Path(directory, "output.json")
            key.write_text("test")
            argv = [
                "channel.py", "--public-key", str(key), "--channel", "devel",
                "--component", "packages", "--json-output", str(output),
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(
                        channel.urllib.request,
                        "urlopen",
                        side_effect=lambda *_args, **_kwargs: Response(next(responses)),
                    ), mock.patch.object(subprocess, "run"), redirect_stdout(io.StringIO()):
                self.assertEqual(channel.main(), 0)
            values = json.loads(output.read_text())

        self.assertEqual(values["system_fingerprint"], current_system)
        self.assertEqual(values["built_against_system"], build_system)

    def test_system_channel_reader_validates_and_exports_exact_closure(self):
        envelope, _ = signed_envelope("system")
        marker = completion_marker()
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

    def test_system_channel_reader_exports_verified_packages_binding(self):
        envelope, _ = signed_envelope("system", include_packages=True, verified=True)
        marker = completion_marker()
        packages_marker = completion_marker("packages")
        packages_marker["fingerprint"] = PACKAGES_FINGERPRINT
        packages_marker["generation"] = 8
        responses = iter((
            envelope,
            json.dumps(marker).encode(),
            json.dumps(packages_marker).encode(),
        ))

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory, "channel.pem")
            output = Path(directory, "output.json")
            key.write_text("test")
            argv = [
                "channel.py", "--public-key", str(key), "--channel", "devel",
                "--component", "system", "--json-output", str(output),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                channel.urllib.request,
                "urlopen",
                side_effect=lambda *_args, **_kwargs: Response(next(responses)),
            ), mock.patch.object(subprocess, "run"), redirect_stdout(io.StringIO()):
                self.assertEqual(channel.main(), 0)
            values = json.loads(output.read_text())

        self.assertEqual(values["verified"], "true")
        self.assertEqual(values["packages_fingerprint"], PACKAGES_FINGERPRINT)
        self.assertEqual(values["packages_generation"], 8)
        self.assertEqual(values["packages_verified"], "true")
        self.assertEqual(values["artifact_packages_sha"], "6" * 40)

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
        self.assertEqual(values["packages_sha"], "6" * 40)
        self.assertEqual(values["packages_fingerprint"], PACKAGES_FINGERPRINT)
        self.assertEqual(values["os_base_sha"], "3" * 40)
        self.assertEqual(values["image_sha256"], "6" * 64)
        self.assertEqual(values["worker_tools_sha256"], "9" * 64)
        self.assertEqual(values["release_version"], "1.1.0")

    def test_iso_identity_changes_with_the_optional_package_pair(self):
        fingerprints = []
        for package_fingerprint in ("b" * 64, "c" * 64):
            with tempfile.TemporaryDirectory() as directory:
                closure = system_closure()
                closure["packages_fingerprint"] = package_fingerprint
                closure_path = Path(directory, "system.json")
                closure_path.write_text(json.dumps(closure))
                argv = ["plan.py", "iso", "--system-closure", str(closure_path)]
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(
                            plan, "remote_sha",
                            side_effect=AssertionError("unexpected remote resolution"),
                        ), redirect_stdout(io.StringIO()) as rendered:
                    self.assertEqual(plan.main(), 0)
                fingerprints.append(json.loads(rendered.getvalue())["iso"])
        self.assertNotEqual(fingerprints[0], fingerprints[1])

    def test_iso_plan_rejects_pending_channel_pair(self):
        for field, value in (
            ("verified", "false"),
            ("packages_fingerprint", ""),
            ("packages_verified", "false"),
            ("packages_generation", 0),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                closure = system_closure()
                closure[field] = value
                closure_path = Path(directory, "system.json")
                closure_path.write_text(json.dumps(closure))
                argv = ["plan.py", "iso", "--system-closure", str(closure_path)]
                with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(SystemExit, "not a verified System/Packages pair"):
                        plan.main()

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
                    mock.patch.object(plan, "remote_recipe_digest", return_value="9" * 64), \
                    mock.patch.object(plan, "current_component", side_effect=AssertionError("unexpected channel refetch")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(plan.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(resolved, ["FreeSense-org/freesense-packages"])
        self.assertEqual(values["system"], "a" * 64)
        self.assertEqual(values["platform"], "b" * 64)
        self.assertEqual(values["os_base_sha"], "3" * 40)
        self.assertEqual(values["packages_sha"], "8" * 40)
        self.assertEqual(values["package_build_config_sha256"], "9" * 64)

    def test_package_fingerprint_is_independent_of_same_pin_system_update(self):
        fingerprints = []
        for system_id, platform_id in (("a" * 64, "b" * 64), ("c" * 64, "d" * 64)):
            with tempfile.TemporaryDirectory() as directory:
                closure = system_closure()
                closure["fingerprint"] = system_id
                closure["artifact_platform"] = platform_id
                closure["packages_fingerprint"] = ""
                closure_path = Path(directory, "system.json")
                closure_path.write_text(json.dumps(closure))
                argv = ["plan.py", "packages", "--system-closure", str(closure_path)]
                with mock.patch.object(sys, "argv", argv), mock.patch.object(
                    plan, "remote_sha", return_value="8" * 40
                ), mock.patch.object(
                    plan, "remote_recipe_digest", return_value="9" * 64
                ), redirect_stdout(io.StringIO()) as rendered:
                    self.assertEqual(plan.main(), 0)
                fingerprints.append(json.loads(rendered.getvalue())["packages"])
        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_package_fingerprint_changes_with_package_build_configuration(self):
        fingerprints = []
        for config_digest in ("9" * 64, "a" * 64):
            with tempfile.TemporaryDirectory() as directory:
                closure = system_closure()
                closure["packages_fingerprint"] = ""
                closure_path = Path(directory, "system.json")
                closure_path.write_text(json.dumps(closure))
                argv = ["plan.py", "packages", "--system-closure", str(closure_path)]
                with mock.patch.object(sys, "argv", argv), mock.patch.object(
                    plan, "remote_sha", return_value="8" * 40
                ), mock.patch.object(
                    plan, "remote_recipe_digest", return_value=config_digest
                ), redirect_stdout(io.StringIO()) as rendered:
                    self.assertEqual(plan.main(), 0)
                fingerprints.append(json.loads(rendered.getvalue())["packages"])
        self.assertNotEqual(fingerprints[0], fingerprints[1])

    def test_stable_package_fingerprint_changes_with_package_build_configuration(self):
        first = stable_values(package_build_config="b" * 64)
        second = stable_values(package_build_config="c" * 64)
        self.assertNotEqual(first["packages"], second["packages"])
        self.assertEqual(first["package_build_config_sha256"], "b" * 64)
        self.assertEqual(first["osversion"], 1600019)

    def test_stable_package_fingerprint_ignores_system_only_change(self):
        first = stable_values(
            package_build_config="b" * 64,
            system_ports_sha="2" * 40,
        )
        second = stable_values(
            package_build_config="b" * 64,
            system_ports_sha="c" * 40,
        )
        self.assertEqual(first["packages"], second["packages"])
        self.assertNotEqual(first["system"], second["system"])


if __name__ == "__main__":
    unittest.main()
