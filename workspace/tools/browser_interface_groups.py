"""Check the historical group list and editor grants against the actual management router."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_interface_groups(page, origin, credential, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *cli, "backup")
    base = origin + "/api/v1/interfaces/groups"
    auth = {"Authorization": "Bearer " + credential}
    grants = ["page-interfaces-groups", "page-interfaces-groups-edit"]
    sent = []
    page.on("request", lambda r: sent.append(r.method) if r.url.startswith(base) and r.method != "GET" else None)

    def config(granted, scope="interfaces.groups:write", rule=None):
        doc = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        user = doc.find("./system/user")
        for node in user.findall("priv"):
            user.remove(node)
        for value in ["page-system-restapi-keys", *granted]:
            ElementTree.SubElement(user, "priv").text = value
        doc.find("./restapi/apitoken/scopes").text = scope
        interfaces = doc.find("interfaces")
        if interfaces.find("opt1") is None:
            interfaces.append(ElementTree.fromstring("<opt1><enable/><if>em2</if><descr>Guest</descr></opt1>"))
            interfaces.append(ElementTree.fromstring("<opt2><enable/><if>ipsec1</if></opt2>"))
        if rule is not None:
            firewall = doc.find("firewall")
            if firewall is None:
                firewall = ElementTree.SubElement(doc, "firewall")
            if rule:
                ElementTree.SubElement(firewall, "rule", {"interface": "group:" + rule, "action": "block"})
            else:
                for used in firewall.findall("rule"):
                    firewall.remove(used)
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(doc))

    def login(path):
        page.goto(origin + path)
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()

    def send(button, suffix, status=200):
        with page.expect_response(lambda r: r.url == base + suffix and r.request.method == "PUT") as response:
            button.click()
        assert response.value.status == status, (suffix, response.value.status, response.value.text())
        return response.value.json()

    def get(suffix=""):
        return page.request.get(base + suffix, headers=auth)

    def put(suffix, operation, data):
        return page.request.put(base + suffix, headers=auth | {"Origin": origin, "X-FreeSense-Command": operation}, data=data)

    try:
        config(grants)
        login("/overview")
        menu_link(page,"Interfaces","Interface Groups").click()
        expect(page.get_by_text("No interface groups yet.")).to_be_visible()
        page.get_by_role("link", name="Add group", exact=True).click()
        name = page.get_by_label("Group Name", exact=True)
        description = page.get_by_label("Group Description", exact=True)
        save = page.get_by_role("button", name="Save", exact=True)
        for bad in ["1lab", "bad-name", "PASS", "FS_example"]:
            name.fill(bad)
            save.click()
            expect(page.locator('[data-error-for="name"]')).not_to_be_empty()
        assert not sent
        name.fill("Office")
        description.fill("Floor 1 & guest")
        row = page.get_by_role("group", name="Group Members", exact=True)
        for role in ["opt1", "lan"]:
            row.get_by_role("button", name="Add item").click()
            choice = row.get_by_role("combobox").last
            assert choice.locator("option").all_text_contents() == [
                "Choose an assigned interface", "LAN (em0)", "WAN (em1)", "Guest (OPT1, em2)"]
            choice.select_option(role)
        if evidence:
            page.screenshot(path=evidence / "group-editor.png", full_page=True)
        receipt = send(save, "")["data"]
        id_ = receipt["id"]
        assert set(receipt) == {"revision", "id"}
        expect(page.get_by_role("cell", name="Guest (OPT1, em2), LAN (em0)", exact=True)).to_be_visible()
        page.get_by_role("link", name="Add group").click()
        name.fill("Reserve")
        unused = send(save, "")["data"]["id"]
        expect(page.get_by_role("cell", name="Reserve", exact=True)).to_be_visible()
        search = page.get_by_label("Search interface groups", exact=True)
        search.fill("floor")
        expect(page.get_by_role("cell", name="Reserve", exact=True)).to_be_hidden()
        search.fill("")
        expect(page.get_by_role("cell", name="Reserve", exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / "group-list.png", full_page=True)
        page.get_by_role("link", name="Edit Office", exact=True).click()
        name.fill("Guest")
        send(save, "/edit", 422)
        expect(page.locator('[data-error-for="name"]')).to_contain_text("already uses")
        name.fill("Floor")
        description.fill("Updated description")
        stale = get().json()["data"]["revision"]
        direct = put("/edit", "editInterfaceGroup", {"revision": stale, "id": unused, "name": "Reserve",
                                                    "description": "Changed elsewhere", "members": []})
        assert direct.status == 200
        send(save, "/edit", 409)
        expect(name).to_have_value("Floor")
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(name).to_have_value("Office")
        name.fill("Floor")
        assert send(save, "/edit")["data"]["id"] == id_
        expect(page.get_by_role("cell", name="Floor", exact=True)).to_be_visible()
        config([grants[1]])
        login("/interfaces/groups/edit/" + id_)
        expect(name).to_have_value("Floor")
        assert get().status == 403 and get("/edit/" + unused).status == 200
        editor = get("/edit/" + id_).json()["data"]
        assert set(editor) == {"revision", "availableRoles", "roleLabels", "group"} and editor["group"]["id"] == id_
        assert get("/edit/" + "0" * 32).status == 404
        assert put("/remove", "removeInterfaceGroup", {"revision": editor["revision"], "id": unused}).status == 403
        expect(page.get_by_role("link", name="Back to Interface Groups")).to_have_count(0)
        config([grants[0]], "interfaces.groups:read")
        login("/interfaces/groups")
        expect(page.get_by_role("cell", name="Floor", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Delete Floor", exact=True)).to_have_count(0)
        assert get("/edit/" + id_).status == 403
        assert put("/remove", "removeInterfaceGroup", {"revision": editor["revision"], "id": id_}).status == 403
        config([grants[0]], rule=id_)
        login("/interfaces/groups")
        delete = page.get_by_role("button", name="Delete Floor", exact=True)
        page.once("dialog", lambda dialog: dialog.accept())
        send(delete, "/remove", 409)
        expect(page.get_by_text("A saved firewall rule references this interface group.")).to_be_visible()
        assert get("/edit").status == 403
        assert put("", "createInterfaceGroup", {}).status == 403
        config([grants[0]], rule="")
        page.once("dialog", lambda dialog: dialog.dismiss())
        count = len(sent)
        delete.click()
        assert len(sent) == count
        page.once("dialog", lambda dialog: dialog.accept())
        send(delete, "/remove")
        expect(page.get_by_role("cell", name="Floor", exact=True)).to_have_count(0)
        config(grants)
        current = get().json()["data"]["revision"]
        valid = {"revision": current, "name": "Example", "description": "", "members": []}
        for changes, field in [({"name": "bad2"}, "name"), ({"name": "bogons"}, "name"), ({"description": "é" * 128}, "description"),
                               ({"members": ["opt2"]}, "members"), ({"members": ["lan", "lan"]}, "members")]:
            response = put("", "createInterfaceGroup", valid | changes)
            assert response.status == 422 and field in response.json()["error"]["fields"], response.text()
        assert put("", "createInterfaceGroup", valid | {"device": "em9"}).status == 422
        assert page.request.post(base, headers=auth, data=valid).status == 405
        # The portable router cannot perform FreeBSD ioctls. Use the native pending receipt shape
        # after a genuine committed save to exercise the operator's recovery instructions.
        login("/interfaces/groups/edit")
        name.fill("DeferredLab")
        def mark_native_pending(route):
            response = route.fetch()
            assert response.status == 200
            payload = response.json()
            payload["data"]["membership"] = "pending"
            route.fulfill(response=response, json=payload)
        page.route(base, mark_native_pending, times=1)
        send(save, "")
        expect(page.locator("#feedback")).to_contain_text("freesense-ifgroups status")
        expect(page.locator("#feedback")).to_contain_text("freesense-ifgroups reconcile")
        config(["page-firewall-rules"], "firewall.rules:read")
        assert get().status == 403 and get("/edit/" + unused).status == 403
        assert set(sent) == {"PUT"}
        print("Interface Groups passed routed add, change, delete, in-use refusal, name/member validation and independent list/editor access.")
    finally:
        docker("exec", "-i", container, *cli, "restore", input=original)
