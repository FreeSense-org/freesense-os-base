"""Verify pinned encoding and independent rebuild inputs on synthetic local trees."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from component_inputs import component_inputs, fingerprint, recipe_digest


class ComponentInputsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def inputs(self, pin="a" * 64):
        return component_inputs(self.root, pin, "amd64")

    def test_pinned_development_encoding(self):
        alpha = self.write("alpha", b"abc\x00")
        beta = self.write("beta", b"xyz\n")
        self.assertEqual(recipe_digest(self.root, [beta, alpha]),
                         "ccdc6327095420f9ea222260f28b22f695b4d350f6e7b12eb0150b67bb0beb75")
        self.assertEqual(fingerprint({"z": ["foo"], "a": 6}),
                         "f323fae55c68dfd255b6c8f7b7c676de86a2d20f43c478c6ee6f84aa0c0d00b8")

    def test_component_independence(self):
        self.write("freesense/LICENSE", b"license")
        self.write("freesense/src/etc/sample", b"config")
        self.write("freesense-system-ports/security/System/pkg-plist", b"entry")
        first = self.inputs()
        self.assertIsNone(first["packages"])
        self.write("freesense-packages/net/example/Makefile", b"optional")
        with self.assertRaises(FileNotFoundError):
            self.inputs()
        self.write("freesense/tools/conf/pfPorts/make.conf", b"settings")
        installed = self.inputs()
        self.assertEqual(first["system"], installed["system"])
        self.write("freesense/src/etc/sample", b"changed")
        system = self.inputs()
        self.assertNotEqual(system["system"], installed["system"])
        self.assertEqual(system["packages"], installed["packages"])
        self.write("freesense/LICENSE", b"revised license")
        revised = self.inputs()
        self.assertNotEqual(system["system"], revised["system"])
        self.assertEqual(system["packages"], revised["packages"])
        self.write("freesense/tools/conf/pfPorts/make.conf", b"new settings")
        options = self.inputs()
        self.assertEqual(revised["system"], options["system"])
        self.assertNotEqual(system["packages"], options["packages"])
        self.write("freesense-packages/net/example/Makefile", b"changed package")
        packages = self.inputs()
        self.assertEqual(options["system"], packages["system"])
        self.assertNotEqual(options["packages"], packages["packages"])
        repin = self.inputs("b" * 64)
        for component in packages:
            self.assertNotEqual(repin[component], packages[component])
