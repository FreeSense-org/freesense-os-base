"""Use the routed Ports catalogue and select a nested alias in a firewall rule."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree
from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect
from browser_aliases import exercise_alias_authority


def exercise_port_aliases(page, origin, credential, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *cli, "backup")
    doc = ElementTree.fromstring(original)
    user = doc.find("./system/user")
    for privilege in ["page-firewall-aliases", "page-firewall-alias-edit", "api-firewall-rules-write"]:
        ElementTree.SubElement(user, "priv").text = privilege
    doc.find("./restapi/apitoken/scopes").text = "firewall.rules:write firewall.aliases:write"
    headers = {"Authorization": "Bearer " + credential}
    catalog = origin + "/api/v1/firewall/aliases/ports"
    rules = origin + "/api/v1/firewall/rules"

    def put(button, endpoint):
        with page.expect_response(lambda r: r.url == endpoint and r.request.method == "PUT") as sent:
            button.click()
        return sent.value

    try:
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(doc))
        page.goto(origin + "/firewall/aliases")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.get_by_role("link", name="Ports", exact=True).click()
        expect(page.get_by_role("heading", name="Port aliases", exact=True)).to_be_visible()
        chooser = page.get_by_role("combobox", name="Saved alias")
        save = page.get_by_role("button", name="Save alias", exact=True)
        member = page.get_by_label("Members 1", exact=True)
        page.get_by_label("Alias name", exact=True).fill("MediaPorts")
        member.fill("65000:60000")
        save.click()
        expect(page.get_by_role("group", name="Members", exact=True)).to_have_attribute("aria-invalid", "true")
        member.fill("8040:8050")
        assert put(save, catalog).status == 200
        expect(chooser).to_have_value("MediaPorts")
        chooser.select_option("")
        page.get_by_label("Alias name", exact=True).fill("BranchServices")
        member.fill("@MediaPorts")
        page.get_by_role("button", name="Add item", exact=True).click()
        page.get_by_label("Members 2", exact=True).fill("853")
        assert put(save, catalog).status == 200
        expect(chooser).to_have_value("BranchServices")
        if evidence:
            page.screenshot(path=evidence / "port-aliases.png")
        menu_link(page,"Firewall","Firewall").click()
        page.get_by_role("button", name="Add rule", exact=True).click()
        row = page.locator(".rule-editor").last
        row.get_by_label("Enabled", exact=True).check()
        row.get_by_role("combobox", name="Action", exact=True).select_option("pass")
        row.get_by_role("combobox", name="Protocol", exact=True).select_option("tcp/udp")
        row.get_by_label("Source", exact=True).fill("any")
        row.get_by_label("Destination", exact=True).fill("any")
        row.get_by_text("Port range", exact=True).click()
        row.get_by_role("combobox", name="Destination port alias").select_option("BranchServices")
        expect(row.get_by_label("Destination port", exact=True)).to_be_disabled()
        response = put(page.get_by_role("button", name="Save rules", exact=True), rules)
        assert response.status == 200, response.text()
        saved = page.request.get(rules, headers=headers).json()["data"]["rules"][-1]
        assert saved["destination_port_alias"] == "BranchServices" and "destination_port" not in saved
        latest = page.request.get(catalog, headers=headers).json()["data"]
        denied = page.request.put(catalog + "/remove", headers=headers | {"Origin":origin, "X-FreeSense-Command":"removePortAlias"},
                                  data={"revision":latest["revision"], "name":"BranchServices"})
        assert denied.status == 422, denied.text()
        invalid = page.request.put(catalog, headers=headers | {"Origin":origin, "X-FreeSense-Command":"savePortAlias"},
                                   data={"revision":latest["revision"], "name":"BranchServices", "members":["65536"]})
        assert invalid.status == 422
        assert page.request.get(catalog, headers=headers).json()["data"] == latest
        readonly = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        account = readonly.find("./system/user")
        for item in account.findall("priv"):
            account.remove(item)
        for privilege in ["page-system-restapi-keys", "api-firewall-rules-read"]:
            ElementTree.SubElement(account, "priv").text = privilege
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(readonly))
        page.goto(origin + "/firewall")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("cell", name="@BranchServices", exact=False)).to_be_visible()
        assert page.request.get(catalog, headers=headers).status == 403
        exercise_alias_authority(page, origin, credential, container, docker)
        print("Chromium validated nested port alias writes, protected deletion, port rule selectors and separate alias grants.")
    finally:
        docker("exec", "-i", container, *cli, "restore", input=original)
