"""Verify Services > Avahi on the real router using Chromium and scoped commands."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import re
from xml.etree import ElementTree
from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_avahi(page, origin, credential, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *cli, "backup")
    docker("exec", container, "cp", "-R", "/optional/net/FreeSense-pkg-Avahi/files/usr/local/.", "/check/prefix/")
    base = origin + "/api/v1/packages/avahi"
    auth = {"Authorization": "Bearer " + credential}
    command = auth | {"Origin": origin, "X-FreeSense-Command": "package.avahi.save"}
    writes = []

    def observe(request):
        if request.url.startswith(base) and request.method == "PUT":
            writes.append(request.url)

    def login(grant, scope):
        doc = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        user = doc.find("./system/user")
        for item in user.findall("priv"):
            user.remove(item)
        for item in ["page-system-restapi-keys", grant]:
            ElementTree.SubElement(user, "priv").text = item
        doc.find("./restapi/apitoken/scopes").text = scope
        links = doc.find("./interfaces")
        if links.find("opt1") is None:
            links.append(ElementTree.fromstring("<opt1><enable/><if>em2</if></opt1>"))
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(doc))
        page.goto(origin + "/packages")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        return page.get_by_role("link", name="Services: Avahi", exact=True)

    page.on("request", observe)
    try:
        expect(login("page-services-mdns-bridge", "packages.avahi:write")).to_have_count(0)
        assert page.request.get(base, headers=auth).status == 403
        login("page-services-avahi", "packages.avahi:write")
        menu_link(page,"Services","Services: Avahi").click()
        expect(page).to_have_url(origin + "/services/avahi")
        save = page.get_by_role("button", name="Save", exact=True)
        form = page.locator("form").filter(has=save)
        expect(form.get_by_role("group", name=re.compile(r"^General Settings( About this section)?$"))).to_be_visible()
        role = form.get_by_role("combobox", name="Interfaces 1", exact=True)
        assert role.input_value() == "lan" and all("wan" not in t for t in role.locator("option").all_text_contents())
        publish = form.get_by_label("Enable publishing", exact=True)
        expect(form.get_by_label("Publish addresses", exact=True)).to_be_hidden()
        publish.check()
        expect(form.get_by_label("Publish addresses", exact=True)).to_be_visible()
        publish.uncheck()
        form.get_by_label("Enable", exact=True).check()
        form.get_by_label("Disable IPv4", exact=True).check()
        form.get_by_label("Disable IPv6", exact=True).check()
        save.click()
        expect(form.locator('[data-error-for="disable_ipv6"]')).to_contain_text("Both")
        form.get_by_label("Disable IPv4", exact=True).uncheck()
        expect(form.get_by_role("group", name=re.compile(r"^Reflection Filtering( About this section)?$"))).to_be_hidden()
        form.get_by_label("Enable reflection", exact=True).check()
        form.get_by_role("group", name="Interfaces", exact=True).get_by_role("button", name="Add item").click()
        form.get_by_role("combobox", name="Interfaces 2", exact=True).select_option("opt1")
        services = form.get_by_role("group", name="Services", exact=True)
        services.get_by_role("button", name="Add item").click()
        for i in range(3):
            services.get_by_role("button", name="Add item").click()
        for i, letter in enumerate("abcd", 1):
            services.get_by_label(f"Services {i}", exact=True).fill(letter * (60 if i == 4 else 59))
        save.click()
        expect(form.locator('[data-error-for="filters"]')).to_contain_text("239")
        for _ in range(3):
            services.get_by_role("button", name="Remove").last.click()
        services.get_by_label("Services 1").fill("_ipp._tcp.local")
        form.locator("summary").filter(has_text="Advanced settings").click()
        form.get_by_label("Include Override host name", exact=True).check()
        hostname = form.get_by_label("Override host name", exact=True)
        hostname.fill("host.with.dot")
        save.click()
        expect(form.locator('[data-error-for="hostname"]')).to_contain_text("format")
        assert writes == []
        form.get_by_label("Include Override domain", exact=True).check()
        hostname.fill("a" * 63)
        form.get_by_label("Override domain", exact=True).fill(".".join(["d" * 50] * 4))
        save.click()
        expect(form.locator('[data-error-for="domain"]')).to_contain_text("combined host and domain")
        form.get_by_label("Include Override domain", exact=True).uncheck()
        hostname.fill("Lab-Printer")
        with page.expect_response(lambda r: r.url == base + "/settings" and r.request.method == "PUT") as req:
            save.click()
        assert req.value.status == 200
        selected = req.value.json()["data"]
        assert selected["roles"] == ["lan", "opt1"] and selected["hostname"] == "Lab-Printer" and selected["filters"] == ["_ipp._tcp.local"]
        expect(form.locator('[name="revision"]')).to_have_value(selected["revision"])
        if evidence:
            page.screenshot(path=str(evidence / "avahi-settings.png"), full_page=True)
        body = {k: v for k, v in selected.items() if k not in ["availableRoles", "roleDevices", "approvedDevices"]}
        rejection = page.request.put(base + "/settings", headers=command, data=body | {"disable_ipv4": True})
        assert rejection.status == 422 and "disable_ipv6" in rejection.json()["error"]["fields"]
        invalid = page.request.put(base + "/settings", headers=command, data=body | {"roles": ["lan","wan"]})
        assert invalid.status == 422 and "roles" in invalid.json()["error"]["fields"]
        assert page.request.put(base + "/settings", headers=command, data=body | {"publishing": True}).status == 200
        assert page.request.put(base + "/settings", headers=command, data=body).status == 409
        login("page-services-avahi", "packages.avahi:read")
        menu_link(page,"Services","Services: Avahi").click()
        expect(page.get_by_role("heading", name="Avahi", exact=True)).to_be_visible()
        expect(save).to_have_count(0)
        assert page.request.get(base, headers=auth).json()["data"]["values"]["hostname"] == "Lab-Printer"
        assert page.request.put(base + "/settings", headers=command, data=body).status == 403
        print("Avahi Chromium verified Services navigation, section order, field errors, one AJAX command, scoped reads and writes, stale edits and source bindings.")
    finally:
        page.remove_listener("request", observe)
        docker("exec", "-i", container, *cli, "restore", input=original)
