"""Exercise the LAN Router Advertisement page with independent read, edit and activation grants."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_ra(page, origin, credential, container, docker, evidence=None):
    config = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *config, "backup")
    path = origin + "/api/v1/services/router-advertisement/lan"
    route = origin + "/services/router-advertisement/lan"
    auth = {"Authorization": "Bearer " + credential}
    commands = {"saveRouterAdvertisement": path, "applyRouterAdvertisement": path + "/apply"}
    requests = []

    def watch(request):
        if request.method != "GET" and request.url.startswith(path):
            requests.append((request.method, request.url))

    def restore(doc):
        docker("exec", "-i", container, *config, "restore", input=ET.tostring(doc))

    def access(grant, scope):
        doc = ET.fromstring(docker("exec", container, *config, "backup"))
        user = doc.find("./system/user")
        for node in user.findall("priv"):
            user.remove(node)
        for name in ["page-system-restapi-keys", grant]:
            ET.SubElement(user, "priv").text = name
        doc.find("./restapi/apitoken/scopes").text = scope
        restore(doc)

    def login():
        page.goto(route)
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="LAN", exact=True)).to_be_visible()

    def read():
        r = page.request.get(path, headers=auth)
        assert r.status == 200, r.status
        return r.json()["data"]

    def put(op, data, marked=True):
        headers = auth | {"Origin": origin}
        if marked:
            headers["X-FreeSense-Command"] = op
        return page.request.put(commands[op], headers=headers, data=data)

    def submit(label, uri, status):
        with page.expect_response(lambda r: r.url == uri and r.request.method == "PUT") as pending:
            page.get_by_role("button", name=label, exact=True).click()
        assert pending.value.status == status, (pending.value.status, pending.value.text())
        return pending.value

    def descriptors():
        return page.request.get(origin + "/api/v1/forms", headers=auth).json()["data"]["operations"]

    # The backend reads interfaces from the native helper (JSON rows), no longer from ifconfig; this disposable namespace supplies it.
    rows = [{'name': 'em0', 'flags': ['UP', 'BROADCAST', 'RUNNING', 'MULTICAST'], 'mtu': 1500,
             'addresses': [{'family': 'inet6', 'address': 'fe80::1', 'prefix': 64}, {'family': 'inet6', 'address': '2001:db8:1::1', 'prefix': 64}]},
            {'name': 'em1', 'flags': ['UP', 'BROADCAST', 'RUNNING', 'MULTICAST'], 'mtu': 1500,
             'addresses': [{'family': 'inet6', 'address': '2001:db8:2::1', 'prefix': 64}]}]
    docker('exec', '-i', container, 'sh', '-c', 'cat > /check/ra-nics', input=json.dumps(rows).encode())
    helper = b'#!/bin/sh' + bytes([10]) + b'case "$1" in --version) echo 1 ;; interfaces) cat /check/ra-nics ;; *) exit 1 ;; esac' + bytes([10])
    docker('exec', '-i', container, 'sh', '-c', 'mkdir -p /usr/local/sbin && cat > /usr/local/sbin/freesense-sysinfo && chmod 755 /usr/local/sbin/freesense-sysinfo', input=helper)
    page.on("request", watch)
    try:
        grant = "page-services-router-advertisement"
        access(grant, "services.ra:read")
        assert page.request.get(path).status == 401
        login()
        expect(page.get_by_text("Disabled - RADVD will not be enabled on this interface.", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Save", exact=True)).to_have_count(0)
        assert not (commands.keys() & descriptors().keys())
        access(grant, "services.ra:write")
        login()
        defaults = read()
        assert defaults["mode"] == "disabled" and defaults["priority"] == "normal" and defaults["dns"] == []
        edit = "saveRouterAdvertisement"
        apply = "applyRouterAdvertisement"
        assert edit in descriptors() and apply not in descriptors()
        save = page.get_by_role("button", name="Save", exact=True)
        mode = page.get_by_role("combobox", name="Router Mode")
        priority = page.get_by_role("combobox", name="Router Priority")
        mode.select_option("unmanaged")
        submit("Save", path, 422)
        expect(page.locator('[data-error-for="mode"]')).to_contain_text("static")
        expect(mode).to_have_value("unmanaged")
        assert put(apply, {"revision": defaults["revision"]}).status == 403
        doc = ET.fromstring(docker("exec", container, *config, "backup"))
        for role, device, address in [("lan","em0","2001:db8:1::1"),("wan","em1","2001:db8:2::1")]:
            node = doc.find("./interfaces/" + role)
            assert node.findtext("if") == device
            ET.SubElement(node, "ipaddrv6").text = address
            ET.SubElement(node, "subnetv6").text = "64"
            if role == "wan":
                ET.SubElement(node, "upstream6").text = "fe80::2"
        ET.SubElement(ET.SubElement(doc, "routing6"), "enable")
        restore(doc)
        submit("Save", path, 409)
        expect(mode).to_have_value("unmanaged")
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_text("2001:db8:1::/64", exact=True)).to_be_visible()
        mode.select_option("unmanaged")
        priority.select_option("high")
        servers = page.get_by_role("group", name="DNS Servers")
        add = servers.get_by_role("button", name="Add item")
        add.click()
        dns1 = servers.get_by_label("DNS Servers 1")
        dns1.fill("fe80::1")
        before = len(requests)
        save.click()
        expect(page.locator('[data-error-for="dns"]')).to_contain_text("network format")
        assert len(requests) == before
        dns1.fill("2001:0DB8:0:0:0:0:0:53")
        add.click()
        dns2 = servers.get_by_label("DNS Servers 2")
        dns2.fill("2001:db8::53")
        save.click()
        expect(page.locator('[data-error-for="dns"]')).to_contain_text("distinct")
        assert len(requests) == before
        dns2.fill("fd42::53")
        changed = submit("Save", path, 200).json()["data"]
        assert changed["mode"] == "unmanaged" and changed["dns"] == ["2001:db8::53", "fd42::53"]
        expect(page.locator("#feedback")).to_have_text("Advertisement settings saved.")
        saved = read()
        assert saved["source"] == "fe80::1" and saved["network"] == "2001:db8:1::/64" and saved["bindingValid"]
        direct = {key: saved[key] for key in ("revision","mode","priority","dns")}
        assert put(edit, direct | {"dns": ["2001:db8::53","2001:DB8:0:0:0:0:0:53"]}).status == 422
        assert put(edit, direct | {"dns": [f"fd42::{i}" for i in range(1,6)]}).status == 422
        assert put(edit, direct | {"mode": "not-a-mode"}).status == 422
        assert put(edit, direct | {"source": "fe80::9"}).status == 422
        assert put(edit, direct, False).status in (400,403,415)
        assert page.request.post(path, headers=auth, data=direct).status == 405
        if evidence:
            page.screenshot(path=str(evidence / "lan-settings.png"), full_page=True)
        page.get_by_role("link", name="Overview", exact=True).click()
        page.go_back()
        expect(mode).to_have_value("unmanaged")
        access("api-services-ra-apply-write", "services.ra.apply:write")
        login()
        assert apply in descriptors() and edit not in descriptors()
        expect(save).to_have_count(0)
        assert put(edit, direct).status == 403
        # The real activation owner refuses an enabled service before System policy approval.
        submit("Apply", path + "/apply", 409)
        assert read()["revision"] == saved["revision"]
        if evidence:
            page.screenshot(path=str(evidence / "requires-confirmation.png"), full_page=True)
        access("api-services-ra-read", "services.ra:read")
        login()
        expect(page.get_by_role("button", name="Apply", exact=True)).to_have_count(0)
        expect(page.get_by_text("2001:db8::53, fd42::53", exact=True)).to_be_visible()
        access("api-services-ra-write", "services.ra:write")
        assert put(edit, direct | {"mode": "disabled", "dns": []}).status == 200
        assert read()["mode"] == "disabled" and read()["dns"] == []
        access("page-services-dhcpserver", "services.ra:write")
        assert page.request.get(path, headers=auth).status == 403
        assert all(method == "PUT" for method, uri in requests)
        print("LAN Router Advertisement browser and HTTP passed scope separation, DNS checks, stale edits and refusal of an unconfirmed service activation.")
    finally:
        page.remove_listener("request", watch)
        docker("exec", container, "rm", "-f", "/sbin/ifconfig")
        docker("exec", "-i", container, *config, "restore", input=original)
