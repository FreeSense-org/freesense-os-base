"""Guard the independent, two-way System package check and local-only boundaries."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check


class WorkspaceChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(check, "ROOT", self.root))
        self.lib = self.root / check.SYSTEM / "src/usr/local/FreeSense/test.inc"
        self.lib.parent.mkdir(parents=True)
        self.lib.write_text("<?php\n")
        self.manifest = self.root / check.PORTS / check.PLIST
        self.manifest.parent.mkdir(parents=True)

    def test_crlf_and_namespace_layout(self):
        folder = self.root / "layout"
        entry = folder / "src/usr/local/FreeSense/lib/Net/Thing.php"
        entry.parent.mkdir(parents=True)
        entry.write_bytes(b"<?php\nnamespace FreeSense\\Net;\nfinal readonly class Thing {}\n")
        check.layout_check([folder])
        entry.write_bytes(b"<?php\r\nnamespace FreeSense\\Net;\r\nclass Thing {}\r\n")
        with self.assertRaisesRegex(check.CheckError, "CRLF"):
            check.layout_check([folder])
        entry.write_bytes(b"<?php\nnamespace FreeSense\\Other;\nclass Thing {}\n")
        with self.assertRaisesRegex(check.CheckError, "namespace does not match"):
            check.layout_check([folder])
        entry.write_bytes(b"<?php\nnamespace FreeSense\\Net;\nclass Other {}\n")
        with self.assertRaisesRegex(check.CheckError, "file name does not match"):
            check.layout_check([folder])

    def test_prefix_and_mode(self):
        self.manifest.write_text("@(,,755) FreeSense/test.inc\n")
        check.plist_check()

    def test_missing_and_stale(self):
        self.manifest.write_text("FreeSense/absent.inc\n")
        with self.assertRaisesRegex(check.CheckError, "missing .*test.inc.*stale .*absent.inc"):
            check.plist_check()

    def test_same_file_spelled_twice(self):
        self.manifest.write_text("FreeSense/test.inc\n/usr/local/FreeSense/test.inc\n")
        with self.assertRaisesRegex(check.CheckError, "duplicate"):
            check.plist_check()

    def test_executable_must_ship_executable(self):
        self.lib.write_text("#!/usr/local/bin/php\n<?php\n")
        self.manifest.write_text("FreeSense/test.inc\n")
        with self.assertRaisesRegex(check.CheckError, "explicit executable mode"):
            check.plist_check()

    def test_reject_world_writable_entry(self):
        self.manifest.write_text("@(root,wheel,0777) FreeSense/test.inc\n")
        with self.assertRaisesRegex(check.CheckError, "plist directive"):
            check.plist_check()

    def test_extensionless_entry_point_and_reference_boundary(self):
        entry = self.lib.with_name("nic-report")
        entry.write_text("#!/usr/local/bin/php\n<?php require '../active/freesense/old.php';\n")
        self.assertEqual("php", check.kind(entry))
        with self.assertRaisesRegex(check.CheckError, "external reference"):
            check.check_component(self.root / check.SYSTEM, False, {})

    def test_every_script_is_checked_before_the_failure(self):
        scripts = [self.root / "one.sh", self.root / "two.sh"]
        seen = []

        def refuse(args, **options):
            seen.append(args)
            raise check.subprocess.CalledProcessError(1, args)

        with patch.object(check.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), patch.object(check, "run", refuse):
            with self.assertRaisesRegex(check.CheckError, "syntax of one.sh, syntax of two.sh, ShellCheck"):
                check.shell_check(scripts, "sh")
        # One syntax call per script and one ShellCheck call for all of them.
        self.assertEqual([["-n", str(scripts[0])], ["-n", str(scripts[1])], ["-s", "sh", *map(str, scripts)]],
                         [args[1:] for args in seen])

    def test_component_jobs_report_a_blocked_php_stage(self):
        component = self.root / "sample"
        (component / "tests").mkdir(parents=True)
        (component / "tests/OneTest.php").write_text("<?php\n")
        jobs = check.check_component(component, False, None)
        self.assertEqual(["sample: portable PHP"], [job.name for job in jobs])
        with self.assertRaisesRegex(check.CheckError, "not run"):
            jobs[0].action()

    def test_no_guest_is_an_error(self):
        with patch.dict(check.os.environ, {"FREESENSE_TEST_GUEST": ""}):
            with self.assertRaisesRegex(check.CheckError, "FREESENSE_TEST_GUEST"):
                check.run_guest(self.root)

    def test_portable_test_selection_is_ordered_and_scoped(self):
        for case in ["Second", "First"]:
            path = self.root / "freesense/tests" / (case + "Test.php")
            path.parent.mkdir(exist_ok=True)
            path.touch()
        names = ["freesense/tests/SecondTest.php", "freesense/tests/FirstTest.php"]
        self.assertEqual(names, check.portable_test_paths([*names, names[0].replace("/", "\\")], {"freesense"}))
        for bad in ["freesense/tests/../src/OtherTest.php", "freesense/tests/T.php", "freesense/tests/appliance/LiveTest.php",
                    "freesense/tests/lab/ProbeTest.php", "freesense/tests/MissingTest.php", "/tmp/BadTest.php",
                    "C:\\outside\\tests\\BadTest.php", "freesense/tests/name:streamTest.php", "tools/tests/HelperTest.php"]:
            with self.subTest(path=bad), self.assertRaises(check.CheckError):
                check.portable_test_paths([names[0], bad], {"freesense"})

    def test_portable_test_rejects_linked_files_and_directories(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "OneTest.php").touch()
        tests = self.root / "freesense/tests"
        tests.mkdir()
        try:
            (tests / "AliasTest.php").symlink_to(outside / "OneTest.php")
            (tests / "nested").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"test environment cannot create symbolic links: {error}")
        for path in ["AliasTest.php", "nested/OneTest.php"]:
            with self.subTest(path=path), self.assertRaisesRegex(check.CheckError, "linked"):
                check.portable_test_paths(["freesense/tests/" + path], {"freesense"})

    @unittest.skipUnless(sys.platform == "win32", "Windows directory junction")
    def test_portable_test_rejects_windows_junction(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "OneTest.php").touch()
        tests = self.root / "freesense/tests"
        tests.mkdir()
        check.subprocess.run(["cmd", "/d", "/c", "mklink", "/j", "linked", r"..\..\outside"],
                             cwd=tests, check=True, capture_output=True)
        with self.assertRaisesRegex(check.CheckError, "linked"):
            check.portable_test_paths(["freesense/tests/linked/OneTest.php"], {"freesense"})

    def test_guest_interpreter_is_used_only_for_native_appliance_phase(self):
        component = self.root / "sample"
        component.mkdir()
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("check-stage.sh", "php-check.php"):
            (tools / name).touch()
        guest_php = "/opt/lab-php/bin/php"
        with patch.dict(check.os.environ, {"FREESENSE_TEST_PHP": guest_php}), \
                patch.object(check.shutil, "which", side_effect=lambda cmd: "/usr/bin/" + cmd), \
                patch.object(check.platform, "system", return_value="FreeBSD"), patch.object(check, "run") as invoked:
            check.php_checks(component, [], False, {})
            self.assertNotIn("FREESENSE_TEST_PHP", invoked.call_args.kwargs["env"])
            check.php_checks(component, [], True, {})
            self.assertEqual(guest_php, invoked.call_args.kwargs["env"]["FREESENSE_TEST_PHP"])
