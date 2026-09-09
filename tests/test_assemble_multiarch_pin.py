import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("assemble_multiarch_pin", ROOT / "scripts" / "assemble_multiarch_pin.py")
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)


def sha(char): return char * 64


def item(char, **extra):
    value = sha(char)
    return {"object": "inputs/sha256/" + value, "sha256": value, "size": 10, **extra}


def fixture():
    common = {
        "schema_version": "freesense.freebsd-pin-common/v1",
        "valid_from": "2026-09-12T11:32:44Z", "valid_until": "2026-09-26T11:32:44Z",
        "freebsd_source": {"commit": "a" * 40, "osversion": 1600021},
        "bootstrap_snapshot": {"commit": "a" * 40, "revision_prefix": "a" * 12,
                               "build_date": "20260907", "osversion": 1600021},
        "freebsd_ports": {"commit": "b" * 40},
    }
    reports = {}
    for number, (arch, abi) in enumerate(module.ARCHES.items()):
        catalog = item(str(number + 1), signature_verified=True, trusted_key_sha256=sha("c"))
        seed = item(str(number + 3), abi=abi, catalog_sha256=catalog["sha256"], verified=True,
                    requirements_components=["system", "packages"], requirements_sha256=sha("d"),
                    provenance_sha256=sha("e"), verified_roots=["rust"], package_count=1)
        image = item(str(number + 5), architecture=arch, boot_verified=True)
        tools = item(str(number + 7), architecture=arch, boot_verified=True)
        reports[arch] = {"schema_version": "freesense.freebsd-pin-target/v1", "architecture": arch,
                         "abi": abi, "ready": True, "jail_seed": item("9"),
                         "package_catalog": catalog, "binary_seed": seed,
                         "worker_image": image, "worker_tools": tools,
                         "evidence": {"schema_version": "freesense.worker-evidence/v1",
                                      "architecture": arch, "boot": True, "tools": True,
                                      "worker_image_sha256": image["sha256"],
                                      "worker_tools_sha256": tools["sha256"],
                                      "requirements_sha256": seed["requirements_sha256"]}}
    return common, reports


class AssembleTests(unittest.TestCase):
    def test_requires_both_complete_native_reports(self):
        common, reports = fixture()
        candidate = module.assemble(common, reports)
        self.assertEqual(candidate["schema_version"], "freesense.freebsd-pin/v4")
        for fault in ("missing", "not-ready", "wrong-evidence"):
            changed = copy.deepcopy(reports)
            if fault == "missing": del changed["arm64"]
            elif fault == "not-ready": changed["arm64"]["ready"] = False
            else: changed["arm64"]["evidence"]["worker_image_sha256"] = sha("f")
            with self.assertRaises(ValueError): module.assemble(common, changed)

    def test_accepts_reports_with_key_instead_of_object(self):
        common, reports = fixture()
        for arch in module.ARCHES:
            for field in ("jail_seed", "package_catalog", "binary_seed", "worker_image", "worker_tools"):
                entry = reports[arch][field]
                entry["key"] = entry.pop("object")
        candidate = module.assemble(common, reports)
        for arch in module.ARCHES:
            for field in ("jail_seed", "package_catalog", "binary_seed", "worker_image", "worker_tools"):
                self.assertIn("object", candidate["targets"][arch][field])
                self.assertEqual(candidate["targets"][arch][field]["object"],
                                 "inputs/sha256/" + candidate["targets"][arch][field]["sha256"])


if __name__ == "__main__": unittest.main()
