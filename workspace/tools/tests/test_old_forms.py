"""Contracts for the old-form extractor, the new-field dump and the parity report, on small fixture pages."""

import csv
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import new_fields
import old_forms
import parity

FORM_PAGE = """<?php
##|+PRIV
##|*IDENT=page-demo-settings
##|-PRIV
$pconfig['mode'] = config_get_path('demo/mode');
$pconfig['limit'] = '10';
$pconfig['on'] = config_path_enabled('demo', 'on');
$pgtitle = array(gettext("Demo"), gettext("Settings"));
fs_tabs('demo', 'demo.php');
$form = new Form;
// $x = new Form_Input('commented', 'No', 'text', '');
$section = new Form_Section(gettext('General'));
$section->addInput(new Form_Checkbox('on', 'Enable', 'Turn it on', $pconfig['on']));
$section->addInput(new Form_Select('mode', '*Mode', $pconfig['mode'], array('a' => gettext('Alpha'), 'b' => 'Beta')))
	->setHelp('Pick %1$s one.', '<br />');
$section->addInput(new Form_Input('limit', 'Limit', 'number', $pconfig['limit']));
$group = new Form_Group('Servers');
$group->addClass('repeatable');
$group->add(new Form_Input('server', 'Server', 'text', ''))->setHelp('Host');
$section->add($group);
$form->add($section);
$section = new Form_Section('Second');
foreach ($rows as $row) {
	$section->addInput(new Form_Input('item' . $row, 'Item', 'text', ''));
}
$form->add($section);
?>
<script>
events.push(function() {
	$('#on').click(function () { hideInput('limit', !$('#on').prop("checked")); });
	disableInput('mode', !$('#on').prop("checked"));
});
</script>
"""

TABLE_PAGE = """<?php
##|*IDENT=page-demo-list
?>
<table><thead><tr><th><?=gettext('Name')?></th><th>Address</th><th>Actions</th></tr></thead>
<tbody><tr><td><a class="fa-solid fa-trash-can" title="Delete" href="demo_list.php?act=del"></a></td></tr></tbody></table>
<input type="text" name="filter" value="x">
"""


class OldFormsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        www = self.root / "src/usr/local/www"
        (www / "includes/tabs").mkdir(parents=True)
        (www / "demo.php").write_text(FORM_PAGE, encoding="utf-8")
        (www / "demo_list.php").write_text(TABLE_PAGE, encoding="utf-8")
        (www / "includes/tabs/demo.inc").write_text(
            "<?php\n$fs_tab_groups['demo'] = [[gettext('Settings'), 'demo.php'], [gettext('List'), 'demo_list.php']];\n", encoding="utf-8")
        self.audit = self.root / "audit.tsv"
        self.audit.write_text("path\tarea\tstatus\nsrc/usr/local/www/demo.php\tSystem\tported\nsrc/usr/local/www/demo_list.php\tSystem\tmissing\n",
                              encoding="utf-8")
        self.out = self.root / "old.tsv"
        self.dynamic = self.root / "dynamic.tsv"

    def rows(self):
        old_forms.run(self.root / "src", self.audit, self.out, self.dynamic)
        with self.out.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream, delimiter="\t"))

    def field(self, rows, name):
        return next(r for r in rows if r["field"] == name and r["old_file"].endswith("demo.php"))

    def test_fields_sections_and_tab(self):
        rows = self.rows()
        mode = self.field(rows, "mode")
        self.assertEqual((mode["section"], mode["tab"], mode["type"], mode["label"]), ("General", "Settings", "select", "Mode"))
        self.assertEqual(mode["choices"], "a=Alpha; b=Beta")
        self.assertEqual(mode["help"], "Pick one.")
        self.assertEqual(mode["config_path"], "demo/mode")
        self.assertEqual(mode["grant"], "page-demo-settings")
        self.assertEqual(mode["title"], "Demo / Settings")
        self.assertNotIn("commented", [r["field"] for r in rows])

    def test_defaults_checkbox_and_config_path(self):
        rows = self.rows()
        self.assertEqual(self.field(rows, "limit")["default"], "10")
        self.assertEqual(self.field(rows, "limit")["type"], "number")
        on = self.field(rows, "on")
        self.assertEqual((on["type"], on["config_path"], on["choices"]), ("checkbox", "demo/on", "text: Turn it on"))

    def test_script_conditions(self):
        rows = self.rows()
        self.assertIn("hidden when !#on checked", self.field(rows, "limit")["depends_on"])
        self.assertIn("disabled when !#on checked", self.field(rows, "mode")["depends_on"])

    def test_groups_and_loops_repeat(self):
        rows = self.rows()
        server = self.field(rows, "server")
        self.assertEqual((server["repeat"], server["help"], server["label"]), ("yes", "Host", "Server"))
        self.assertEqual(self.field(rows, "item{$row}")["repeat"], "yes")
        with self.dynamic.open(encoding="utf-8", newline="") as stream:
            reasons = dict(csv.reader(stream, delimiter="\t"))
        self.assertIn("field name item{$row}", reasons["src/usr/local/www/demo.php"])

    def test_tabs_row(self):
        tabs = next(r for r in self.rows() if r["type"] == "tabs")
        self.assertEqual(tabs["choices"], "Settings=demo.php; List=demo_list.php")

    def test_hand_built_page_gets_table_row(self):
        rows = [r for r in self.rows() if r["old_file"].endswith("demo_list.php")]
        table = next(r for r in rows if r["type"] == "table")
        self.assertEqual(table["choices"], "columns: Name; Address; Actions")
        self.assertIn("Delete|demo_list.php?act=del", table["help"])
        self.assertTrue(any(r["field"] == "filter" and r["section"] == "(html)" for r in rows))

    def test_decode_and_masking(self):
        self.assertEqual(old_forms.decode("gettext('A') . ' b' . $x"), "A b{$x}")
        self.assertNotIn("http", old_forms.mask_comments("<?php // http://x\n$a = 1; ?>"))
        self.assertIn("b//c", old_forms.mask_comments("<?php $a = 'b//c'; ?>"))


