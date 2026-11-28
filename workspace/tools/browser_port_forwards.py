"""Exercise the routed static port-forward editor with separate edit and list page grants."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree

from browser_aliases import seed_hostname_alias
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_port_forwards(page, origin, credential, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    endpoint = origin + "/api/v1/firewall/nat/port-forwards"
    headers = {"Authorization": "Bearer " + credential, "Origin": origin}
    original = docker("exec", container, *cli, "backup")

    def sign_in(*grants):
        document = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        user = document.find("./system/user")
        for field in user.findall("priv"):
            user.remove(field)
        for privilege in ["page-system-restapi-keys", *grants]:
            ElementTree.SubElement(user, "priv").text = privilege
        wan = document.find("./interfaces/wan")
        for name, value in {"ipaddr": "198.51.100.1", "subnet": "24", "upstream4": "198.51.100.254"}.items():
            element = wan.find(name)
            if element is None:
                element = ElementTree.SubElement(wan, name)
            element.text = value
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(document))
        page.goto(origin + "/firewall/nat/port-forwards")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Port forwards", exact=True)).to_be_visible()

    def click_put(title, path=""):
        with page.expect_response(lambda r: r.url == endpoint + path and r.request.method == "PUT") as response:
            page.get_by_role("button", name=title, exact=True).click()
        return response.value

    def catalog():
        result = page.request.get(endpoint, headers=headers)
        assert result.status == 200
        return result.json()["data"]

    def fill_target(public, target, port):
        page.get_by_label("First WAN port", exact=True).fill(str(public))
        page.get_by_label("LAN target address", exact=True).fill(target)
        page.get_by_label("First target port", exact=True).fill(str(port))

    sent = []

    def observe(request):
        if request.method == "PUT" and request.url.startswith(endpoint):
            sent.append(request.url)

    page.on("request", observe)
    try:
        seed_hostname_alias(container, docker, "PartnerHosts", ["branch.nat.test"])
        sign_in("page-firewall-nat-portforward-edit")
        chooser = page.get_by_role("combobox", name="Select forward", exact=True)
        expect(chooser).to_be_visible()
        assert page.request.get(endpoint, headers=headers).status == 403
        assert chooser.locator("option").all_text_contents() == ["New forward", "Edit by ID"]
        page.get_by_role("combobox", name="Protocol", exact=True).select_option("tcp/udp")
        expect(page.locator("details").filter(has=page.locator("summary", has_text="Source"))).not_to_have_attribute("open", "")
        fill_target(14440, "192.0.2.20", 65535)
        page.get_by_label("Include Last WAN port", exact=True).check()
        end = page.get_by_label("Last WAN port", exact=True)
        end.fill("14439")
        create = page.get_by_role("button", name="Create forward", exact=True)
        create.click()
        expect(end).to_have_attribute("aria-invalid", "true")
        end.fill("14442")
        create.click()
        expect(page.get_by_label("First target port", exact=True)).to_have_attribute("aria-invalid", "true")
        assert sent == []
        fill_target(14440, "203.0.113.100", 24440)
        failed = click_put("Create forward")
        assert failed.status == 422 and "target" in failed.json()["error"]["fields"]
        page.get_by_label("LAN target address", exact=True).fill("192.0.2.20")
        receipt = click_put("Create forward")
        assert receipt.status == 200 and set(receipt.json()["data"]) == {"revision", "id"}
        first = receipt.json()["data"]["id"]
        expect(page.locator("#feedback")).to_contain_text(first)
        chooser.select_option("known")
        page.get_by_label("Forward ID", exact=True).fill(first)
        fill_target(14440, "192.0.2.21", 24440)
        page.get_by_role("combobox", name="Protocol", exact=True).select_option("tcp/udp")
        page.get_by_label("Include Last WAN port", exact=True).check()
        end.fill("14442")
        page.locator('summary', has_text="Source").click()
        page.get_by_label("Replace saved source settings", exact=True).check()
        page.get_by_label("Include Source address or network", exact=True).check()
        source = page.get_by_label("Source address or network", exact=True)
        source.fill("203.0.113.1/25")
        saved_count = len(sent)
        page.get_by_role("button", name="Save forward", exact=True).click()
        expect(source).to_have_attribute("aria-invalid", "true")
        source.fill("@PartnerHosts")
        page.get_by_label("Include Last source port", exact=True).check()
        page.get_by_label("Last source port", exact=True).fill("51000")
        page.get_by_role("button", name="Save forward", exact=True).click()
        expect(page.get_by_label("First source port", exact=True)).to_have_attribute("aria-invalid", "true")
        page.get_by_label("Include First source port", exact=True).check()
        page.get_by_label("First source port", exact=True).fill("51001")
        page.get_by_role("button", name="Save forward", exact=True).click()
        expect(page.get_by_label("Last source port", exact=True)).to_have_attribute("aria-invalid", "true")
        assert len(sent) == saved_count
        page.get_by_label("First source port", exact=True).fill("50999")
        page.once("dialog", lambda dialog: dialog.accept())
        updated = click_put("Save forward", "/edit")
        assert updated.status == 200 and set(updated.json()["data"]) == {"revision", "id"}
        assert page.request.get(endpoint, headers=headers).status == 403
        expect(page.locator("#feedback")).to_contain_text(first)
        blind = {"revision": updated.json()["data"]["revision"], "id": first, "enabled": True, "protocol": "tcp/udp",
                 "public_port": 14440, "public_port_end": 14442, "target": "192.0.2.21", "target_port": 24441}
        edit_headers = headers | {"X-FreeSense-Command": "editPortForward"}
        attempt = page.request.put(endpoint + "/edit", headers=edit_headers, data=blind | {"source": "0.0.0.0/0"})
        assert attempt.status == 422 and "replace_source" in attempt.json()["error"]["fields"]
        assert page.request.put(endpoint + "/edit", headers=edit_headers, data=blind).status == 200
        page.get_by_role("button", name="Refresh", exact=True).click()
        fill_target(14442, "192.0.2.30", 8443)
        overlapping = click_put("Create forward")
        assert overlapping.status == 422 and "public_port" in overlapping.json()["error"]["fields"]
        fill_target(14443, "192.0.2.30", 8443)
        second = click_put("Create forward").json()["data"]["id"]
        sign_in("page-firewall-nat-portforward")
        expect(page.get_by_text("Saved WAN forwards", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Save forward", exact=True)).to_have_count(0)
        first_row, second_row = catalog()["forwards"]
        assert (first_row["id"], first_row["target"], first_row["protocol"], first_row["public_port_end"]) == (
            first, "192.0.2.21", "tcp/udp", 14442)
        assert second_row["id"] == second
        assert (first_row["source"], first_row["source_port"], first_row["source_port_end"]) == ("@PartnerHosts", 50999, 51000)
        for header in ["Source address", "Source ports"]:
            expect(page.get_by_role("columnheader", name=header, exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="@PartnerHosts", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="50999–51000", exact=True)).to_be_visible()
        sign_in("page-firewall-nat-portforward", "page-firewall-nat-portforward-edit")
        chooser.select_option(first)
        expect(page.get_by_label("Source address or network", exact=True)).to_be_visible()
        expect(page.get_by_label("Source address or network", exact=True)).to_have_value("@PartnerHosts")
        expect(page.get_by_label("Last source port", exact=True)).to_have_value("51000")
        expect(page.get_by_label("Source address or network", exact=True)).to_be_disabled()
        assert click_put("Save forward", "/edit").status == 200
        assert next(row for row in catalog()["forwards"] if row["id"] == first)["source"] == "@PartnerHosts"
        chooser.select_option(first)
        page.get_by_label("Replace saved source settings", exact=True).check()
        for field in ["Source address or network", "First source port", "Last source port"]:
            page.get_by_label("Include " + field, exact=True).uncheck()
        assert click_put("Save forward", "/edit").status == 200
        cleared = next(row for row in catalog()["forwards"] if row["id"] == first)
        assert not {"source", "source_port", "source_port_end", "replace_source"}.intersection(cleared)
        sign_in("page-firewall-nat-portforward")
        chooser.select_option(second)
        assert click_put("Move up", "/move").status == 200
        expect(page.locator("#feedback")).to_have_text("Forward order saved.")
        assert [row["id"] for row in catalog()["forwards"]] == [second, first]
        revision = catalog()["revision"]
        other_writer = page.request.put(endpoint + "/enabled", headers=headers | {"X-FreeSense-Command": "setPortForwardEnabled"},
                                        data={"revision": revision, "id": first, "enabled": False})
        assert other_writer.status == 200
        assert click_put("Disable forward", "/enabled").status == 409
        assert [row["enabled"] for row in catalog()["forwards"]] == [True, False]
        page.get_by_role("button", name="Refresh", exact=True).click()
        chooser.select_option(first)
        if evidence:
            page.screenshot(path=str(evidence / "port-forward-list.png"), full_page=True)
        assert click_put("Enable forward", "/enabled").status == 200
        expect(page.locator("#feedback")).to_have_text("Port forward updated.")
        before = len(sent)
        page.once("dialog", lambda dialog: dialog.dismiss())
        remove = page.get_by_role("button", name="Remove forward", exact=True)
        remove.click()
        expect(remove).to_be_enabled()
        assert len(sent) == before
        page.once("dialog", lambda dialog: dialog.accept())
        assert click_put("Remove forward", "/remove").status == 200
        assert [row["id"] for row in catalog()["forwards"]] == [second]
        # Operators with catalog read access get name suggestions using the normal authorized endpoints.
        document = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        aliases = document.find("aliases")
        if aliases is None:
            aliases = ElementTree.SubElement(document, "aliases")
        for kind, name, members in [("alias", "ExternalPeers", ["203.0.113.4/32"]),
                                    ("port_alias", "IncomingPorts", ["20101:20102", "20104"]),
                                    ("port_alias", "ExternalPorts", ["41010:41011"])]:
            entry = ElementTree.SubElement(aliases, kind)
            ElementTree.SubElement(entry, "name").text = name
            for member in members:
                ElementTree.SubElement(entry, "member").text = member
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(document))
        sign_in("page-firewall-nat-portforward", "page-firewall-nat-portforward-edit", "page-firewall-aliases")
        chooser.select_option("")
        page.get_by_label("LAN target address", exact=True).fill("192.0.2.24")
        page.get_by_label("Include First WAN port", exact=True).uncheck()
        page.get_by_label("Include First target port", exact=True).uncheck()
        page.get_by_label("Include WAN port alias", exact=True).check()
        pub_alias = page.get_by_label("WAN port alias", exact=True)
        expect(page.locator('#forward-public_port_alias option[value="IncomingPorts"]')).to_have_count(1)
        pub_alias.fill("IncomingPorts")
        page.locator('summary', has_text="Source").click()
        page.get_by_label("Include Source address or network", exact=True).check()
        page.get_by_label("Source address or network", exact=True).fill("@ExternalPeers")
        page.get_by_label("Include Source port alias", exact=True).check()
        page.get_by_label("Source port alias", exact=True).fill("ExternalPorts")
        page.get_by_label("Include First source port", exact=True).check()
        page.get_by_label("First source port", exact=True).fill("41010")
        before = len(sent)
        page.get_by_role("button", name="Create forward", exact=True).click()
        expect(page.get_by_label("Source port alias", exact=True)).to_have_attribute("aria-invalid", "true")
        assert len(sent) == before
        page.get_by_label("Include First source port", exact=True).uncheck()
        alias_id = click_put("Create forward").json()["data"]["id"]
        result = next(row for row in catalog()["forwards"] if row["id"] == alias_id)
        assert (result["public_port_alias"], result["source_port_alias"], result["source"]) == ("IncomingPorts", "ExternalPorts", "@ExternalPeers")
        assert "target_port" not in result and "public_port" not in result
        expect(page.get_by_role("cell", name="Keep WAN ports", exact=True)).to_be_visible()
        chooser.select_option(alias_id)
        page.get_by_label("Include First target port", exact=True).check()
        page.get_by_label("First target port", exact=True).fill("443")
        assert click_put("Save forward", "/edit").status == 200
        assert next(row for row in catalog()["forwards"] if row["id"] == alias_id)["target_port"] == 443
        print("Port-forward browser/API passed Source disclosure and columns, paired source-port validation, restriction-preserving blind edits and explicit clearing, separate grants, stale saves, reordering, toggling and removal.")
    except Exception:
        if evidence:
            (evidence / "browser-failure.txt").write_text(page.locator("#workspace").inner_text(), encoding="utf-8")
        raise
    finally:
        page.remove_listener("request", observe)
        docker("exec", "-i", container, *cli, "restore", input=original)
