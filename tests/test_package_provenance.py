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

    def _tree(self, root):
        ports = root / "ports"
        (ports / "Mk").mkdir(parents=True); (ports / "Mk/bsd.port.mk").write_text("mk")
        (ports / "devel/a").mkdir(parents=True); (ports / "devel/a/Makefile").write_text("a")
        config, policy = root / "make.conf", root / "policy.json"
        config.write_text("OPTIONS_SET=TLS"); policy.write_text("{}")
        return ports, config, policy

    def test_a_core_package_has_no_port_and_takes_the_build_inputs(self):
        """core_pkg_create stamps origins no ports tree holds.

        FreeSense-base and its siblings are cut from the staged chroot and the
        built kernel. Recording them as external would let a dependent look
        unchanged across a kernel change, so their port digest stands in for the
        revisions they were cut from and they are always kernel-sensitive.
        """
        core = "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ports, config, policy = self._tree(root)
            records = [
                {"name": "FreeSense-base", "origin": "security/FreeSense-base",
                 "version": "1", "options": {}, "dependencies": {}},
                {"name": "FreeSense-kernel-FreeSense", "origin": "security/FreeSense-kernel",
                 "version": "1", "options": {}, "dependencies": {}},
                {"name": "FreeSense-u-boot", "origin": "sysutil/FreeSense-u-boot",
                 "version": "1", "options": {}, "dependencies": {}},
                {"name": "a", "origin": "devel/a", "version": "1", "options": {},
                 "dependencies": {"FreeSense-base": {"origin": "security/FreeSense-base", "version": "1"}}},
            ]
            kwargs = dict(ports=ports, overlays=[], make_config=config, architecture_policy=policy,
                          abi="FreeBSD:16:amd64", osversion=1600001, product="FreeSense")
            built = package_provenance.build(records, core_inputs_sha256=core, **kwargs)
            items = {item["name"]: item["provenance"] for item in built["packages"]}
            self.assertEqual(len(items), 4)
            for name in ("FreeSense-base", "FreeSense-kernel-FreeSense", "FreeSense-u-boot"):
                self.assertEqual(items[name]["port_directory_sha256"], core)
                self.assertTrue(items[name]["kernel_sensitive"], name)
                self.assertFalse(items[name]["patched"], name)
            # A core package is never reusable, and a dependent notices it moved.
            self.assertNotIn("FreeSense-base",
                             package_provenance.select(built, built, pin_unchanged=True)["accepted"])
            moved = package_provenance.build(records, core_inputs_sha256="c" * 64, **kwargs)
            self.assertEqual(package_provenance.select(built, moved, pin_unchanged=True)["accepted"], [])

    def test_an_absent_origin_that_is_not_a_core_package_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ports, config, policy = self._tree(root)
            kwargs = dict(ports=ports, overlays=[], make_config=config, architecture_policy=policy,
                          abi="FreeBSD:16:amd64", osversion=1600001, product="FreeSense")
            gone = [{"name": "unbound", "origin": "dns/unbound", "version": "1",
                     "options": {}, "dependencies": {}}]
            with self.assertRaisesRegex(ValueError, "package origin is absent"):
                package_provenance.build(gone, core_inputs_sha256="b" * 64, **kwargs)
            # Product-named but the product was not declared: still an error.
            core = [{"name": "FreeSense-base", "origin": "security/FreeSense-base",
                     "version": "1", "options": {}, "dependencies": {}}]
            with self.assertRaisesRegex(ValueError, "package origin is absent"):
                package_provenance.build(core, **{**kwargs, "product": ""}, core_inputs_sha256="b" * 64)
            # Declared, but no build-input digest to bind it to.
            with self.assertRaisesRegex(ValueError, "build-input digest"):
                package_provenance.build(core, core_inputs_sha256="", **kwargs)

    def test_core_package_discrimination(self):
        for name, origin in (("FreeSense-base", "security/FreeSense-base"),
                             ("FreeSense-u-boot", "sysutil/FreeSense-u-boot"),
                             ("FreeSense-default-config-serial", "security/FreeSense-default-config-serial")):
            self.assertTrue(package_provenance.core_package(name, origin, "FreeSense"), origin)
        for name, origin in (("FreeSense", "security/FreeSense"),
                             ("unbound", "dns/unbound"),
                             ("FreeSense-base", "security/Other-base"),
                             ("other", "security/FreeSense-base")):
            self.assertFalse(package_provenance.core_package(name, origin, "FreeSense"), origin)

    def test_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "real").mkdir(); (root / "real/file").write_text("x")
            try: (root / "real/link").symlink_to(root / "real/file")
            except OSError: self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "symlink"):
                package_provenance.tree_digest(root / "real")
