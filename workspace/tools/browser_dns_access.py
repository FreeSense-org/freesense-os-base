"""Exercise DNS resolver access lists and their independent automatic-mode grant."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_dns_access(page, origin, key, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    backup = docker("exec", container, *cli, "backup")
    path = "/services/dns/access-lists"
    endpoint = origin + "/api/v1" + path
    advanced = "/services/dns/advanced"
    acl_grant, advanced_grant = "page-services-dnsresolver-acls", "page-services-dnsresolver-advanced"
    auth = {"Authorization":"Bearer " + key}

    def configure(grants, scope):
        doc = ET.fromstring(docker("exec", container, *cli, "backup"))
        user = doc.find("./system/user")
        for priv in user.findall("priv"):
            user.remove(priv)
        for grant in ["page-system-restapi-keys", *grants]:
            ET.SubElement(user,"priv").text = grant
        doc.find("./restapi/apitoken/scopes").text = scope
        docker("exec", "-i", container, *cli, "restore", input=ET.tostring(doc))

    def login(target):
        page.goto(origin + target)
        fill_api_key(page, key)
        page.get_by_role("button",name="Sign in",exact=True).click()

    def submit(url, button):
        with page.expect_response(lambda r:r.url == url and r.request.method == "PUT") as response:
            button.click()
        return response.value

    def write(suffix, command, data):
        return page.request.put(endpoint + suffix,headers=auth | {"Origin":origin,"X-FreeSense-Command":command},data=data)

    try:
        configure([acl_grant],"services.dns:write")
        login("/overview")
        page.get_by_role("link",name="Add access list",exact=True).click()
        name, save = page.get_by_label("Access List name",exact=True),page.get_by_role("button",name="Save",exact=True)
        name.fill("Office and workshop")
        page.get_by_role("combobox",name="Action",exact=True).select_option("allow_snoop")
        page.get_by_label("Description",exact=True).first.fill("Local observation")
        networks = page.get_by_label("Network / prefix",exact=False)
        networks.first.fill("192.0.2.1/24")
        sent = []
        page.on("request",lambda r:sent.append(r.method) if r.method in ("PUT","POST") and r.url.startswith(endpoint) else None)
        save.click()
        expect(page.locator('[data-error-for="networks"]')).not_to_be_empty()
        assert not sent
        networks.first.fill("192.0.2.0/24")
        page.get_by_role("button",name="Add item",exact=True).click()
        networks.nth(1).fill("2001:db8::1")
        stored = submit(endpoint,save)
        assert stored.status == 200,stored.text()
        first = stored.json()["data"]
        assert set(first) == {"id","revision"}
        expect(page.get_by_role("cell",name="Office and workshop",exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / "access-lists.png",full_page=True)
        url = page.get_by_role("link",name="Edit Office and workshop",exact=True)
        url.click()
        expect(networks.nth(1)).to_have_value("2001:db8::1/128")
        page.get_by_role("combobox",name="Action",exact=True).select_option("refuse_non_local")
        if evidence:
            page.screenshot(path=evidence / "access-list-edit.png",full_page=True)
        result = submit(endpoint+"/edit",save)
        assert result.status == 200,result.text()
        rev = result.json()["data"]["revision"]
        assert write("/remove","removeDnsAccessList",first).status == 409
        duplicate = write("","createDnsAccessList",{"revision":rev,"name":"Duplicate","action":"deny","description":"",
            "networks":[{"network":"2001:0db8::1","description":""}]})
        assert duplicate.status == 422 and "networks" in duplicate.json()["error"]["fields"]
        assert page.request.get(origin+"/api/v1"+advanced,headers=auth).status == 403
        configure([advanced_grant],"services.dns:write")
        login(advanced)
        auto = page.get_by_label("Disable auto-added access control",exact=True)
        expect(auto).to_be_visible()
        auto.check()
        response = submit(origin+"/api/v1"+advanced,page.get_by_role("button",name="Save advanced settings"))
        assert response.status == 200 and response.json()["data"]["disable_auto_added_access_control"] is True,response.text()
        assert page.request.get(endpoint,headers=auth).status == 403
        configure([acl_grant],"services.dns:read")
        login(path)
        expect(page.get_by_role("cell",name="Refuse Nonlocal",exact=True)).to_be_visible()
        page.get_by_role("link",name="View Office and workshop",exact=True).click()
        expect(page.get_by_role("cell",name="2001:db8::1/128",exact=True)).to_be_visible()
        expect(save).to_have_count(0)
        assert write("/apply","applyDnsAccessLists",{"revision":rev}).status == 403
        configure([acl_grant],"status:write")
        assert page.request.get(endpoint,headers=auth).status == 403
        configure([acl_grant],"services.dns:write")
        login(path)
        page.once("dialog", lambda dialog:dialog.accept())
        response = submit(endpoint+"/remove",page.get_by_role("button",name="Delete Office and workshop"))
        assert response.status == 200,response.text()
        expect(page.get_by_text("No access lists yet.",exact=True)).to_be_visible()
        assert all(method == "PUT" for method in sent), sent
        print("Chromium verified DNS ACL creation, network rows, validation, stale edits, separate advanced grants, read-only details and removal.")
    finally:
        docker("exec", "-i", container, *cli, "restore", input=backup)

