# SPDX-License-Identifier: Apache-2.0
"""Contracts for the read-only old-UI crawler: classifier, planning, extraction, TSV writing, and the request guard."""

import argparse
import csv
import http.server
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import old_ui_crawl as crawl

BASE = "https://192.0.2.1/"

PAGES = {
    "/index.php": """<html><head><title>Fixture</title></head><body>
<nav><a href="/system.php">System</a> <a href="/diag_reboot.php">Reboot</a> <a href="/index.php?logout">Logout</a>
<a href="/list.php?act=del&id=0">Del</a></nav></body></html>""",
    "/list.php": """<html><head><title>List</title></head><body>
<ul class="breadcrumb"><li>Firewall</li><li>List</li></ul>
<ul class="nav-tabs"><li class="active"><a href="/list.php">One</a></li><li><a href="/list.php?if=wan">Two</a></li>
<li><a href="#js">Js</a></li></ul>
<a href="/list_edit.php?id=0">edit</a> <a href="/list_edit.php?id=1">edit</a> <a href="/list_edit.php">Add</a>
<a href="/list.php?act=del&id=0">delete</a>
<table><thead><tr><th>Name</th><th>Action</th></tr></thead><tbody><tr><td>a</td><td><a title="Edit" href="/list_edit.php?id=0"></a></td></tr></tbody></table>
</body></html>""",
    "/system.php": """<html><head><title>System</title></head><body>
<form method="post" action="/save.php"><div class="panel"><div class="panel-heading"><h2 class="panel-title">General</h2></div>
<div class="form-group"><label for="mode">Mode</label><select name="mode" id="mode"><option value="a" selected>Alpha</option>
<option value="b">Beta</option></select><span class="help-block">Pick</span></div>
<div class="form-group" id="extra"><label for="extrafld">Extra</label><input name="extrafld" id="extrafld" style="display:none"></div>
<div class="form-group"><label><input type="checkbox" name="on" value="yes" checked> On</label></div>
<div class="form-group"><label for="pw">Pw</label><input type="password" name="pw" id="pw" value="topsecret"></div>
<textarea name="cert">PRIVATE-KEY-DATA</textarea>
<div class="collapse" id="more"><input name="hidden_in_collapse"></div>
<button type="submit" name="Submit">Save</button></div></form>
<script>document.getElementById('mode').addEventListener('change',function(){
document.getElementById('extrafld').style.display=this.value=='b'?'':'none';});
fetch('/post-probe',{method:'POST',body:'x'}).catch(function(){});</script></body></html>""",
}


class Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.seen.append(("GET", self.path))
        body = PAGES.get(self.path.split("?")[0], "<html><body>x</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.seen.append(("POST", self.path))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class ClassifierTest(unittest.TestCase):
    def test_denied(self):
        for url in ("index.php?logout", "x.php?act=del&id=1", "diag_reboot.php", "diag_halt.php", "diag_defaults.php",
                    "diag_edit.php", "diag_backup.php?download=1", "status_logs.php?clear=1", "a.php?restore",
                    "system_update.php?upgrade=1", "pkg_mgr_install.php", "a.php?reload=1", "a.php?kill=1",
                    "a.php?restart", "a.php?apply=1", "a.php?download", "a.php?reset", "diag_confbak.php?newver=3",
                    "https://evil.example/x.php"):
            self.assertFalse(crawl.classify_url(crawl.canonical_url(url, BASE), BASE)[0], url)

    def test_allowed(self):
        for url in ("system_advanced_admin.php", "firewall_rules.php?if=wan", "x_edit.php?act=edit&id=0",
                    "x.php?act=new", "pkg_mgr_installed.php", "diag_backup.php", "interfaces.php?if=opt1"):
            self.assertTrue(crawl.classify_url(crawl.canonical_url(url, BASE), BASE)[0], url)

    def test_safe_click(self):
        self.assertFalse(crawl.safe_click("button", "submit", "Save", "", "", BASE)[0])
        self.assertFalse(crawl.safe_click("button", "", "Open", "", "", BASE)[0])
        self.assertFalse(crawl.safe_click("a", "", "Delete", "#", "", BASE)[0])
        self.assertFalse(crawl.safe_click("a", "", "Rules", "x.php?act=del", "", BASE)[0])
        self.assertFalse(crawl.safe_click("a", "", "Tab", "#t", "this.form.submit()", BASE)[0])
        self.assertTrue(crawl.safe_click("a", "", "Advanced", "#adv", "", BASE)[0])

    def test_secret_fields(self):
        self.assertTrue(crawl.is_secret("input", "password", "x", ""))
        self.assertTrue(crawl.is_secret("textarea", "textarea", "cert", ""))
        self.assertTrue(crawl.is_secret("input", "text", "psk", ""))
        self.assertFalse(crawl.is_secret("input", "checkbox", "passthru", ""))
        self.assertFalse(crawl.is_secret("input", "text", "descr", ""))


class PlanTest(unittest.TestCase):
    def test_seed_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "a.tsv").write_text("path\tstatus\nsrc/usr/local/www/a.php\tmissing\nsrc/usr/local/www/b.php\tobsolete\n"
                                       "src/usr/local/www/classes/c.php\tported\nsrc/etc/rc\tmissing\n"
                                       "src/usr/local/www/a.php\tpartial\n", encoding="utf-8")
            (tmp / "m.tsv").write_text("target\n/d.php?x=1\n/e.php?if=<id>\n$helpurl (page specific)\n"
                                       "/index.php?logout (POST)\n/pkg_edit.php?xml=\n", encoding="utf-8")
            (tmp / "t.tsv").write_text("tab_target\nf.php\nd.php?x=1\n", encoding="utf-8")
            self.assertEqual(crawl.seed_paths(tmp / "a.tsv", tmp / "m.tsv", tmp / "t.tsv"),
                             ["a.php", "d.php?x=1", "index.php?logout", "f.php"])
            urls = crawl.plan_urls(BASE, audit=tmp / "a.tsv", menu=tmp / "m.tsv", tabs=tmp / "t.tsv")
            self.assertEqual(urls, [BASE + "a.php", BASE + "d.php?x=1", BASE + "f.php"])

    @unittest.skipUnless(crawl.AUDIT.is_file(), 'the planning notes are not part of a published checkout')
    def test_real_inventory_is_safe(self):
        urls = crawl.plan_urls(BASE)
        self.assertGreater(len(urls), 100)
        self.assertFalse([u for u in urls if "reboot" in u or "logout" in u])

    def test_slug_and_dedupe(self):
        self.assertEqual(crawl.slugify(BASE + "firewall_rules.php?if=wan"), "firewall_rules.php__if-wan")
        self.assertEqual(crawl.canonical_url("a.php?b=2&a=1#x", BASE), BASE + "a.php?a=1&b=2")
        self.assertLess(len(crawl.slugify(BASE + "x.php?" + "q" * 300)), 100)

    def test_discover(self):
        data = {"nav_links": ["/system.php", "/interfaces.php?if=opt1", "/diag_reboot.php", "#"],
                "tabs": [{"href": "list.php?if=wan"}],
                "content_links": [{"href": "list_edit.php?id=0", "text": "e"}, {"href": "list_edit.php?id=1", "text": "e"},
                                  {"href": "list_edit.php?if=wan", "text": "Add"}, {"href": "list.php?act=del&id=1", "text": "x"}]}
        found = crawl.discover(data, BASE + "list.php", BASE, leaf=False)
        self.assertIn(BASE + "firewall_rules.php?if=opt1", found)
        self.assertIn(BASE + "services_dhcp.php?if=opt1", found)
        self.assertIn(BASE + "list.php?if=wan", found)
        self.assertIn(BASE + "list_edit.php?id=0", found)
        self.assertNotIn(BASE + "list_edit.php?id=1", found)
        self.assertIn(BASE + "list_edit.php", found)
        self.assertFalse([u for u in found if "reboot" in u or "act=del" in u])
        self.assertNotIn(BASE + "list_edit.php?id=0", crawl.discover(data, BASE + "list.php", BASE, leaf=True))


