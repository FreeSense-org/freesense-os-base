"""Exercise the floating rule editor and both validation boundaries."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from xml.etree import ElementTree
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_floating_rules(page, origin, credential, container, docker, evidence=None):
    config = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *config, "backup")
    xml = ElementTree.fromstring(original)
    user = xml.find("./system/user")
    for priv in user.findall("priv"):
        user.remove(priv)
    for privilege in ["page-system-restapi-keys", "api-firewall-rules-write"]:
        ElementTree.SubElement(user, "priv").text = privilege
    xml.find("./restapi/apitoken/scopes").text = "firewall.rules:write"
    endpoint = origin + "/api/v1/firewall/rules"
    headers = {"Authorization": "Bearer " + credential}
    try:
        docker("exec", "-i", container, *config, "restore", input=ElementTree.tostring(xml))
        page.goto(origin + "/firewall")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.get_by_role("combobox", name="Filter by interface", exact=True).select_option("floating")
        page.get_by_role("button", name="Add rule", exact=True).click()
        row = page.locator(".rule-editor:visible").last
        row.get_by_role("combobox", name="Action", exact=True).select_option("reject")
        row.get_by_role("combobox", name="Direction", exact=True).select_option("out")
        row.get_by_label("Quick", exact=True).check()
        row.get_by_label("Enabled", exact=True).check()
        row.get_by_role("listbox", name="Interfaces (empty for all)", exact=True).select_option(["lan", "wan"])
        row.get_by_label("Source", exact=True).fill("any")
        row.get_by_label("Destination", exact=True).fill("any")
        description = 'Tromsø <b> DNS "guest" + $if'
        row.get_by_label("Description", exact=True).fill(description)
        row.get_by_text("Packet tagging", exact=True).click()
        row.get_by_label("Tag", exact=True).fill("Tier_One")
        row.get_by_label("Tagged", exact=True).fill("Tier_Two")
        row.get_by_label("Invert tagged match", exact=True).check()
        with page.expect_response(lambda r: r.url == endpoint and r.request.method == "PUT") as saved:
            page.get_by_role("button", name="Save rules", exact=True).click()
        assert saved.value.status == 200, saved.value.text()
        draft = page.request.get(endpoint, headers=headers).json()["data"]
        record = next(r for r in draft["rules"] if r.get("traffic") == "floating")
        assert record["tag"] == "Tier_One" and record["tagged"] == "Tier_Two" and record["nottagged"]
        assert record["description"] == description
        tracker = draft["trackers"][record["id"]]
        expect(page.get_by_text("Log tracker " + str(tracker), exact=True)).to_be_visible()
        expect(page.locator(".rule-editor:visible").last.get_by_label("Tag", exact=True)).to_have_value("Tier_One")
        assert set(record["interfaces"]) == {"wan", "lan"} and record["quick"] and record["direction"] == "out" and record["action"] == "reject"
        expect(page.locator(".rule-editor:visible").last.get_by_role("combobox", name="Direction", exact=True)).to_have_value("out")
        if evidence:
            page.locator(".rule-editor:visible").last.screenshot(path=evidence/"floating-rule-editor.png")
        bad = record | {"interfaces": ["wan", "wan"]}
        sent = []
        page.on("request", lambda r: sent.append(r.url) if r.method == "PUT" else None)
        page.get_by_label("JSON rules file").set_input_files({"name": "rules.json", "mimeType":"application/json",
                                                             "buffer": json.dumps([bad]).encode()})
        page.get_by_role("button", name="Import JSON", exact=True).click()
        expect(page.get_by_text("Choose distinct list items.", exact=False).first).to_be_visible()
        assert not sent
        refused = page.request.put(endpoint, headers=headers | {"Origin":origin, "X-FreeSense-Command":"saveRules"},
                                   data={"revision":draft["revision"], "rules":[bad]})
        assert refused.status == 422, refused.text()
        assert page.request.get(endpoint, headers=headers).json()["data"]["rules"] == draft["rules"]
        for malformed in [record | {"tag":"FS_LAN_PASS"}, {k:v for k,v in record.items() if k != "tagged"},
                          record | {"description":"é" * 128}, record | {"description":"line\nfeed"}, record | {"tracker":tracker}]:
            rejected_tag = page.request.put(endpoint, headers=headers | {"Origin":origin, "X-FreeSense-Command":"saveRules"},
                                            data={"revision":draft["revision"], "rules":[malformed]})
            assert rejected_tag.status == 422, rejected_tag.text()
        sent.clear()
        page.get_by_label("JSON rules file").set_input_files({"name":"tags.json","mimeType":"application/json",
                                                             "buffer":json.dumps([record | {"tag":"FS_FLOAT_PASS"}]).encode()})
        page.get_by_role("button", name="Import JSON", exact=True).click()
        expect(page.get_by_text("reserved FS_ namespace", exact=False).first).to_be_visible()
        assert not sent
        page.get_by_label("JSON rules file").set_input_files({"name":"length.json", "mimeType":"application/json",
                                                             "buffer":json.dumps([record | {"description":"é" * 128}]).encode()})
        page.get_by_role("button", name="Import JSON", exact=True).click()
        expect(page.get_by_text("allowed byte or character length", exact=False).first).to_be_visible()
        assert not sent
        latest = ElementTree.fromstring(docker("exec", container, *config, "backup"))
        user = latest.find("./system/user/priv[.='api-firewall-rules-write']")
        user.text = "api-firewall-rules-read"
        docker("exec", "-i", container, *config, "restore", input=ElementTree.tostring(latest))
        page.goto(origin + "/firewall")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        readonly = page.get_by_role("row").filter(has_text=description)
        expect(readonly).to_contain_text("Log tracker " + str(tracker))
        expect(readonly).to_contain_text("Set Tier_One")
        expect(readonly).to_contain_text("Match except Tier_Two")
        assert readonly.locator("b").count() == 0
        if evidence:
            page.get_by_role("table").screenshot(path=evidence/"rule-descriptions-readonly.png")
        user.text = "api-firewall-rules-write"
        privilege = latest.find("./system/user/priv[.='api-firewall-rules-write']")
        privilege.text = "api-firewall-rules-lan-write"
        docker("exec", "-i", container, *config, "restore", input=ElementTree.tostring(latest))
        scoped = page.request.get(endpoint, headers=headers).json()["data"]
        assert all(row.get("traffic") != "floating" for row in scoped["rules"])
        assert record["id"] not in scoped.get("trackers", {}) and description not in json.dumps(scoped, ensure_ascii=False)
        forms = page.request.get(origin + "/api/v1/forms", headers=headers).json()["data"]
        fields = forms["operations"]["saveRules"]["fields"]["rules"]["itemFields"]
        assert "interfaces" not in fields and "quick" not in fields and "direction" not in fields
        assert fields["action"]["choices"] == ["pass", "block"]
        rejected = page.request.put(endpoint, headers=headers | {"Origin":origin, "X-FreeSense-Command":"saveRules"},
                                    data={"revision":scoped["revision"], "rules":[record]})
        assert rejected.status == 403, rejected.text()
        scoped_save = page.request.put(endpoint, headers=headers | {"Origin":origin, "X-FreeSense-Command":"saveRules"},
                                       data={"revision":scoped["revision"], "rules":scoped["rules"]})
        assert scoped_save.status == 200, scoped_save.text()
        refreshed = ElementTree.fromstring(docker("exec", container, *config, "backup"))
        stored = refreshed.find("./firewall/rule[@traffic='floating']")
        assert stored is not None and stored.get("id") == record["id"] and stored.get("quick") == "true"
        print("Chromium saved and reopened a multi-interface floating rule. Both validation boundaries refused duplicate interfaces.")
    finally:
        docker("exec", "-i", container, *config, "restore", input=original)
