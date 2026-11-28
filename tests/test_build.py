"""The build driver's check and source logic (build/build, build/packages.py, build/source.py). No network."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

import contextlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

COMPONENT = Path(__file__).resolve().parents[1]
BUILD = COMPONENT / "build"
sys.dont_write_bytecode = True
sys.path.insert(0, str(BUILD))
import packages  # noqa: E402
import source  # noqa: E402
import stages  # noqa: E402
import vm  # noqa: E402

loader = SourceFileLoader("freesense_build", str(BUILD / "build"))
driver = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(driver)

LOCK = json.loads((BUILD / "lock.json").read_text(encoding="utf-8"))
BASE = json.loads((BUILD / "base-provides.json").read_text(encoding="utf-8"))


def entry(version="1.0", origin="x/y", deps=(), requires=(), provides=(), osversion=1600031):
    return {"version": version, "origin": origin, "abi": "FreeBSD:16:amd64", "deps": {name: {} for name in deps},
            "shlibs_required": list(requires), "shlibs_provided": list(provides), "repopath": "All/p.pkg", "sum": "s",
            "pkgsize": 1, "annotations": {"FreeBSD_version": str(osversion)}}


class LockTests(unittest.TestCase):
    def test_lock_is_consistent_with_the_checked_in_inputs(self):
        self.assertTrue(LOCK["targets"][LOCK["target"]]["enabled"])
        self.assertEqual("amd64", LOCK["target"])
        self.assertFalse(LOCK["targets"]["arm64"]["enabled"])
        self.assertEqual(LOCK["freebsd_source"]["commit"], BASE["freebsd_commit"])
        for local in LOCK["tree_files"]["add"].values():
            self.assertTrue((COMPONENT / local).is_file(), local)

    def test_patches_carry_no_binary_data_or_vendored_library(self):
        patches = source.patch_files(COMPONENT)
        self.assertEqual([], source.binary_patches(patches))
        for patch in patches:
            self.assertNotIn(b"b/sys/contrib/intel-ipsec-mb/", patch.read_bytes(), patch.name)

    def test_names_the_installer_and_ports_ask_for_have_an_owner(self):
        """Without the catalogue: every FreeSense name must come from our ports or the pinned base."""
        ours, wanted = packages.requirements(COMPONENT.parent, LOCK["php"])
        for name, who, *_ in wanted:
            if name.startswith("FreeSense"):
                self.assertTrue(name in ours or name in BASE["packages"], f"{name} asked for by {who}")
        self.assertIn("installer", {who for _, who, *_ in wanted})


class NameTests(unittest.TestCase):
    def test_installer_list_and_makefile_words(self):
        self.assertEqual(["a", "b-c"], packages.installer_packages("const array DEFAULT_PACKAGES = ['a',\n 'b-c'];"))
        text = "RUN_DEPENDS+=\tkea>=3.2:net/kea \\\n\tpkg>0:ports-mgmt/pkg # note\n.if x\nRUN_DEPENDS+= c>=1:a/c\n.endif\n"
        self.assertEqual(["kea>=3.2:net/kea", "pkg>0:ports-mgmt/pkg", "c>=1:a/c"], packages.make_words(text, "RUN_DEPENDS"))
        self.assertEqual(("n", "=", "${T}.0", "sysutils/n"), packages.dependency("n=${T}.0:sysutils/n"))
        with self.assertRaises(ValueError):
            packages.dependency("/usr/local/bin/x:devel/x")

    def test_version_order(self):
        ordered = ["1.0rc1", "1.0", "1.0_1", "1.0.1", "1.2", "1.10", "0.1,1"]
        self.assertEqual(ordered, sorted(ordered, key=packages.version_key))
        self.assertTrue(packages.satisfies("3.2.1_1", ">=", "3.2.1"))
        self.assertFalse(packages.satisfies("1.30.5", ">=", "1.30.6"))
        self.assertTrue(packages.satisfies("2.6.0,1", ">=", "0.1,1"))
        self.assertTrue(packages.satisfies("4.4.3P1_1", ">=", "4.4.3"))
        self.assertTrue(packages.satisfies("1.0.0", "=", "1.0"))


class ResolveTests(unittest.TestCase):
    def resolve(self, wanted, catalogue, built_extensions=None):
        lock = {**LOCK, "php": {**LOCK["php"], "built_extensions": built_extensions or {}}}
        base = {"packages": ["FreeSense-pf"], "shlibs": ["libc.so.7"]}
        return packages.resolve({"FreeSense-system": "security/FreeSense-system"}, wanted, catalogue, lock, base)

    def test_closure_over_dependencies_base_and_our_ports(self):
        catalogue = {"app": entry(deps=["lib"], requires=["libz.so.1", "libc.so.7"]), "lib": entry(provides=["libz.so.1"])}
        wanted = [(name, "installer", "", "", "") for name in ("app", "FreeSense-pf", "FreeSense-system")]
        result = self.resolve(wanted, catalogue)
        self.assertEqual([], result["errors"])
        self.assertEqual({"app", "lib"}, set(result["catalogue"]))
        self.assertEqual(["FreeSense-pf"], result["base"])
        self.assertEqual({"FreeSense-system"}, set(result["built"]))
        self.assertEqual(["app", "lib"], [row["name"] for row in packages.manifest(result["catalogue"])])

    def test_every_problem_is_reported(self):
        catalogue = {"app": entry("1.0", "a/other", deps=["gone"], requires=["libnew.so.9"], osversion=1600099)}
        errors = "\n".join(self.resolve([("app", "port", ">=", "2.0", "a/app"), ("nope", "installer", "", "", "")],
                                        catalogue)["errors"])
        for text in ("package gone", "package nope", "needs app>=2.0", "origin a/app", "libnew.so.9", "1600099"):
            self.assertIn(text, errors)

    def test_an_extension_we_build_is_not_looked_up_in_the_catalogue(self):
        wanted = [("php86-thing", "port", "", "", "")]
        self.assertIn("php86-thing", "".join(self.resolve(wanted, {"php86": entry()})["errors"]))
        result = self.resolve(wanted, {"php86": entry()}, {"thing": "freesense-os-base/extensions/thing"})
        self.assertEqual([], result["errors"])
        self.assertIn("php86-thing", result["built"])
        self.assertIn("php86", result["catalogue"])


class CustomPortTests(unittest.TestCase):
    def test_custom_ports_are_ours_and_their_runtime_packages_come_from_the_catalogue(self):
        custom = {"security/vpn": {"package": "vpn", "runtime": ["lib"]}}
        ours, wanted = packages.with_custom_ports({}, [("vpn", "port", ">=", "1.0", "security/vpn")], custom)
        lock = {**LOCK, "custom_ports": custom}
        result = packages.resolve(ours, wanted, {"vpn": entry(), "lib": entry()}, lock, {"packages": [], "shlibs": []})
        self.assertEqual([], result["errors"])
        self.assertEqual({"lib"}, set(result["catalogue"]))
        self.assertEqual("security/vpn", result["built"]["vpn"])

    def test_every_custom_port_is_one_the_system_runs_with_and_options_do_not_contradict(self):
        wanted = {name for name, *_ in packages.requirements(COMPONENT.parent, LOCK["php"])[1]}
        for origin, port in LOCK["custom_ports"].items():
            self.assertIn(port["package"], wanted, origin)
            self.assertFalse(set(port["set"]) & set(port["unset"]), origin)
        # What the appliance uses and the stock packages lack.
        self.assertLessEqual({"EAPRADIUS", "XAUTH"}, set(LOCK["custom_ports"]["security/strongswan"]["set"]))
        self.assertIn("CHROOT", LOCK["custom_ports"]["shells/scponly"]["set"])

    def test_make_conf_and_the_check_of_built_options(self):
        custom = {"security/strongswan": {"package": "strongswan", "set": ["XAUTH", "VSTR"], "unset": ["SMP"]}}
        text = stages.ports_make_conf(custom)
        self.assertIn("security_strongswan_SET_FORCE=\tXAUTH VSTR\n", text)
        self.assertIn("security_strongswan_UNSET_FORCE=\tSMP\n", text)
        port = custom["security/strongswan"]
        self.assertEqual([], stages.option_problems("o", port, {"XAUTH": "on", "VSTR": "on", "SMP": "off"}))
        self.assertEqual(["o: option VSTR is not on", "o: option SMP is on"],
                         stages.option_problems("o", port, {"XAUTH": "on", "SMP": "on"}))


class NamingTests(unittest.TestCase):
    def test_build_id_names_packages_and_image(self):
        self.assertIsNone(stages.package_version(None))
        self.assertEqual("2.0.0.a.20261128.7", stages.package_version("2.0.0-dev.20261128.7"))
        with self.assertRaises(ValueError):
            stages.package_version("2.0.0")
        self.assertEqual("FreeSense-2.0.0.a.1-amd64.iso", stages.iso_name("FreeSense-system-2.0.0.a.1.pkg", "amd64", None))
        self.assertEqual("FreeSense-2.0.0.a.1-amd64.iso", stages.iso_name("FreeSense-system-php86-2.0.0.a.1.pkg", "amd64", None))
        self.assertEqual("FreeSense-B-amd64.iso", stages.iso_name("FreeSense-system-2.0.pkg", "amd64", "B"))


class VmTests(unittest.TestCase):
    def test_guest_script_switches_the_upgrade_off_first_and_reports_the_status(self):
        script = vm.guest_script("n1", "Locked: {}", ["python3"], True, "return 3")
        self.assertLess(script.index("firstboot_pkg_upgrade_enable=NO"), script.index("pkg install"))
        self.assertIn("echo END-n1 $status", script)
        self.assertNotIn("pkg install", vm.guest_script("n1", "", [], False, "true"))
        self.assertNotIn("zpool", vm.guest_script("n1", "", [], False, "true"))

    def test_results_replace_the_old_ones_only_when_the_vm_wrote_some(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, sent, run = root / "work", root / "guest", root / "run"
            for path, text in ((work / "out/old", "old"), (sent / "out/new", "new"), (sent / "state/base.json", "{}")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            run.mkdir()
            (run / "out.raw").write_bytes(bytes(4096))
            vm.collect(run / "out.raw", work)
            self.assertTrue((work / "out/old").is_file())
            subprocess.run(["tar", "-cf", "out.raw", "-C", "../guest", "out", "state"], cwd=run, check=True)
            vm.collect(run / "out.raw", work)
            self.assertEqual(["new"], [path.name for path in (work / "out").iterdir()])
            self.assertTrue((work / "state/base.json").is_file())

    def test_a_linux_build_boots_the_vm_only_for_stale_stages(self):
        calls = []
        saved = driver.run_stages, driver.stale, driver.vm.run_stages
        try:
            driver.run_stages = lambda context, last, forced, **options: calls.append(("host", last, forced))
            driver.vm.run_stages = lambda context, last, forced, base: calls.append(("vm", last, forced, base))
            for stale_stages, expected in (
                    (set(), [("host", "check", None), ("host", "smoke", None)]),
                    ({"iso"}, [("host", "check", None), ("vm", "iso", None, False), ("host", "smoke", None)])):
                driver.stale = lambda context, names: bool(stale_stages & set(names))
                calls.clear()
                with contextlib.redirect_stdout(io.StringIO()):
                    driver.run_on_linux(None, "smoke", None)
                self.assertEqual(expected, calls)
            driver.stale = lambda context, names: True
            calls.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                driver.run_on_linux(None, "base", "check")
            self.assertEqual([("host", "check", "check"), ("vm", "base", "source", True)], calls)
        finally:
            driver.run_stages, driver.stale, driver.vm.run_stages = saved


class LiveLogTests(unittest.TestCase):
    def test_the_live_log_gets_every_line_and_outlives_the_vm(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            os.environ["FREESENSE_LIVE_LOG"] = str(live)
            try:
                with contextlib.redirect_stdout(io.StringIO()) as shown:
                    stages.run_logged([sys.executable, "-c", "print('quiet'); print('>>> step')"], Path(tmp) / "a.log", "work", r"^>>>")
            finally:
                del os.environ["FREESENSE_LIVE_LOG"]
            self.assertIn("quiet\n>>> step\n", live.read_text())
            self.assertNotIn("quiet", shown.getvalue())
            scratch = Path(tmp) / "vm"
            with vm.Scratch(scratch):
                (scratch / "live.log").write_text("kept")
                (scratch / "disk").write_text("gone")
            self.assertEqual(["live.log"], [path.name for path in scratch.iterdir()])


class PatchTests(unittest.TestCase):
    def test_series_applies_to_the_pinned_commit_and_failures_name_the_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree, component = root / "tree", root / "component"
            tree.mkdir()
            (component / "patches").mkdir(parents=True)
            (tree / "a.txt").write_bytes(b"one\n")
            (tree / "old.png").write_bytes(b"old")
            (component / "new.png").write_bytes(b"new\0")
            identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false"]
            for command in (["init", "-q"], ["add", "."], ["commit", "-qm", "base"]):
                subprocess.run(["git", *identity, *command], cwd=tree, check=True, capture_output=True)
            repository = tree / ".git"
            commit = source.git(repository, "rev-parse", "HEAD").strip()
            patch = component / "patches/0001.patch"
            patch.write_bytes(b"diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
            files = {"add": {"new.png": "new.png"}, "remove": ["old.png"]}
            result = source.patched_index(repository, commit, [patch], files, component, root / "tmp/index")
            self.assertEqual({"a.txt", "new.png"},
                             set(source.git(repository, "ls-tree", "--name-only", result).split()))
            self.assertEqual("two\n", source.git(repository, "cat-file", "blob", f"{result}:a.txt"))
            patch.write_bytes(patch.read_bytes().replace(b"-one", b"-other"))
            with self.assertRaisesRegex(ValueError, "0001.patch does not apply"):
                source.patched_index(repository, commit, [patch], files, component, root / "tmp/index")

    def test_a_download_with_another_checksum_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror, cache = Path(tmp) / "mirror", Path(tmp) / "cache"
            mirror.mkdir()
            (mirror / "x.tar").write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "the lock says"):
                source.fetch_file(cache / "x.tar", [], "0" * 64, mirror)
            self.assertFalse((cache / "x.tar").exists())
            digest = source.sha256(mirror / "x.tar")
            self.assertTrue(source.fetch_file(cache / "x.tar", [], digest, mirror).is_file())
            (cache / "x.tar").write_bytes(b"y")
            with self.assertRaisesRegex(ValueError, "does not match"):
                source.fetch_file(cache / "x.tar", [], digest, mirror)


class StageTests(unittest.TestCase):
    def test_unchanged_stages_are_kept_and_from_runs_them_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = SimpleNamespace(work=Path(tmp), state=Path(tmp) / "state")
            versions = {"check": 1, "source": 1}
            failing = set()

            def action(name):
                def run(_):
                    if name in failing:
                        raise ValueError("broken")
                    (context.work / name).write_text("out")
                return run, (name,)

            def go(last="source", forced=None):
                with contextlib.redirect_stdout(io.StringIO()):
                    return driver.run_stages(context, last, forced, actions={n: action(n) for n in versions},
                                             inputs=lambda _, name: versions[name])

            self.assertEqual(["check"], go("check"))
            self.assertEqual(["source"], go())
            self.assertEqual([], go())
            self.assertEqual(["source"], go(forced="source"))
            self.assertEqual(["check", "source"], go(forced="check"))
            versions["check"] = 2
            self.assertEqual(["check"], go())
            (context.work / "source").unlink()
            self.assertEqual(["source"], go())
            failing.add("source")
            versions["source"] = 2
            with self.assertRaisesRegex(ValueError, "stage source: broken"):
                go()
            self.assertFalse((context.state / "source.json").exists())


if __name__ == "__main__":
    unittest.main()
