"""Exercise dual-stack multicast channel management through the production browser and HTTP router."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_multicast_bridge(page, origin, credential, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    docker("exec", container, "cp", "-R", "/optional/net/FreeSense-pkg-mcast-bridge/files/usr/local/.", "/check/prefix/")
    original = docker("exec", container, *cli, "backup")
    url = origin + "/api/v1/packages/mcast-bridge"
    auth = {"Authorization": "Bearer " + credential}
    sent = []
    page.on("request", lambda r: sent.append(r.method) if r.url.startswith(url + "/") else None)

    def values():
        response = page.request.get(url, headers=auth)
        assert response.status == 200
        return response.json()["data"]["values"]

    def configure(scope, device="em2", grant=True):
        doc = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        user = doc.find("./system/user")
        for priv in user.findall("priv"):
            user.remove(priv)
        ElementTree.SubElement(user, "priv").text = "page-system-restapi-keys"
        if grant:
            ElementTree.SubElement(user, "priv").text = "page-services-mcast-bridge"
        doc.find("./restapi/apitoken/scopes").text = "packages.mcast-bridge:" + scope
        devices = doc.find("interfaces")
        if devices.find("opt1") is None:
            devices.append(ElementTree.fromstring("<opt1><enable/><if>em2</if></opt1>"))
        devices.find("./opt1/if").text = device
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(doc))

    def login():
        page.goto(origin + "/packages")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        menu_link(page,"Services","Services: Multicast Bridge").click()
        expect(page.get_by_role("heading", name="Multicast Bridge", exact=True)).to_be_visible()

    def editor(action):
        page.get_by_role("button", name=action, exact=True).click()
        save = page.get_by_role("button", name="Save multicast channel", exact=True)
        return page.locator("form").filter(has=save), save

    def select_role(form, label, role):
        group = form.get_by_role("group", name=label, exact=True)
        group.get_by_role("button", name="Add item").click()
        choice = group.get_by_role("combobox").last
        assert choice.locator("option").all_text_contents() == ["Choose an internal interface", "lan (em0)", "opt1 (em2)"]
        choice.select_option(role)

    def send(button, path, status=200):
        with page.expect_response(lambda r: r.url == url + path and r.request.method == "PUT") as response:
            button.click()
        assert response.value.status == status, (path, response.value.status, response.value.text())
        if status == 200:
            expect(page.get_by_text("Changes saved.", exact=True)).to_be_visible()
        return response.value

    try:
        configure("write")
        login()
        page.get_by_label("Enabled", exact=True).check()
        page.get_by_role("combobox", name="IGMP querier").select_option("delay")
        page.get_by_role("combobox", name="MLD querier").select_option("never")
        send(page.get_by_role("button", name="Save general settings"), "/settings")
        assert values()["igmp"] == "delay" and values()["mld"] == "never"
        form, save = editor("Add bridge")
        form.get_by_label("UDP port", exact=True).fill("44000")
        form.get_by_label("Description", exact=True).fill("Studio & guest <media>")
        save.click()
        expect(form.locator('[data-error-for="ipv4"]')).not_to_be_empty()
        form.get_by_label("Include IPv4 multicast group", exact=True).check()
        form.get_by_label("IPv4 multicast group", exact=True).fill("224.0.0.251")
        select_role(form, "Inbound interfaces", "lan")
        select_role(form, "Static outbound interfaces", "opt1")
        save.click()
        expect(form.locator('[data-error-for="ipv4"]')).not_to_be_empty()
        form.get_by_label("IPv4 multicast group", exact=True).fill("239.1.2.3")
        form.get_by_label("Include IPv6 multicast group", exact=True).check()
        form.get_by_label("IPv6 multicast group", exact=True).fill("ff02::fb")
        save.click()
        expect(form.locator('[data-error-for="ipv6"]')).not_to_be_empty()
        assert sent == ["PUT"], "Invalid channels reached the router"
        form.get_by_label("IPv6 multicast group", exact=True).fill("ff15::123")
        send(save, "/channels/save")
        expect(page.get_by_role("cell", name="LAN (em0), OPT1 (em2)", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="Studio & guest <media>", exact=True)).to_be_visible()
        if evidence:
            editor("Edit bridge 44000")
            page.screenshot(path=evidence / "channel-editor.png", full_page=True)
        headers = auth | {"Origin": origin, "X-FreeSense-Command": "package.mcast-bridge.channel"}
        saved = values()
        draft = {k: v for k, v in saved["channels"][0].items() if k not in ("devices", "bindings")}
        draft["revision"] = saved["revision"]
        for override in ({"description": "ä" * 81}, {"ipv6": "ff02::fb"}, {"inbound": [], "static_inbound": []},
                         {"devices": {"lan": "em7"}}):
            invalid = page.request.put(url + "/channels/save", headers=headers, data=draft | override)
            assert invalid.status == 422, invalid.text()
        assert page.request.post(url + "/channels/save", headers=headers, data=draft).status == 405
        assert values()["revision"] == saved["revision"]
        form, save = editor("Duplicate bridge 44000")
        expect(form.get_by_label("Original UDP port (0 to add)", exact=True)).to_have_value("0")
        expect(form.get_by_label("UDP port", exact=True)).to_be_empty()
        expect(form.get_by_label("Description", exact=True)).to_have_value("Studio & guest <media>")
        form.get_by_label("UDP port", exact=True).fill("44000")
        send(save, "/channels/save", 422)
        expect(form.locator('[data-error-for="port"]')).to_contain_text("already has a channel")
        form.get_by_label("UDP port", exact=True).fill("44001")
        form.get_by_label("Description", exact=True).fill("Overflow screens")
        form.get_by_label("Enabled", exact=True).uncheck()
        send(save, "/channels/save")
        assert [(r["id"], r["enabled"]) for r in values()["channels"]] == [(44000, True), (44001, False)]
        if evidence:
            page.screenshot(path=evidence / "multicast-bridges.png", full_page=True)
        form, save = editor("Edit bridge 44000")
        form.get_by_label("Description", exact=True).fill("Review after assignment change")
        configure("write", "em4")
        send(save, "/channels/save", 409)
        expect(form.get_by_label("Description", exact=True)).to_have_value("Review after assignment change")
        assert values()["channels"][0]["devices"]["opt1"] == "em2"
        configure("write")
        login()
        editor("Edit bridge 44001")
        remove = page.get_by_role("button", name="Remove multicast channel", exact=True)
        before = len(sent)
        page.once("dialog", lambda dialog: dialog.dismiss())
        remove.click()
        assert len(sent) == before and len(values()["channels"]) == 2
        page.once("dialog", lambda dialog: dialog.accept())
        send(remove, "/channels/remove")
        assert [r["id"] for r in values()["channels"]] == [44000]
        configure("read")
        login()
        expect(page.get_by_role("cell", name="Studio & guest <media>", exact=True)).to_be_visible()
        expect(page.get_by_text("delay", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Add bridge", exact=True)).to_have_count(0)
        headers = auth | {"Origin": origin, "X-FreeSense-Command": "package.mcast-bridge.settings"}
        response = page.request.put(url + "/settings", headers=headers, data={
            "revision": values()["revision"], "enabled": False, "igmp": "quick", "mld": "quick"})
        assert response.status == 403
        configure("write", grant=False)
        assert page.request.get(url, headers=auth).status == 403
        assert set(sent) == {"PUT"}
        print("Multicast Bridge routed browser passed dual-stack channels, role bindings, duplicate, deletion, validation, stale edits and grants.")
    finally:
        docker("exec", "-i", container, *cli, "restore", input=original)
