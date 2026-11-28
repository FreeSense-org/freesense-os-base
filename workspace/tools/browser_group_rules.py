"""Exercise group rule editing, immutable group IDs, and role-scoped firewall views."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from xml.etree import ElementTree

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_group_rules(page, origin, credential, container, docker, evidence=None):
    config = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *config, "backup")
    groups = origin + "/api/v1/interfaces/groups"
    rules = origin + "/api/v1/firewall/rules"
    auth = {"Authorization": "Bearer " + credential}
    root_headers = auth | {"Origin": origin}
    group_grants = ["page-interfaces-groups", "page-interfaces-groups-edit"]
    guarded = []
    page.on("request", lambda r: guarded.append(r.url) if r.method == "PUT" and r.url.startswith(rules) else None)

    def grant(grants, scope):
        xml = ElementTree.fromstring(docker("exec", container, *config, "backup"))
        user = xml.find("./system/user")
        for item in user.findall("priv"):
            user.remove(item)
        for value in ["page-system-restapi-keys", *grants]:
            ElementTree.SubElement(user, "priv").text = value
        xml.find("./restapi/apitoken/scopes").text = scope
        wan = xml.find("./interfaces/wan")
        if wan.find("ipaddr") is None:
            ElementTree.SubElement(wan, "ipaddr").text = "dhcp"
            ElementTree.SubElement(wan, "upstream4").text = "dhcp"
        docker("exec", "-i", container, *config, "restore", input=ElementTree.tostring(xml))

    def login():
        page.goto(origin + "/firewall")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Firewall", exact=True)).to_be_visible()

    def data(url):
        res = page.request.get(url, headers=auth)
        assert res.status == 200, res.text()
        return res.json()["data"]

    def group(name, members, id_=None):
        draft = {"name": name, "description": "", "members": members, "revision": data(groups)["revision"]}
        if id_:
            draft["id"] = id_
        res = page.request.put(groups + ("/edit" if id_ else ""), headers=root_headers | {
            "X-FreeSense-Command": "editInterfaceGroup" if id_ else "createInterfaceGroup"}, data=draft)
        assert res.status == 200, res.text()
        return res.json()["data"]["id"]

    def submit(button, endpoint, status=200):
        with page.expect_response(lambda r: r.url == endpoint and r.request.method == "PUT") as res:
            button.click()
        assert res.value.status == status, res.value.text()
        if status == 200:
            expect(page.locator("#feedback")).to_have_text("Draft saved.")

    def set_file(rows):
        page.get_by_label("JSON rules file").set_input_files({"name": "groups.json", "mimeType": "application/json",
                                                             "buffer": json.dumps(rows).encode()})

    try:
        grant(group_grants, "interfaces.groups:write")
        target_id = group("Offices", ["wan", "lan"])
        target = "group:" + target_id
        empty = "group:" + group("Standby", [])
        grant(["api-firewall-rules-write"], "firewall.rules:write")
        login()
        filter_ = page.get_by_role("combobox", name="Filter by interface", exact=True)
        filter_.select_option(target)
        page.get_by_role("button", name="Add rule", exact=True).click()
        row = page.locator(".rule-editor:visible").last
        role = row.get_by_role("combobox", name="Interface", exact=True)
        expect(role).to_have_value(target)
        assert role.locator("option:checked").inner_text() == "Offices (group)"
        row.get_by_label("Enabled", exact=True).check()
        row.get_by_role("combobox", name="Action", exact=True).select_option("pass")
        row.get_by_label("Source", exact=True).fill("any")
        row.get_by_label("Destination", exact=True).fill("192.0.2.20")
        expect(row.get_by_text("Replies use the firewall routing table", exact=False)).to_be_visible()
        direction = row.get_by_role("combobox", name="Traffic direction", exact=True)
        direction.select_option("local-in")
        assert not row.get_by_role("combobox", name="Internal interface", exact=True).locator(f'option[value="{target}"]').count()
        direction.select_option("transit")
        assert role.input_value() == target
        submit(page.get_by_role("button", name="Save rules", exact=True), rules)
        draft = data(rules)
        assert draft["rules"][0]["interface"] == target and draft.get("dhcp_return") is None
        if evidence:
            page.screenshot(path=evidence / "group-firewall-rule.png", full_page=True)
        ordinary = {"enabled": True, "action": "block", "source": "any", "destination": "any", "interface": "lan"}
        second = ordinary | {"interface": empty}
        set_file(draft["rules"] + [second, ordinary])
        submit(page.get_by_role("button", name="Import JSON", exact=True), rules + "/import")
        stored = data(rules)["rules"]
        assert len(stored) == 3 and stored[1]["interface"] == empty
        unknown = ordinary | {"enabled": False, "interface": "group:" + "0" * 32}
        before = len(guarded)
        set_file([unknown])
        page.get_by_role("button", name="Import JSON", exact=True).click()
        expect(page.locator('[data-error-for="file"]')).to_contain_text("available interface group")
        assert len(guarded) == before
        save_headers = root_headers | {"X-FreeSense-Command": "saveRules"}
        assert page.request.put(rules, headers=save_headers, data={"revision": data(rules)["revision"], "rules": [unknown]}).status == 422
        grant(group_grants, "interfaces.groups:write")
        group("Campus", ["wan", "lan"], target_id)
        grant(["page-firewall-rules"], "firewall.rules:read")
        login()
        expect(page.get_by_role("cell", name="Campus (group)", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="Standby (group)", exact=True)).to_be_visible()
        assert data(rules)["rules"][0]["interface"] == target
        grant(["api-firewall-rules-lan-write"], "firewall.rules:write")
        login()
        allowed = data(rules)
        assert len(allowed["rules"]) == 1 and allowed["rules"][0]["id"] == stored[2]["id"]
        forms = data(origin + "/api/v1/forms")
        for result in [allowed, forms]:
            serialized = json.dumps(result)
            assert all(secret not in serialized for secret in [target_id, empty, "Campus", "Standby"])
        expect(filter_).to_be_visible()
        assert all("group" not in label for label in filter_.locator("option").all_text_contents())
        forged = page.request.put(rules, headers=save_headers, data={"revision": allowed["revision"],
                                                                    "rules": [ordinary | {"interface": target, "enabled": False}]})
        assert forged.status == 403
        assert page.request.get(groups, headers=auth).status == 403
        submit(page.get_by_role("button", name="Save rules", exact=True), rules)
        grant(["api-firewall-rules-write"], "firewall.rules:write")
        assert data(rules)["rules"][:2] == stored[:2]
        print("Grouped firewall editor and JSON import passed; ordinary/group rule labels survived rename and scoped writes preserved protected rows.")
    finally:
        docker("exec", "-i", container, *config, "restore", input=original)