NEW_DUMP = [{
    "class": "DemoForm", "case": "Save", "value": "saveDemo", "path": "/api/v1/demo", "permission": "EditDnsHost",
    "schema": {"fields": {
        "revision": {"type": "revision", "required": True},
        "mode": {"type": "enum", "required": True, "label": "Mode", "choices": ["a", "c"]},
        "servers": {"type": "list", "label": "Servers",
                    "items": {"type": "record", "fields": {"address": {"type": "string", "label": "Address"}}}},
    }},
}]


class NewFieldsTest(unittest.TestCase):
    def test_rows_flatten_and_skip_revision(self):
        src = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (src / "usr/local/FreeSense/lib/Auth").mkdir(parents=True)
        (src / "usr/local/FreeSense/lib/Auth/ApiPermission.php").write_text(
            "<?php\n$pageGrant = match ($this) {\n self::EditDnsHost, self::DnsHostEditor => 'page-demo-settings',\n};\n", encoding="utf-8")
        rows = new_fields.rows_from(NEW_DUMP, src)
        self.assertEqual([r["field"] for r in rows], ["mode", "servers", "servers[].address"])
        self.assertEqual(rows[0]["grant"], "page-demo-settings")
        self.assertEqual(rows[0]["choices"], "a; c")
        self.assertEqual(rows[0]["page"], "/demo")


class ParityTest(unittest.TestCase):
    def old(self, field, label="", type_="text", choices="", config="", depends=""):
        row = dict.fromkeys(old_forms.COLUMNS, "")
        row.update(field=field, label=label, type=type_, choices=choices, config_path=config, depends_on=depends)
        return row

    def new(self, field, label="", type_="string", choices="", conditions=""):
        row = dict.fromkeys(new_fields.COLUMNS, "")
        row.update(field=field, label=label, type=type_, choices=choices, conditions=conditions)
        return row

    def test_matching_and_differences(self):
        old = [self.old("mode", "Mode", "select", "a=Alpha; b=Beta; c=Gamma"), self.old("gone", "Gone"),
               self.old("dns1", "DNS", config="system/dnsserver"), self.old("renamed", "Same label"),
               self.old("shown", "Shown", depends="pconfig:shown; hidden when !#x checked")]
        new = [self.new("mode", "Mode", "enum", "a; c; d"), self.new("dnsserver"), self.new("other", "Same label"),
               self.new("shown", "Shown")]
        audit = {"missing": "gone dropped per D9-1"}
        result = parity.compare(old, new, audit, {})
        self.assertEqual([m[0]["field"] for m in result["missing"]], ["gone"])
        self.assertEqual(result["missing"][0][1:], ("accepted", "D9-1"))
        self.assertEqual(result["options"][0][1:], (["b"], ["d"]))
        self.assertEqual(len(result["matched"]), 4)
        self.assertEqual([r["field"] for r, _ in result["conditions"]], ["shown"])

    def test_doubt_index_and_report(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "planning/inventory").mkdir(parents=True)
        (root / "planning/webui-doubts.md").write_text("| D1a-1 | Page | `legacy` drop | what | why | p | r |\n", encoding="utf-8")
        self.assertEqual(parity.doubt_index(root / "planning/webui-doubts.md"), {"legacy": "D1a-1"})
        tables = (
            ("old-fields", old_forms.COLUMNS,
             [["src/usr/local/www/a.php", "", "S", "legacy", "L", "text", "", "", "", "", "", "", "page-a", ""],
              ["src/usr/local/www/a.php", "", "S", "kept", "K", "text", "", "", "", "", "", "", "page-a", ""]]),
            ("new-fields", new_fields.COLUMNS, [["/a", "A.Save", "", "kept", "K", "string", "", "", "", "kept", "P", "page-a", "yes"]]),
            ("page-audit", ["path", "area", "status", "menu_title"], [["src/usr/local/www/a.php", "System", "partial", "A"]]),
        )
        for name, columns, rows in tables:
            with (root / f"planning/inventory/{name}.tsv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
                writer.writerow(columns)
                writer.writerows(rows)
        self.assertEqual(parity.run(root)["pages"], 1)
        text = (root / "planning/page-parity/system.md").read_text(encoding="utf-8")
        self.assertIn("accepted drop D1a-1", text)
        self.assertIn("matched 1, missing 1", text)


if __name__ == "__main__":
    unittest.main()
