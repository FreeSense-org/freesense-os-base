"""Exercise the generated-page and redirect contracts shared by site checks."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from site_links import check_site, handler_urls, redirect_sources


class SiteLinksTest(unittest.TestCase):
    def test_generated_pages_and_redirects(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "dist"
            (site / "guide").mkdir(parents=True)
            (site / "guide/index.html").write_text("")
            redirects = Path(tmp) / "_redirects"
            redirects.write_text("# routes\n /old/* /guide/ 301\n/archive/ /guide/ 301\n")
            (site / "index.html").write_text(
                '<a href="/guide/?q=1">x</a><a href="/old/1">y</a><a href="//example.com">z</a>'
                "<A HREF='/archive#section'>w</A><!-- <img src='/noise'> -->")
            self.assertEqual(check_site(site, redirect_sources(redirects)), (3, set()))
            (site / "index.html").write_text('<img src="/missing"><img src="/%2e%2e/outside">')
            self.assertEqual(check_site(site, [])[1], {"index.html: /missing", "index.html: /%2e%2e/outside"})

    def test_empty_output_is_not_verified(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "No HTML"):
            check_site(Path(tmp), [])

    def test_only_concrete_handler_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("index.js", "stable.json.js", "_middleware.js", "[user].ts"):
                (root / name).touch()
            self.assertEqual(handler_urls(root), {"/", "/stable.json"})
