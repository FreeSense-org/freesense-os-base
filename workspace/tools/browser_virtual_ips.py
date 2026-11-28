"""Exercise routed IP Alias editing and reuse of virtual addresses by the firewall and NAT editors."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_virtual_ips(page, origin, key, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    backup = docker("exec", container, *cli, "backup")
    endpoint = origin + "/api/v1/firewall/virtual-ips"
    site = "/firewall/virtual-ips"
    auth = {"Authorization": "Bearer " + key}
    listing = "page-firewall-virtualipaddresses"
    editor = "page-firewall-virtualipaddress-edit"

    def configure(grants, scopes):
        doc = ET.fromstring(docker("exec", container, *cli, "backup"))
        user = doc.find("./system/user")
        for privilege in user.findall("priv"):
            user.remove(privilege)
        for name in ["page-system-restapi-keys", *grants]:
            ET.SubElement(user, "priv").text = name
        doc.find("./restapi/apitoken/scopes").text = scopes
        wan = doc.find("./interfaces/wan")
        for field, value in {"ipaddr": "198.51.100.1", "subnet": "24", "upstream4": "198.51.100.254"}.items():
            elem = wan.find(field)
            if elem is None:
                elem = ET.SubElement(wan, field)
            elem.text = value
        docker("exec", "-i", container, *cli, "restore", input=ET.tostring(doc))

    def login(path):
        page.goto(origin + path)
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in", exact=True).click()

    def write(command, suffix, data):
        return page.request.put(endpoint + suffix, headers=auth | {"Origin": origin, "X-FreeSense-Command": command}, data=data)

    def capture(command_path, button):
        with page.expect_response(lambda r: r.url == command_path and r.request.method == "PUT") as response:
            button.click()
        return response.value

    try:
        configure([listing, editor], "firewall.virtual-ips:write")
        login(site)
        page.get_by_role("link", name="Add virtual IP", exact=True).click()
        submit = page.get_by_role("button", name="Save virtual IP", exact=True)
        address = page.get_by_label("Virtual IP address", exact=True)
        sent = []
        page.on("request", lambda r: sent.append(r.url) if r.method == "PUT" and r.url.startswith(endpoint) else None)
        # The address field takes any IP (Proxy ARP and Other blocks share it); a prefix is refused in the browser, and the
        # IP Alias host rules (no multicast, no link-local) are refused by the server next to the field.
        address.fill("192.0.2.33/24")
        submit.click()
        expect(address).to_have_attribute("aria-invalid", "true")
        assert not sent
        for invalid in ["224.0.0.1", "fe80::2"]:
            address.fill(invalid)
            refused = capture(endpoint, submit)
            assert refused.status == 422 and "address" in refused.json()["error"]["fields"], refused.text()
            expect(address).to_have_attribute("aria-invalid", "true")
        address.fill("192.0.2.1")
        rejected = capture(endpoint, submit)
        assert rejected.status == 422 and "address" in rejected.json()["error"]["fields"]
        address.fill("2001:db8::10")
        no_v6 = capture(endpoint, submit)
        assert no_v6.status == 422 and "role" in no_v6.json()["error"]["fields"]
        address.fill("192.0.2.33")
        page.get_by_label("Include Description", exact=True).check()
        page.get_by_label("Description", exact=True).fill("Lab <secondary>")
        created = capture(endpoint, submit)
        assert created.status == 200 and set(created.json()["data"]) == {"id", "revision"}, created.text()
        first = created.json()["data"]
        expect(page.get_by_role("cell", name="Lab <secondary>", exact=True)).to_be_visible()
        page.get_by_role("link", name="Add virtual IP", exact=True).click()
        page.get_by_role("combobox", name="Interface", exact=True).select_option("wan")
        address.fill("198.51.100.33")
        second = capture(endpoint, submit)
        assert second.status == 200, second.text()
        second = second.json()["data"]
        expect(page.get_by_role("cell", name="198.51.100.33", exact=True)).to_be_visible()
        doc = ET.fromstring(docker("exec", container, *cli, "backup"))
        ET.SubElement(ET.SubElement(doc, "routing6"), "enable")
        for role, primary in [("lan", "2001:db8:1::1"), ("wan", "2001:db8:2::1")]:
            iface = doc.find("./interfaces/" + role)
            ET.SubElement(iface, "ipaddrv6").text = primary
            ET.SubElement(iface, "subnetv6").text = "64"
            if role == "wan":
                ET.SubElement(iface, "upstream6").text = "2001:db8:2::ff"
        docker("exec", "-i", container, *cli, "restore", input=ET.tostring(doc))
        login(site + "/edit")
        address.fill("2001:0db8:1:0::89")
        created_v6 = capture(endpoint, submit)
        assert created_v6.status == 200, created_v6.text()
        expect(page.get_by_role("cell", name="2001:db8:1::89", exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / "virtual-ips.png", full_page=True)
        assert write("removeVirtualIp", "/remove", first).status == 409
        configure([editor], "firewall.virtual-ips:write")
        login(site + "/edit/" + first["id"])
        expect(address).to_have_value("192.0.2.33")
        assert page.request.get(endpoint, headers=auth).status == 403
        projection = page.request.get(endpoint + "/edit/" + first["id"], headers=auth)
        assert projection.status == 200 and set(projection.json()["data"]) == {"revision", "row"}
        assert "198.51.100.33" not in projection.text()
        if evidence:
            page.screenshot(path=evidence / "virtual-ip-editor.png", full_page=True)
        address.fill("192.0.2.34")
        edited = capture(endpoint + "/edit", submit)
        assert edited.status == 200, edited.text()
        revision = edited.json()["data"]["revision"]
        assert write("removeVirtualIp", "/remove", {"revision": revision, "id": second["id"]}).status == 403
        assert write("applyVirtualIps", "/apply", {"revision": revision}).status == 403

        # The actual NAT command stores the WAN alias id; a saved forward protects it from deletion.
        configure([listing, editor, "page-firewall-nat-portforward-edit", "page-firewall-nat-1-1-edit", "api-firewall-rules-write"],
                  "firewall.virtual-ips:write firewall.nat:write firewall.rules:write")
        login("/firewall/nat/port-forwards")
        destination = page.get_by_role("combobox", name="Destination address", exact=True)
        # The shared select presents the native <select>, which stays the source of the options.
        options = destination.locator("xpath=../preceding-sibling::select[1]")
        expect(options).to_contain_text("198.51.100.33")
        assert options.locator("option[value=primary]").count() == 1
        assert options.locator("option[value='" + first["id"] + "']").count() == 0
        destination.select_option(second["id"])
        page.get_by_label("First WAN port", exact=True).fill("25001")
        page.get_by_label("First target port", exact=True).fill("25002")
        page.get_by_label("LAN target address", exact=True).fill("192.0.2.55")
        forward = capture(origin + "/api/v1/firewall/nat/port-forwards", page.get_by_role("button", name="Create forward", exact=True))
        assert forward.status == 200, forward.text()
        assert write("removeVirtualIp", "/remove", {"revision": revision, "id": second["id"]}).status in (409, 422)
        login("/firewall/nat/one-to-one")
        external = page.get_by_label("External subnet IP", exact=True)
        expect(external).to_be_visible()
        selector = page.locator("datalist[id='" + external.get_attribute("list") + "']")
        expect(selector.locator("option[value='198.51.100.33']")).to_have_count(1)
        expect(selector.locator("option[value='192.0.2.34']")).to_have_count(0)
        login("/firewall")
        for ip in ["198.51.100.33", "192.0.2.34", "2001:db8:1::89"]:
            expect(page.locator("#rule-address-aliases option[value='" + ip + "']")).to_have_count(1)

        configure([listing], "firewall.virtual-ips:read")
        login(site)
        expect(page.get_by_role("cell", name="192.0.2.34", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Apply changes", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Delete 192.0.2.34", exact=True)).to_have_count(0)
        assert page.request.get(endpoint + "/edit/" + first["id"], headers=auth).status == 403
        assert write("applyVirtualIps", "/apply", {"revision": revision}).status == 403
        configure([listing], "firewall.virtual-ips:write")
        login(site)
        expect(page.get_by_role("link", name="Add virtual IP", exact=True)).to_have_count(0)
        page.on("dialog", lambda dialog: dialog.accept())
        apply = capture(endpoint + "/apply", page.get_by_role("button", name="Apply changes", exact=True))
        assert apply.status == 503 and apply.json()["error"]["code"] == "service_unavailable"
        deleted = capture(endpoint + "/remove", page.get_by_role("button", name="Delete 192.0.2.34", exact=True))
        assert deleted.status == 200, deleted.text()
        expect(page.get_by_role("cell", name="198.51.100.33", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="192.0.2.34", exact=True)).to_have_count(0)
        print("Chromium verified IP Alias validation, grant isolation, stale edits, dependency refusal, NAT and firewall choices.")
    finally:
        docker("exec", "-i", container, *cli, "restore", input=backup)
