"""Guard the --affected test selection: changed files pick the tests that name them, shared files pick their group's set."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import affected


class AffectedSelection(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repositories" / "refactor2.0"
        files = {
            "freesense/src/usr/local/FreeSense/lib/Vpn/OpenVpnConfig.php": "<?php\n",
            "freesense/src/usr/local/www/assets/vpn/openvpn-servers.js": "",
            "freesense/src/usr/local/www/assets/shared/ui.js": "",
            "freesense/tests/OpenVpnConfigTest.php": "<?php new OpenVpnConfig();\n",
            "freesense/tests/DhcpTest.php": "<?php\n",
            "freesense/tests/appliance/OpenVpnTunnelTest.php": "<?php // OpenVpnConfig\n",
            "freesense/tests/web/test_openvpn.py": "PAGE = '/vpn/openvpn-servers'\n",
            "freesense/tests/web/test_sign_in.py": "",
            "freesense/tests/web/test_dhcp.py": "",
            "freesense/tests/web/test_language.py": "CATALOGUE = 'assets/locales/de.js'\n",
            "freesense/tests/web/test_navigation.py": "",
            "freesense/tests/VpnRoutesTest.php": "<?php new Vpn\\VpnRoutes();\n",
            "freesense/src/usr/local/FreeSense/lib/Http/Vpn/VpnRoutes.php": "<?php\n",
            "freesense/src/usr/local/www/assets/locales/de.js": "",
            "freesense-system-ports/security/FreeSense-system/pkg-plist": "",
            "tools/tests/test_check.py": "",
        }
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.root / "tools/check-affected.json").write_text(json.dumps({
            "groups": [
                {"name": "shared UI", "files": ["freesense/src/usr/local/www/assets/shared/*"],
                 "tests": ["freesense/tests/web/test_sign_in.py"]},
                {"name": "feature routes", "files": ["freesense/src/usr/local/FreeSense/lib/Http/*/*Routes.php"], "names": True,
                 "tests": ["freesense/tests/web/test_navigation.py"]},
                {"name": "translations", "files": ["freesense/src/usr/local/www/assets/locales/*"], "mentions": "locales"},
                {"name": "System port", "files": ["freesense-system-ports/security/FreeSense-system/pkg-plist"],
                 "mentions": "security/FreeSense-system"},
            ],
            "always": ["freesense/tests/DhcpTest.php"],
        }))
        old = time.time() - 3600
        for path in self.root.rglob("*"):
            os.utime(path, (old, old))
        self.since = datetime.now() - timedelta(minutes=5)

    def touch(self, name):
        os.utime(self.root / name, None)

    def test_feature_files_pick_their_tests_only(self):
        self.touch("freesense/src/usr/local/FreeSense/lib/Vpn/OpenVpnConfig.php")
        self.touch("freesense/src/usr/local/www/assets/vpn/openvpn-servers.js")
        selection = affected.select(self.root, affected.changed_files(self.root, self.since))
        self.assertEqual(set(selection.php_tests), {"freesense/tests/OpenVpnConfigTest.php", "freesense/tests/DhcpTest.php"})
        self.assertEqual(set(selection.web_tests), {"freesense/tests/web/test_openvpn.py"})
        self.assertEqual(set(selection.native_tests), {"freesense/tests/appliance/OpenVpnTunnelTest.php"})
        self.assertEqual(selection.lint_php, ["freesense/src/usr/local/FreeSense/lib/Vpn/OpenVpnConfig.php"])

    def test_shared_file_picks_its_group_set(self):
        self.touch("freesense/src/usr/local/www/assets/shared/ui.js")
        selection = affected.select(self.root, affected.changed_files(self.root, self.since))
        self.assertEqual(set(selection.web_tests), {"freesense/tests/web/test_sign_in.py"})
        self.assertEqual(selection.core_hits, ["freesense/src/usr/local/www/assets/shared/ui.js"])

    def selected(self, *names):
        for name in names:
            self.touch(name)
        return affected.select(self.root, affected.changed_files(self.root, self.since))

    def test_feature_routes_keep_their_own_tests(self):
        selection = self.selected("freesense/src/usr/local/FreeSense/lib/Http/Vpn/VpnRoutes.php")
        self.assertEqual(set(selection.php_tests), {"freesense/tests/VpnRoutesTest.php", "freesense/tests/DhcpTest.php"})
        self.assertEqual(set(selection.web_tests), {"freesense/tests/web/test_navigation.py"})

    def test_translations_and_packaging_pick_only_what_reads_them(self):
        selection = self.selected("freesense/src/usr/local/www/assets/locales/de.js",
                                  "freesense-system-ports/security/FreeSense-system/pkg-plist")
        self.assertEqual(set(selection.web_tests), {"freesense/tests/web/test_language.py"})
        self.assertEqual(set(selection.php_tests), {"freesense/tests/DhcpTest.php"})

    def test_repository_map_names_existing_tests_and_files(self):
        root = Path(__file__).resolve().parents[2]
        rules = affected.load_map(root)
        known = {path.relative_to(root).as_posix() for group in affected.test_files(root).values() for path in group}
        listed = [*rules["always"], *(test for group in rules["groups"] for test in group.get("tests", []))]
        self.assertEqual([], sorted(set(listed) - known))
        files = [pattern for group in rules["groups"] for pattern in group["files"]]
        self.assertEqual([], [pattern for pattern in files if not list(root.glob(pattern))])

    def test_nothing_changed_selects_nothing(self):
        selection = affected.select(self.root, affected.changed_files(self.root, self.since))
        self.assertTrue(selection.empty())

    def test_stamp_round_trip_and_spans(self):
        started = datetime(2026, 11, 27, 12, 0, 0)
        affected.write_stamp(self.root, started)
        self.assertEqual(affected.parse_since(self.root, None), started)
        self.assertLess(affected.parse_since(self.root, "6h"), datetime.now() - timedelta(hours=5))


if __name__ == "__main__":
    unittest.main()
