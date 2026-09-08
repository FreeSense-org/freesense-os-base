from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import binary_seed
import multiarch_pin
import multiarch_plan
import package_requirements
import seal_multiarch_pin
import arm64_capability
import verify_multiarch_canary


def pin_fixture():
    def blob(letter):
        return {"sha256": letter * 64, "object": "inputs/sha256/" + letter * 64, "size": 100}
    pin = {
        "schema_version": "freesense.freebsd-pin/v4",
        "valid_from": "2026-09-01T00:00:00Z", "valid_until": "2026-09-15T00:00:00Z",
        "freebsd_source": {"commit": "a" * 40}, "freebsd_ports": {"commit": "b" * 40},
        "targets": {},
    }
    for arch, abi in multiarch_pin.ARCHES.items():
        pin["targets"][arch] = {
            "abi": abi, "ready": True, "jail_seed": blob("a"),
            "package_catalog": {**blob("b"), "signature_verified": True, "trusted_key_sha256": "c" * 64},
            "binary_seed": {
                **blob("d"), "abi": abi, "catalog_sha256": "b" * 64, "verified": True,
                "requirements_components": ["system", "packages"], "requirements_sha256": "e" * 64,
                "provenance_sha256": "f" * 64, "verified_roots": ["rust"],
            },
            "worker_image": {**blob("e"), "architecture": arch, "boot_verified": True},
            "worker_tools": {**blob("f"), "architecture": arch, "boot_verified": True},
        }
    return pin


def requirement(name="rust", origin="lang/rust", deps=None):
    return {"name": name, "origin": origin, "version": "1.90.0_1,1",
            "abi": "FreeBSD:16:amd64", "deps": deps or {}, "options": {"DOCS": "off"},
            **{flag: False for flag in binary_seed.FLAGS}}


def catalogue(req, payload=b"verified package bytes"):
    return {**{k: v for k, v in req.items() if k not in binary_seed.FLAGS},
            "repopath": f"All/{req['name']}-{req['version']}.pkg", "pkgsize": len(payload),
            "sum": hashlib.sha256(payload).hexdigest()}


def requirements(*packages):
    return {"schema_version": "freesense.package-requirements/v1", "abi": "FreeBSD:16:amd64",
            "roots": {"system": [packages[0]["name"]], "packages": [packages[-1]["name"]]},
            "packages": list(packages)}


class BinarySeedTests(unittest.TestCase):
    def test_accepts_matching_package_and_options_booleans(self):
        req = requirement()
        pkg = catalogue(req)
        pkg["options"] = {"DOCS": False}
        result = binary_seed.select(requirements(req), [pkg], "amd64")
        self.assertEqual(list(result["accepted"]), ["rust"])

    def test_mandatory_blacklist_cannot_be_overridden(self):
        for name, origin in (("php85", "lang/php85"), ("php85-curl", "ftp/php85-curl"),
                             ("FreeSense", "security/FreeSense"), ("world", "base/world"),
                             ("kernel", "base/kernel"), ("wireguard-kmod", "net/wireguard-kmod")):
            with self.subTest(name=name):
                req = requirement(name, origin)
                rust = requirement()
                result = binary_seed.select(requirements(rust, req), [catalogue(rust), catalogue(req)], "amd64")
                self.assertIn(name, result["rejected"])

    def test_customizations_and_missing_audit_fail_closed(self):
        for flag in binary_seed.FLAGS:
            with self.subTest(flag=flag):
                req = requirement()
                req[flag] = True
                with self.assertRaisesRegex(ValueError, "requires a compatible official"):
                    binary_seed.select(requirements(req), [catalogue(req)], "amd64")
                del req[flag]
                with self.assertRaisesRegex(ValueError, "eligibility audit"):
                    binary_seed.select(requirements(req), [catalogue(req)], "amd64")

    def test_rejects_version_epoch_revision_origin_abi_options_and_dependency_mismatches(self):
        for field, value in (("version", "1.90.0_1"), ("version", "1.90.0,1"),
                             ("origin", "lang/rust-nightly"), ("abi", "FreeBSD:16:aarch64"),
                             ("options", {"DOCS": "on"}),
                             ("deps", {"lib": {"origin": "devel/lib", "version": "1"}})):
            with self.subTest(field=field, value=value):
                req = requirement()
                pkg = catalogue(req)
                pkg[field] = value
                with self.assertRaises(ValueError):
                    binary_seed.select(requirements(req), [pkg], "amd64")

    def test_rejects_duplicate_catalogue_names_and_cross_arch_requirements(self):
        req = requirement()
        with self.assertRaises(ValueError):
            binary_seed.select(requirements(req), [catalogue(req)] * 2, "amd64")
        with self.assertRaises(ValueError):
            binary_seed.select(requirements(req), [catalogue(req)], "arm64")

    def test_transitive_customized_dependencies_are_not_reused(self):
        lib = requirement("lib", "devel/lib")
        middle = requirement("middle", "devel/middle", {"lib": {"version": lib["version"], "origin": lib["origin"]}})
        rust = requirement(deps={"middle": {"version": middle["version"], "origin": middle["origin"]}})
        lib["custom_patches"] = True
        with self.assertRaisesRegex(ValueError, "requires a compatible official"):
            binary_seed.select(requirements(rust, middle, lib), list(map(catalogue, (rust, middle, lib))), "amd64")

    def test_bundle_is_deterministic_and_records_upstream_provenance(self):
        req = requirement()
        selected = binary_seed.select(requirements(req), [catalogue(req)], "amd64")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "All").mkdir()
            package = root / catalogue(req)["repopath"]
            package.write_bytes(b"verified package bytes")
            first = binary_seed.bundle(selected, root, root / "a.tar", abi=req["abi"], catalog_sha256="a" * 64)
            second = binary_seed.bundle(selected, root, root / "b.tar", abi=req["abi"], catalog_sha256="a" * 64)
            self.assertEqual(first, second)
            with tarfile.open(root / "a.tar") as archive:
                manifest = json.load(archive.extractfile("provenance.json"))
                self.assertEqual(manifest["packages"][0]["upstream_checksum"], catalogue(req)["sum"])
            package.write_bytes(b"corrupt package bytes!")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                binary_seed.bundle(selected, root, root / "c.tar", abi=req["abi"], catalog_sha256="a" * 64)

    def test_wrong_catalogue_key_and_bad_signature_are_rejected(self):
        def member(command):
            return {"packagesite.yaml": b"{}\n", "packagesite.yaml.pub": b"key",
                    "packagesite.yaml.sig": b"signature"}[command[-1]]
        with mock.patch.object(subprocess, "check_output", side_effect=member):
            with self.assertRaisesRegex(ValueError, "trust root"):
                binary_seed.verify_catalogue(Path("catalog.pkg"), "0" * 64)
            with mock.patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, "openssl")):
                with self.assertRaises(subprocess.CalledProcessError):
                    binary_seed.verify_catalogue(Path("catalog.pkg"), hashlib.sha256(b"key").hexdigest())


