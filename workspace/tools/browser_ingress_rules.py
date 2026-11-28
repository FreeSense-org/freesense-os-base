"""Exercise assigned-interface filtering, ordered replacements and role-limited imports."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_ingress_rules(page, origin, credential, container, docker, evidence=None):
    config = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *config, "backup")
    rules = origin + "/api/v1/firewall/rules"
    auth = {"Authorization": "Bearer " + credential}
    save_headers = auth | {"Origin": origin, "X-FreeSense-Command": "saveRules"}
    save = page.get_by_role("button", name="Save rules", exact=True)
    filt = page.get_by_role("combobox", name="Filter by interface", exact=True)
    put_requests = []

    def observe(request):
        if request.method == "PUT" and request.url.startswith(rules):
            put_requests.append(request.url)

    def restore(document):
        docker("exec", "-i", container, *config, "restore", input=ET.tostring(document))

    def document():
        return ET.fromstring(docker("exec", container, *config, "backup"))

    def stored():
        return json.loads(docker("exec", container, "php", "/check/prefix/sbin/freesense-firewall", "list"))["rules"]

    def read():
        reply = page.request.get(rules, headers=auth)
        assert reply.status == 200
        return reply.json()["data"]

    def grant(*privileges):
        doc = document()
        owner = doc.find("./system/user")
        for node in owner.findall("priv"):
            owner.remove(node)
        for value in ("page-system-restapi-keys", *privileges):
            ET.SubElement(owner, "priv").text = value
        doc.find("./restapi/apitoken/scopes").text = "firewall.rules:write"
        restore(doc)
        page.goto(origin + "/firewall")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Firewall", exact=True)).to_be_visible()

    def submit(button, url=rules):
        with page.expect_response(lambda r: r.url == url and r.request.method == "PUT") as pending:
            button.click()
        assert pending.value.status == 200, pending.value.status
        expect(page.locator("#feedback")).to_have_text("Draft saved.")
        return pending.value

    def file(rows):
        page.get_by_label("JSON rules file").set_input_files(
            {"name": "ordered.json", "mimeType": "application/json", "buffer": json.dumps(rows).encode()})

    page.on("request", observe)
    try:
        doc = ET.fromstring(original)
        for name in ("interfaces", "firewall"):
            for old in doc.findall(name):
                doc.remove(old)
        doc.append(ET.fromstring("<interfaces><lan><enable/><if>em0</if><ipaddr>192.0.2.1</ipaddr><subnet>24</subnet></lan>"
                                 "<wan><enable/><if>em1</if><ipaddr>dhcp</ipaddr><upstream4>dhcp</upstream4><ipaddrv6>dhcp6</ipaddrv6><gatewayv6>WAN_V6_IMPORTED</gatewayv6></wan>"
                                 "<opt1><enable/><if>em2</if><ipaddr>198.51.100.1</ipaddr><subnet>24</subnet></opt1></interfaces>"))
        restore(doc)
        grant("api-firewall-rules-write")
        base = {"enabled": True, "action": "block", "source": "any", "destination": "192.0.2.2", "protocol": "tcp"}
        local = {"enabled": True, "action": "pass", "traffic": "local-in", "interface": "opt1", "protocol": "udp",
                 "destination_port": 5353, "source": "link", "destination": "224.0.0.251"}
        seed = [base | {"interface": "opt1", "destination_port": 5011}, base | {"destination_port": 5012},
                base | {"interface": "opt1", "destination_port": 5013}, local,
                base | {"interface": "wan", "destination_port": 5014}]
        result = page.request.put(rules, headers=save_headers, data={"revision": read()["revision"], "rules": seed})
        assert result.status == 200, result.status
        saved = stored()
        page.get_by_role("button", name="Refresh", exact=True).click()
        filt.select_option("opt1")
        first = page.locator(".rule-editor:visible").first
        expect(page.locator(".rule-editor:visible")).to_have_count(3)
        first.get_by_role("button", name="Move down", exact=True).click()
        submit(save)
        assert [r["id"] for r in stored()] == [saved[i]["id"] for i in (2, 1, 0, 3, 4)]
        expect(filt).to_have_value("opt1")
        page.get_by_role("button", name="Add rule", exact=True).click()
        new = page.locator(".rule-editor:visible").last
        expect(new.get_by_role("combobox", name="Interface", exact=True)).to_have_value("opt1")
        new.get_by_label("Enabled", exact=True).check()
        new.get_by_role("combobox", name="Action", exact=True).select_option("pass")
        new.get_by_label("Source", exact=True).fill("any")
        new.get_by_label("Destination", exact=True).fill("192.0.2.15")
        submit(save)
        assert stored()[-1]["interface"] == "opt1"
        if evidence:
            page.screenshot(path=str(evidence / "ordinary-interface-rules.png"))
        filt.select_option("wan")
        wan = page.locator(".rule-editor:visible")
        wan.get_by_role("combobox", name="Action", exact=True).select_option("pass")
        expect(wan.get_by_text("The appliance learns the return gateway", exact=False)).to_be_visible()
        submit(save)
        expect(page.get_by_role("heading", name="WAN IPv4 return gateway")).to_be_visible()
        expect(page.get_by_text("Awaiting approval", exact=True)).to_be_visible()
        dhcp = read()["dhcp_return"]
        assert dhcp == {"source": "dhcp", "phase": "unapproved", "gateway": None, "expires": None, "reason": "approval_required"}
        if evidence:
            page.screenshot(path=str(evidence / "wan-dhcp-return-draft.png"))
        forged = stored()
        next(r for r in forged if r.get("interface") == "wan")["gateway"] = "198.51.100.254"
        assert page.request.put(rules, headers=save_headers, data={"revision": read()["revision"], "rules": forged}).status == 422
        wan.get_by_role("combobox", name="Address family", exact=True).select_option("inet6")
        wan.get_by_label("Destination", exact=True).fill("2001:db8::1")
        # A dynamic IPv6 WAN passes without a reviewed gateway (1.2 parity); an imported 1.2 <gatewayv6> name is still refused.
        expect(wan.get_by_text("A pass in this address family", exact=False)).to_be_visible()
        before = len(put_requests)
        save.click()
        expect(page.locator('[data-error-for="rules"]')).to_contain_text("reply gateway")
        expect(wan.locator("[data-role-error]")).to_contain_text("reply gateway")
        expect(wan.get_by_role("combobox", name="Interface", exact=True)).to_have_attribute("aria-invalid", "true")
        assert len(put_requests) == before
        reject = stored()
        reject[-2]["action"] = "pass"
        reject[-2]["family"] = "inet6"
        reject[-2]["destination"] = "2001:db8::1"
        assert page.request.put(rules, headers=save_headers, data={"revision": read()["revision"], "rules": reject}).status == 422
        del reject[-2]["interface"]
        file(reject)
        count = len(put_requests)
        page.get_by_role("button", name="Import JSON", exact=True).click()
        expect(page.locator('[data-error-for="file"]')).to_contain_text("reply gateway")
        assert len(put_requests) == count

        grant("api-firewall-rules-opt1-write")
        access = read()
        assert access["editable_roles"] == ["opt1"] and len(access["rules"]) == 3
        assert "dhcp_return" not in access
        spec = page.request.get(origin + "/api/v1/forms", headers=auth).json()["data"]["operations"]
        for operation, field in (("saveRules", "rules"), ("importRules", "file")):
            descriptor = spec[operation]["fields"][field]
            assert descriptor["editableRoles"] == ["opt1"] and descriptor["ingressRoles"] == ["opt1"]
            assert descriptor["dynamicReturnRoles"] == []
            assert descriptor["itemFields"]["traffic"]["choices"] == ["transit"]
        expect(page.locator(".rule-editor")).to_have_count(3)
        assert filt.locator("option[value=wan]").count() == 0
        before = stored()
        import_button = page.get_by_role("button", name="Import JSON", exact=True)
        file([access["rules"][0] | {"interface": "wan"}])
        count = len(put_requests)
        import_button.click()
        expect(page.locator('[data-error-for="file"]')).to_contain_text("read-only")
        assert len(put_requests) == count
        file(list(reversed(access["rules"])))
        submit(import_button, rules + "/import")
        after = stored()
        slots = [index for index, r in enumerate(before) if r.get("interface") == "opt1" and not r.get("traffic")]
        expected = before.copy()
        for index, row in zip(slots, reversed(access["rules"])):
            expected[index] = row
        assert after == expected

        grant("api-firewall-rules-read", "api-firewall-rules-opt1-write")
        expect(page.get_by_role("heading", name="Read-only firewall rules")).to_be_visible()
        expect(page.locator(".rule-editor")).to_have_count(3)
        response = submit(save)
        assert all(r.get("interface") == "opt1" and not r.get("traffic") for r in response.request.post_data_json["rules"])
        assert stored() == after
        first = page.locator(".rule-editor:visible").first
        first.get_by_label("Destination", exact=True).fill("192.0.2.48")
        changed = document()
        changed.find("interfaces").append(ET.fromstring("<opt2><enable/><if>em3</if></opt2>"))
        restore(changed)
        with page.expect_response(lambda r: r.url == rules and r.request.method == "PUT") as stale:
            save.click()
        assert stale.value.status == 409 and stored() == after
        expect(first.get_by_label("Destination", exact=True)).to_have_value("192.0.2.48")
        page.get_by_role("link", name="Overview", exact=True).click()
        page.go_back()
        expect(save).to_be_visible()
        grant("api-firewall-rules-wan-read")
        expect(save).to_have_count(0)
        expect(page.get_by_role("cell", name="WAN", exact=True)).to_be_visible()
        assert read()["editable_roles"] == [] and len(read()["rules"]) == 1
        assert page.request.put(rules, headers=save_headers, data={"revision": read()["revision"], "rules": []}).status == 403
        print("Assigned-interface editor and HTTP passed filter/reorder, scoped imports, IPv4 DHCP drafts, IPv6 dynamic refusal and role ownership.")
    finally:
        page.remove_listener("request", observe)
        docker("exec", "-i", container, *config, "restore", input=original)