class TsvTest(unittest.TestCase):
    def test_field_rows_and_outputs(self):
        data = {"url": BASE + "firewall_rules_edit.php?if=wan", "tabs": [], "breadcrumbs": ["Firewall", "Rules"],
                "controls": [{"tag": "select", "type": "select-one", "name": "proto", "label": "Protocol", "section": "Edit",
                              "help": "h", "default": "tcp", "options": [{"value": "tcp", "label": "TCP"},
                                                                          {"value": "Any", "label": "Any"}]},
                             {"tag": "input", "type": "password", "name": "pw", "label": "", "section": "", "help": "",
                              "default": "", "secret": True, "state": "set"}],
                "tables": [{"headers": ["A", "B"], "row_actions": [{"text": "", "title": "Edit"}]}],
                "dependencies": {"port": "visible (proto=tcp)"},
                "meta": {"slug": "s", "status": "ok", "screenshots": ["dark.png"]}}
        rows = crawl.field_rows(data)
        self.assertEqual(rows[0]["old_file"], "src/usr/local/www/firewall_rules_edit.php")
        self.assertEqual(rows[0]["tab"], "?if=wan")
        self.assertEqual(rows[0]["choices"], "tcp=TCP; Any")
        self.assertEqual(rows[1]["default"], "set")
        self.assertEqual(rows[2]["choices"], "columns: A; B")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "s").mkdir()
            (out / "s" / "data.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(crawl.write_outputs(out), (1, 3))
            with (out / "old-fields-rendered.tsv").open(encoding="utf-8", newline="") as stream:
                header = next(csv.reader(stream, delimiter="\t"))
            self.assertEqual(header, crawl.COLUMNS)
            self.assertIn("status", (out / "index.tsv").read_text(encoding="utf-8").splitlines()[0])


class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/"
        try:
            from playwright.sync_api import sync_playwright
            cls.engine = sync_playwright().start()
            channel = os.environ.get("FREESENSE_BROWSER_CHANNEL", "chrome" if os.name == "nt" else None)
            cls.browser = cls.engine.chromium.launch(channel=channel)
        except Exception as error:
            cls.server.shutdown()
            raise unittest.SkipTest(f"no browser: {error}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.engine.stop()
        cls.server.shutdown()

    def crawler(self, out):
        context = self.browser.new_context(viewport={"width": 1440, "height": 900})
        guard = crawl.RouteGuard(self.base)
        context.route("**/*", guard)
        page = context.new_page()
        args = argparse.Namespace(delay_min=0, delay_max=0)
        return crawl.Crawler(page, guard, self.base, out, args), guard, context

    def test_guard_blocks_post_and_capture(self):
        Handler.seen.clear()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            crawler, guard, context = self.crawler(out)
            data = crawler.capture(self.base + "system.php")
            context.close()
            self.assertNotIn("POST", [m for m, _ in Handler.seen])
            self.assertTrue(any(b.startswith("POST") for b in data["blocked_requests"]))
            by_name = {c["name"]: c for c in data["controls"]}
            self.assertEqual(by_name["pw"]["state"], "set")
            self.assertEqual(by_name["pw"]["default"], "")
            self.assertEqual(by_name["cert"]["state"], "set")
            self.assertEqual(by_name["mode"]["help"], "Pick")
            self.assertEqual([o["value"] for o in by_name["mode"]["options"]], ["a", "b"])
            self.assertEqual(data["panel_headings"], ["General"])
            self.assertEqual(data["dependencies"]["extrafld"], "visible (mode=b)")
            self.assertNotIn("topsecret", json.dumps(data))
            self.assertNotIn("PRIVATE-KEY-DATA", json.dumps(data))
            shots = data["meta"]["screenshots"]
            slug = data["meta"]["slug"]
            for name in ("dark.png", "light.png", "expanded.png"):
                self.assertIn(name, shots)
                self.assertGreater((out / slug / name).stat().st_size, 1000)
            self.assertTrue(any(s.startswith("option-") for s in shots))
            self.assertNotIn("/save.php", [p for _, p in Handler.seen])

    def test_tabs_and_denylist_not_followed(self):
        Handler.seen.clear()
        with tempfile.TemporaryDirectory() as tmp:
            crawler, guard, context = self.crawler(Path(tmp))
            data = crawler.capture(self.base + "list.php")
            context.close()
            self.assertEqual([t["label"] for t in data["tabs"]], ["One", "Two", "Js"])
            self.assertTrue(data["tabs"][0]["active"])
            found = crawl.discover(data, self.base + "list.php", self.base, leaf=False)
            self.assertIn(self.base + "list_edit.php?id=0", found)
            requested = [p for _, p in Handler.seen]
            self.assertFalse([p for p in requested if "act=del" in p or "logout" in p or "reboot" in p])

    def test_guard_aborts_denied_get(self):
        Handler.seen.clear()
        with tempfile.TemporaryDirectory() as tmp:
            crawler, guard, context = self.crawler(Path(tmp))
            crawler.page.goto(self.base + "index.php")
            with self.assertRaises(Exception):
                crawler.page.goto(self.base + "diag_reboot.php")
            context.close()
            self.assertNotIn("/diag_reboot.php", [p for _, p in Handler.seen])


if __name__ == "__main__":
    unittest.main()