class MultiarchPinTests(unittest.TestCase):
    def test_complete_pin_and_native_fallback_inputs(self):
        pin = pin_fixture()
        multiarch_pin.validate(pin, now=datetime(2026, 9, 6, tzinfo=timezone.utc))
        self.assertEqual(multiarch_pin.worker(pin, "arm64", "dedicated")["worker_image"]["architecture"], "amd64")
        self.assertEqual(multiarch_pin.worker(pin, "arm64", "github-arm64")["executor"], "native-arm64")
        with self.assertRaises(ValueError):
            multiarch_pin.worker(pin, "amd64", "github-arm64")

    def test_missing_or_incompatible_target_blocks_whole_pin(self):
        for field in ("jail_seed", "package_catalog", "binary_seed", "worker_image", "worker_tools"):
            pin = pin_fixture()
            del pin["targets"]["arm64"][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                multiarch_pin.validate(pin)
        for field, key, value in (("binary_seed", "verified_roots", []), ("binary_seed", "abi", "FreeBSD:16:amd64"),
                                  ("package_catalog", "signature_verified", False), ("worker_image", "boot_verified", False)):
            pin = pin_fixture()
            pin["targets"]["arm64"][field][key] = value
            with self.subTest(field=field, key=key), self.assertRaises(ValueError):
                multiarch_pin.validate(pin)

    def test_rollover_preserves_previous_and_requires_explicit_security_movement(self):
        previous = pin_fixture()
        candidate = copy.deepcopy(previous)
        candidate.update(valid_from="2026-09-10T00:00:00Z", valid_until="2026-09-24T00:00:00Z")
        with self.assertRaises(ValueError):
            multiarch_pin.rollover(previous, candidate)
        self.assertEqual(previous, pin_fixture())
        self.assertEqual(multiarch_pin.rollover(previous, candidate, security_rollover=True), candidate)
        candidate.update(valid_from="2026-09-15T00:00:00Z", valid_until="2026-09-29T00:00:00Z")
        self.assertEqual(multiarch_pin.rollover(previous, candidate), candidate)

    def test_probe_failure_or_wrong_image_selects_dedicated_before_pair_identity(self):
        pin = pin_fixture()
        probe = {"schema_version": "freesense.arm64-capability/v1", "image_sha256": "e" * 64,
                 **{key: True for key in ("kvm", "memory", "disk", "qemu", "firmware", "boot")}}
        self.assertEqual(multiarch_pin.select_arm_host(probe, pin), "github-arm64")
        self.assertEqual(multiarch_pin.select_arm_host(probe, pin, force_dedicated=True), "dedicated")
        for key in ("kvm", "memory", "disk", "qemu", "firmware", "boot", "image_sha256"):
            with self.subTest(key=key):
                self.assertEqual(multiarch_pin.select_arm_host({**probe, key: False}, pin), "dedicated")
        fps = {arch: {"system": "a" * 64, "packages": "b" * 64} for arch in multiarch_pin.ARCHES}
        native = multiarch_plan.plan(pin, probe, fps)
        fallback = multiarch_plan.plan(pin, probe, fps, force_dedicated=True)
        self.assertNotEqual(native["pair_fingerprint"], fallback["pair_fingerprint"])
        self.assertEqual(len(native["system_matrix"]["include"]), 10)
        self.assertEqual(len(native["packages_matrix"]["include"]), 8)

    def test_shards_cover_roots_once_and_isolate_measured_heavy_roots(self):
        roots = ["net/b", "net/a", "lang/heavy", "devel/c", "devel/d", "net/a"]
        shards = multiarch_plan.shard_roots(roots, heavy=["lang/heavy"])
        self.assertEqual(shards[0], ["lang/heavy"])
        self.assertEqual(sorted(root for shard in shards for root in shard), sorted(set(roots)))
        self.assertEqual(shards, multiarch_plan.shard_roots(list(reversed(roots)), heavy=["lang/heavy"]))


class RequirementsCollectorTests(unittest.TestCase):
    def test_current_make_conf_is_audited_and_unknown_knobs_fail_closed(self):
        root = Path(__file__).resolve().parents[2]
        path = root / "freesense/tools/conf/pfPorts/make.conf"
        if path.exists():
            self.assertIn("PHP_FD_SETSIZE", package_requirements.audit_make_conf(path.read_text()))
        with self.assertRaisesRegex(ValueError, "non-OPTIONS knob"):
            package_requirements.audit_make_conf("CFLAGS+= -DUNREVIEWED\n")
        with self.assertRaisesRegex(ValueError, "directive"):
            package_requirements.audit_make_conf('.include "other.conf"\n')

    def test_dependency_origins_preserve_flavors_and_reject_unsupported_subpackages(self):
        self.assertEqual(package_requirements.dependency_origin("git>=2:devel/git@tiny"), "devel/git@tiny")
        for value in ("pkg:../outside", "pkg:devel/lib~dev", "unresolved", "pkg:/absolute/path"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                package_requirements.dependency_origin(value)

    def test_collects_build_dependencies_without_misclassifying_them_as_runtime_dependencies(self):
        collector = object.__new__(package_requirements.Collector)
        collector.abi, collector.knobs, collector.changed = "FreeBSD:16:amd64", set(), set()
        collector.records, collector.by_origin = {}, {}
        def values(origin, names):
            result = {name: "" for name in names}
            result.update(PKGBASE=origin.split("/")[1], PKGVERSION="1", PKGORIGIN=origin,
                          COMPLETE_OPTIONS_LIST="DOCS", PORT_OPTIONS="")
            if origin == "net/app":
                result.update(BUILD_DEPENDS="rust>=1:lang/rust", RUN_DEPENDS="lib>=1:devel/lib")
            return result
        collector.values = values
        with tempfile.TemporaryDirectory() as directory:
            collector.make_conf = Path(directory) / "make.conf"
            collector.make_conf.write_text("")
            report = collector.collect({"system": ["net/app"], "packages": ["net/app"]})
        packages = {pkg["name"]: pkg for pkg in report["packages"]}
        self.assertEqual(set(packages), {"app", "lib", "rust", "pkg"})
        self.assertEqual(set(packages["app"]["deps"]), {"lib"})

    def test_collector_allows_audited_mk_overlay_helper(self):
        audited_mk = {"Mk/bsd.freesense-package.mk"}
        allowed_paths = {"Mk/bsd.freesense-package.mk", "net/sample/Makefile"}
        self.assertFalse(any(path.startswith(("Templates/", "Keywords/")) or (path.startswith("Mk/") and path not in audited_mk) for path in allowed_paths))
        unauthorized_paths = {"Mk/bsd.unauthorized.mk"}
        self.assertTrue(any(path.startswith(("Templates/", "Keywords/")) or (path.startswith("Mk/") and path not in audited_mk) for path in unauthorized_paths))

    def test_installer_allows_bidirectional_one_revision_osversion_skew(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/runner/install-worker-tools.sh").read_text()
        self.assertIn("running_osversion + 1", script)
        self.assertIn("required_osversion + 1", script)

    def test_collector_handles_origin_aliases_for_identical_package(self):
        collector = object.__new__(package_requirements.Collector)
        collector.abi, collector.knobs, collector.changed = "FreeBSD:16:amd64", set(), set()
        collector.records, collector.by_origin = {}, {}
        def values(origin, names):
            result = {name: "" for name in names}
            if origin in ("textproc/py-docutils", "textproc/py-docutils@py312"):
                result.update(PKGBASE="py312-docutils", PKGVERSION="0.21.2", PKGORIGIN="textproc/py-docutils",
                              COMPLETE_OPTIONS_LIST="", PORT_OPTIONS="", USES="python", EXTRA_PATCHES="", SUBPACKAGES="")
            return result
        collector.values = values
        self.assertEqual(collector.visit("textproc/py-docutils"), "py312-docutils")
        # Visiting the flavored alias should resolve without conflict
        self.assertEqual(collector.visit("textproc/py-docutils@py312"), "py312-docutils")

    def test_collector_rejects_conflicting_package_records(self):
        collector = object.__new__(package_requirements.Collector)
        collector.abi, collector.knobs, collector.changed = "FreeBSD:16:amd64", set(), set()
        collector.records, collector.by_origin = {}, {}
        def values(origin, names):
            result = {name: "" for name in names}
            pkgorigin = "textproc/py-docutils" if origin == "textproc/py-docutils" else "other/py-docutils"
            result.update(PKGBASE="py312-docutils", PKGVERSION="0.21.2", PKGORIGIN=pkgorigin,
                          COMPLETE_OPTIONS_LIST="", PORT_OPTIONS="", USES="python", EXTRA_PATCHES="", SUBPACKAGES="")
            return result
        collector.values = values
        self.assertEqual(collector.visit("textproc/py-docutils"), "py312-docutils")
        with self.assertRaisesRegex(ValueError, "conflicting package name across ports/flavors"):
            collector.visit("other/py-docutils")


class PinSealingTests(unittest.TestCase):
    def prepare(self, root):
        candidate = pin_fixture()
        candidate.update(valid_from="2026-09-15T00:00:00Z", valid_until="2026-09-29T00:00:00Z")
        for arch in multiarch_pin.ARCHES:
            directory = root / arch
            directory.mkdir()
            target = candidate["targets"][arch]
            for field, filename in seal_multiarch_pin.FILES.items():
                data = f"{arch}/{field} verified bytes".encode()
                (directory / filename).write_bytes(data)
                sha = hashlib.sha256(data).hexdigest()
                target[field].update(sha256=sha, object=f"inputs/sha256/{sha}", size=len(data))
            target["binary_seed"]["catalog_sha256"] = target["package_catalog"]["sha256"]
            (directory / "worker-evidence.json").write_text(json.dumps({
                "schema_version": "freesense.worker-evidence/v1", "architecture": arch, "boot": True, "tools": True,
                "worker_image_sha256": target["worker_image"]["sha256"],
                "worker_tools_sha256": target["worker_tools"]["sha256"],
                "requirements_sha256": target["binary_seed"]["requirements_sha256"],
            }))
        pin = root / "active.json"
        pin.write_text(json.dumps(pin_fixture()))
        return pin, candidate

    def test_failed_architecture_leaves_active_pin_unchanged_and_mirrors_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, candidate = self.prepare(root)
            before = path.read_bytes()
            (root / "arm64/binary-seed.tar").write_bytes(b"corrupt")
            mirror = mock.Mock()
            with self.assertRaisesRegex(ValueError, "hash/size mismatch"):
                seal_multiarch_pin.seal(path, candidate, root, mirror=mirror)
            mirror.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_mirror_failure_cannot_advance_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, candidate = self.prepare(root)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "conflicting identity"):
                seal_multiarch_pin.seal(path, candidate, root, mirror=lambda path: {})
            self.assertEqual(path.read_bytes(), before)

    def test_complete_mirror_advances_one_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, candidate = self.prepare(root)
            mirrored = []
            def mirror(path):
                mirrored.append(path)
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                return {"key": f"inputs/sha256/{sha}", "sha256": sha, "size": path.stat().st_size}
            seal_multiarch_pin.seal(path, candidate, root, mirror=mirror)
            self.assertEqual(len(mirrored), 10)
            self.assertEqual(json.loads(path.read_text()), candidate)


class CapabilityProcessTests(unittest.TestCase):
    def test_timeout_requests_launcher_cleanup_before_killing(self):
        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.wait.side_effect = [subprocess.TimeoutExpired("probe", 900), 143]
        with mock.patch.object(subprocess, "Popen", return_value=process):
            self.assertFalse(arm64_capability.run_probe(["bash", "probe.sh"]))
        process.send_signal.assert_called_once()
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=900), mock.call(timeout=45)])

    def test_success_does_not_send_a_termination_signal(self):
        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.wait.return_value = 0
        with mock.patch.object(subprocess, "Popen", return_value=process):
            self.assertTrue(arm64_capability.run_probe(["bash", "probe.sh"]))
        process.send_signal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
