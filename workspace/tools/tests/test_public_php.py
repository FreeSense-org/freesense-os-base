"""Protect the public document root, including package claims before staging."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import port_ownership
import public_php


class PublicPhpTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.public = self.root / "usr/local/www"
        self.public.mkdir(parents=True)

    def test_static_assets_and_private_controller(self):
        private = self.root / "usr/local/FreeSense/web"
        private.mkdir(parents=True)
        (private / "index.php").write_bytes(b"<?php return;")
        (self.public / "logo.svg").write_bytes(b'<?xml version="1.0"?><svg/>')
        styles = self.public / "assets/shell"
        styles.mkdir(parents=True)
        (styles / "app.css").write_bytes(b"body{color:black}")
        public_php.check_stage(self.root)

    def test_disguised_or_embedded_code(self):
        for name, data in [
            ("index.PhP8.png", b""),
            ("include.inc", b""),
            ("image.phtml", b""),
            ("cache", b" " * 65535 + b"<?php echo 1;"),
            ("help.html", b"<aside>short form</aside><?= 1 ?>"),
            ("worker", b"#!/usr/bin/env php\necho 1;"),
        ]:
            with self.subTest(name=name):
                asset = self.public / name
                asset.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "public document root"):
                    public_php.check_stage(self.root)
                asset.unlink()

    def test_unsafe_optional_plist(self):
        port = self.root / "optional"
        port.mkdir()
        (port / "Makefile").write_text("PLIST_FILES= www/private/entry.php\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "entry.php"):
            port_ownership.file_claims(port, optional=True)

    def test_one_browser_request_client_for_system_and_packages(self):
        assets = self.public / "assets/shared"
        assets.mkdir(parents=True)
        (assets / "commands.js").write_text("export const send = url => fetch(url);")
        public_php.check_stage(self.root)
        feature = assets / "settings.js"
        for source in ["fetch('/api/v1/settings')", "new XMLHttpRequest()", "navigator.sendBeacon('/save')"]:
            with self.subTest(source=source):
                feature.write_text(source)
                with self.assertRaisesRegex(ValueError, "shared command client"):
                    public_php.check_stage(self.root)
        provider = self.root / "files/private"
        provider.mkdir(parents=True)
        (provider / "widget.mjs").write_text("window.fetch('/settings')")
        with self.assertRaisesRegex(ValueError, "shared command client"):
            public_php.check_package(self.root)

    def test_browser_asset_ownership(self):
        for relative in ["view.js", "assets/page.mjs", "assets/site.css"]:
            asset = self.public / relative
            asset.parent.mkdir(exist_ok=True)
            asset.write_text("")
            with self.assertRaisesRegex(ValueError, "assets/<area>"):
                public_php.check_stage(self.root)
            asset.unlink()
        extension = self.root / "files/usr/local/www/assets"
        feature = extension / "firewall"
        feature.mkdir(parents=True)
        script = feature / "rules.js"
        script.write_text("")
        with self.assertRaisesRegex(ValueError, "assets/packages"):
            public_php.check_package(self.root)
        (self.root / "Makefile").write_text("PLIST_FILES= www/assets/firewall/rules.js\n")
        with self.assertRaisesRegex(ValueError, "assets/packages"):
            port_ownership.file_claims(self.root, optional=True)
        owner = extension / "packages/test-package"
        owner.mkdir(parents=True)
        script.rename(owner / "rules.js")
        public_php.check_package(self.root)
        public_php.check_stage(self.root / "files")
        with self.assertRaisesRegex(ValueError, "System assets"):
            public_php.check_stage(self.root / "files", optional=False)

    def test_public_link(self):
        try:
            (self.public / "asset").symlink_to(self.root / "private.php")
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaisesRegex(ValueError, "link"):
            public_php.check_stage(self.root)

    def test_native_package_form_submission(self):
        files = self.root / "files"
        files.mkdir()
        for source in ['<form method=POST>', '<a usepost href="/action">',
                       '<button formaction="/action">', 'form.submit()', '<?php echo $_POST["a"];']:
            with self.subTest(source=source):
                (files / "page.inc").write_text(source, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "management router"):
                    public_php.check_package(self.root)
