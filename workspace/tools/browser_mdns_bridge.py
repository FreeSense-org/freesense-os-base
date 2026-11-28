"""Verify the optional bridge with the installed router, real browser controls and scoped credentials."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_mdns_bridge(page, origin, credential, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    prefix = "/optional/net/FreeSense-pkg-mDNS-Bridge/files/usr/local/."
    docker("exec", container, "cp", "-R", prefix, "/check/prefix/")
    original = docker("exec", container, *cli, "backup")
    endpoint = origin + "/api/v1/packages/mdns-bridge"
    auth = {"Authorization": "Bearer " + credential}
    command = auth | {"Origin": origin, "X-FreeSense-Command": "package.mdns-bridge.save"}
    writes = []

    def observe(request):
        if request.method == "PUT" and request.url.startswith(endpoint):
            writes.append(request.url)

    def fixture(grant, scope):
        doc = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        user = doc.find("./system/user")
        for item in user.findall("priv"):
            user.remove(item)
        for privilege in ["page-system-restapi-keys", grant]:
            ElementTree.SubElement(user, "priv").text = privilege
        doc.find("./restapi/apitoken/scopes").text = scope
        ports = doc.find("./interfaces")
        if ports.find("opt1") is None:
            ports.append(ElementTree.fromstring("<opt1><enable/><if>em2</if></opt1>"))
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(doc))
        page.goto(origin + "/packages")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        service = page.get_by_role("link", name="Services: mDNS Bridge", exact=True)
        if grant == "page-services-mdns-bridge":
            expect(page.get_by_role("link", name="mDNS Bridge", exact=True)).to_have_attribute("href", "/packages/mdns-bridge")
            menu_link(page,"Services","Services: mDNS Bridge").click()
            expect(page).to_have_url(origin + "/packages/mdns-bridge")
        else:
            expect(service).to_have_count(0)

    def put(button):
        with page.expect_response(lambda r: r.url == endpoint + "/settings" and r.request.method == "PUT") as response:
            button.click()
        return response.value

    page.on("request", observe)
    try:
        fixture("page-services-dnsresolver", "packages.mdns-bridge:write")
        assert page.request.get(endpoint, headers=auth).status == 403
        assert page.request.put(endpoint + "/settings", headers=command, data={}).status == 403
        fixture("page-services-mdns-bridge", "packages.mdns-bridge:write")
        expect(page.get_by_role("heading", name="mDNS Bridge", exact=True)).to_be_visible()
        save = page.get_by_role("button", name="Save bridge settings", exact=True)
        form = page.locator("form").filter(has=save)
        form.get_by_label("Enable mDNS Bridge").check()
        roles = form.get_by_role("group", name="Internal interfaces", exact=True)
        roles.get_by_role("button", name="Add item", exact=True).click()
        role = roles.get_by_role("combobox", name="Internal interfaces 1", exact=True)
        assert role.locator("option").all_text_contents() == ["Choose an internal interface", "lan", "opt1"]
        role.select_option("opt1")
        save.click()
        expect(form.locator('[data-error-for="roles"]')).to_contain_text("2")
        assert writes == []
        roles.get_by_role("button", name="Add item", exact=True).click()
        roles.get_by_role("combobox", name="Internal interfaces 2", exact=True).select_option("lan")
        form.get_by_role("combobox", name="Global inbound filter", exact=True).select_option("allow")
        save.click()
        expect(form.locator('[data-error-for="filters"]')).to_contain_text("1")
        filters = form.get_by_role("group", name="mDNS filter names", exact=True)
        filters.get_by_role("button", name="Add item", exact=True).click()
        first = filters.get_by_label("mDNS filter names 1", exact=True)
        first.fill("_ipp,*")
        save.click()
        expect(form.locator('[data-error-for="filters"]')).to_contain_text("format")
        assert writes == []
        first.fill("Office-Printer._ipp")
        result = put(save)
        assert result.status == 200
        values = result.json()["data"]
        assert values["roles"] == ["opt1", "lan"] and values["filters"] == ["Office-Printer._ipp"] and values["enabled"]
        expect(form.locator('[name="revision"]')).to_have_value(values["revision"])
        if evidence:
            page.screenshot(path=str(evidence / "mdns-bridge-editor.png"), full_page=True)
        body = {k: v for k, v in values.items() if k != "availableRoles"}
        invalid = page.request.put(endpoint + "/settings", headers=command, data=body | {"roles": ["wan", "lan"]})
        assert invalid.status == 422 and "roles" in invalid.json()["error"]["fields"]
        other_writer = page.request.put(endpoint + "/settings", headers=command, data=body | {"family": "ipv6"})
        assert other_writer.status == 200 and put(save).status == 409
        fixture("page-services-mdns-bridge", "packages.mdns-bridge:read")
        expect(page.get_by_role("heading", name="mDNS Bridge", exact=True)).to_be_visible()
        assert page.request.get(endpoint, headers=auth).json()["data"]["values"]["family"] == "ipv6"
        expect(save).to_have_count(0)
        assert page.request.put(endpoint + "/settings", headers=command, data=body).status == 403
        apply_headers = auth | {"Origin": origin, "X-FreeSense-Command": "package.mdns-bridge.apply"}
        assert page.request.put(endpoint + "/apply", headers=apply_headers, data={"revision": values["revision"]}).status == 403
        print("mDNS Bridge chrome/PHP passed assigned LAN/OPT choices, case-preserving filters, client rejection, draft save, independent grants, stale edit and scoped apply.")
    finally:
        page.remove_listener("request", observe)
        docker("exec", "-i", container, *cli, "restore", input=original)
