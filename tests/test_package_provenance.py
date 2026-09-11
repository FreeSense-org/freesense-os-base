from pathlib import Path
import tempfile
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import package_provenance


class PackageProvenanceTests(unittest.TestCase):
    def test_source_mk_config_policy_and_dependencies_invalidate_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ports = root / "ports"
            (ports / "Mk").mkdir(parents=True); (ports / "Mk/bsd.port.mk").write_text("mk")
            (ports / "devel/a").mkdir(parents=True); (ports / "devel/a/Makefile").write_text("a")
            (ports / "devel/b").mkdir(parents=True); (ports / "devel/b/Makefile").write_text("b")
            config, policy = root / "make.conf", root / "policy.json"
            config.write_text("OPTIONS_SET=TLS"); policy.write_text("{}")
            records = [
                {"name":"a", "origin":"devel/a", "version":"1", "options":{}, "dependencies":{}},
                {"name":"b", "origin":"devel/b", "version":"1", "options":{}, "dependencies":{"a":{"origin":"devel/a", "version":"1"}}},
            ]
            (ports / "devel/c").mkdir(parents=True); (ports / "devel/c/Makefile").write_text("c")
            records.append({"name":"c", "origin":"devel/c", "version":"1", "options":{},
                            "dependencies":{"b":{"origin":"devel/b", "version":"1"}}})
            first = package_provenance.build(records, ports=ports, overlays=[], make_config=config,
                                               architecture_policy=policy, abi="FreeBSD:16:amd64", osversion=1600001)
            self.assertEqual(package_provenance.select(first, first, pin_unchanged=True)["accepted"], ["a", "b", "c"])
            (ports / "devel/a/Makefile").write_text("changed")
            second = package_provenance.build(records, ports=ports, overlays=[], make_config=config,
                                                architecture_policy=policy, abi="FreeBSD:16:amd64", osversion=1600001)
            selected = package_provenance.select(first, second, pin_unchanged=True)
            self.assertEqual(selected["accepted"], [])

    def test_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "real").mkdir(); (root / "real/file").write_text("x")
            try: (root / "real/link").symlink_to(root / "real/file")
            except OSError: self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "symlink"):
                package_provenance.tree_digest(root / "real")
