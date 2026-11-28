"""Contracts for the local cross-overlay check; no external port tree required."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import port_ownership as ports


class PortOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(redirect_stdout(StringIO()))
        self.system = self.root / ports.SYSTEM
        self.system.mkdir()
        self.optional = self.root / ports.OPTIONAL
        self.files = {"/usr/local/sbin/freesense-config": 0o555}

    def put(self, path: Path, content: str = ""):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def audit(self):
        ports.audit(self.root, self.files)

    def test_standalone_system(self):
        self.audit()
        self.assertFalse(self.optional.exists())

    def test_nested_wrapper_in_system(self):
        self.put(self.system / "devel/demo/files/FreeSense-pkg-Demo/x")
        with self.assertRaisesRegex(ValueError, "optional wrapper"):
            self.audit()

    def test_framework_in_system(self):
        self.put(self.system / ports.FRAMEWORK)
        with self.assertRaisesRegex(ValueError, "framework.*System"):
            self.audit()

    def test_missing_optional_framework(self):
        self.optional.mkdir()
        with self.assertRaisesRegex(ValueError, "requires Mk/bsd"):
            self.audit()

    def test_reserved_origin_even_without_makefile(self):
        self.put(self.optional / ports.FRAMEWORK)
        self.put(self.optional / "emulators/open-vm-tools/dummy")
        with self.assertRaisesRegex(ValueError, "emulators/open-vm-tools"):
            self.audit()

    def test_named_wrapper_and_third_party_overlay_are_allowed(self):
        self.put(self.optional / ports.FRAMEWORK)
        self.put(self.optional / "emulators/FreeSense-pkg-Open-VM-Tools/Makefile")
        self.put(self.optional / "net/haproxy/Makefile")
        self.audit()

    def test_admitted_system_origin_is_reserved(self):
        self.put(self.optional / ports.FRAMEWORK)
        self.put(self.system / "net/new-port/Makefile")
        self.put(self.optional / "net/new-port/files/patch")
        with self.assertRaisesRegex(ValueError, "net/new-port"):
            self.audit()

    def test_prefix_and_manifest_attributes_cannot_hide_overlap(self):
        self.put(self.optional / ports.FRAMEWORK)
        port = self.optional / "net/FreeSense-pkg-A"
        self.put(port / "Makefile")
        self.put(port / "pkg-plist", "@(root,wheel,0755) sbin/freesense-config\n")
        with self.assertRaisesRegex(ValueError, "sbin/freesense-config claimed by System"):
            self.audit()

    def test_shell_keyword_is_a_concrete_claim(self):
        self.put(self.optional / ports.FRAMEWORK)
        self.put(self.system / "sysutils/demo-shell/Makefile")
        self.put(self.system / "sysutils/demo-shell/pkg-plist", "@shell sbin/demo_shell\n")
        port = self.optional / "net/FreeSense-pkg-A"
        self.put(port / "Makefile")
        self.put(port / "pkg-plist", "sbin/demo_shell\n")
        with self.assertRaisesRegex(ValueError, "sbin/demo_shell claimed by System sysutils/demo-shell"):
            self.audit()

    def test_inline_files_collide_but_shared_directories_do_not(self):
        self.put(self.optional / ports.FRAMEWORK)
        port = self.optional / "net/FreeSense-pkg-A"
        self.put(port / "Makefile")
        self.put(port / "pkg-plist", "@dir /usr/local/sbin\n@comment sbin/freesense-config\n")
        self.audit()
        self.put(port / "Makefile", "PLIST_FILES+= \\\n /usr/local/sbin/freesense-config\n")
        with self.assertRaisesRegex(ValueError, "sbin/freesense-config claimed by System"):
            self.audit()

    def test_relative_privilege_registration_refused(self):
        self.put(self.optional / ports.FRAMEWORK)
        port = self.optional / "net/FreeSense-pkg-A"
        self.put(port / "Makefile")
        self.put(port / "pkg-plist", "etc/inc/priv/a.priv.inc\n")
        with self.assertRaisesRegex(ValueError, "absolute /etc/inc/priv"):
            self.audit()
        self.put(port / "pkg-plist", "/etc/inc/priv/a.priv.inc\n")
        self.audit()
