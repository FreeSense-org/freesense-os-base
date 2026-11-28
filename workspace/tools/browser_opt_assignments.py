"""Exercise the granted assignment editor and the same authenticated HTTP command."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_opt_assignments(page, origin, credential, container, docker, evidence=None):
    config = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *config, "backup")
    path = origin + "/api/v1/interfaces/assignments"
    auth = {"Authorization": "Bearer " + credential}
    marker = {"Origin": origin, "X-FreeSense-Command": "addOptAssignment"}
    secret = auth | marker
    # The backend reads interfaces from the native helper (/usr/local/sbin/freesense-sysinfo, JSON rows), no longer from ifconfig.
    mock = b'#!/bin/sh\ncase "$1" in --version) echo 1 ;; interfaces) cat /check/assignment-nics ;; *) exit 1 ;; esac\n'

    def rows(*names):
        flags = {"em0": ["UP", "BROADCAST", "RUNNING"], "em1": ["UP", "BROADCAST", "RUNNING"], "em2": ["UP", "BROADCAST", "RUNNING"],
                 "em3": ["BROADCAST"], "lo0": ["UP", "LOOPBACK", "RUNNING"], "tun_wg0": ["UP", "POINTOPOINT", "RUNNING"]}
        return json.dumps([{"name": name, "flags": flags[name], "mtu": 1420 if name == "tun_wg0" else 1500, "addresses": []}
                           for name in names]).encode()
    nics = rows("em0", "em1", "em2", "em3", "lo0", "tun_wg0")
    seen = []

    def observe(request):
        if request.url == path and request.method != "GET":
            seen.append(request.method)

    def grants(privileges, scope):
        doc = ET.fromstring(docker("exec", container, *config, "backup"))
        user = doc.find("./system/user")
        for token in user.findall("priv"):
            user.remove(token)
        for value in ["page-system-restapi-keys", *privileges]:
            ET.SubElement(user, "priv").text = value
        doc.find("./restapi/apitoken/scopes").text = scope
        docker("exec", "-i", container, *config, "restore", input=ET.tostring(doc))

    def login():
        page.goto(origin + "/interfaces/assignments")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Interface Assignments", exact=True)).to_be_visible()

    def read():
        response = page.request.get(path, headers=auth)
        assert response.status == 200
        return response.json()["data"]

    def put(rev, device, headers=secret):
        return page.request.put(path, headers=headers, data={"revision": rev, "device": device})

    # This harness owns the disposable Linux web namespace. The native helper is supplied solely for this fixture.
    helper = "/usr/local/sbin/freesense-sysinfo"
    docker("exec", container, "test", "!", "-e", helper)
    docker("exec", "-i", container, "sh", "-c", "mkdir -p /usr/local/sbin && cat > " + helper + " && chmod 755 " + helper, input=mock)
    docker("exec", "-i", container, "sh", "-c", "cat > /check/assignment-nics", input=nics)
    page.on("request", observe)
    try:
        grant = ["page-interfaces-assignnetworkports"]
        grants(grant, "interfaces.assignments:write")
        assert page.request.get(path).status == 401
        login()
        start = read()
        assert start["availableDevices"] == ["em2", "em3"] and start["nextRole"] == "opt1", start
        page.get_by_role("button", name="Add interface", exact=True).click()
        dialog = page.locator("dialog[open]")
        combo = dialog.get_by_label("Network port", exact=True)
        expect(combo).to_have_value("em2")
        if evidence:
            page.screenshot(path=str(evidence / "assignments-before.png"))
        assert put(start["revision"], "lo0").status == 422
        assert put(start["revision"], "em0").status == 422
        assert put(start["revision"], "tun_wg0").status == 422
        assert page.request.post(path, headers=secret, data={"device": "em2"}).status == 405
        assert put(start["revision"], "em2", auth | {"Origin": origin}).status in (400,403,415)
        with page.expect_response(lambda r: r.url == path and r.request.method == "PUT") as added:
            dialog.get_by_role("button", name="Add", exact=True).click()
        assert added.value.status == 200 and added.value.json()["data"] == {"role": "opt1", "device": "em2"}
        expect(page.locator("#feedback")).to_contain_text("Interface OPT1 has been added.")
        expect(page.get_by_role("row").filter(has_text="OPT1")).to_contain_text("em2")
        assert put(start["revision"], "em3").status == 409
        if evidence:
            page.screenshot(path=str(evidence / "assignments-after.png"))
        # A late installation appears in the live catalog and invalidates outstanding approvals.
        prior = read()
        docker("exec", "-i", container, "sh", "-c", "cat > /check/assignment-nics", input=rows("em0", "em1", "em2", "em3", "lo0"))
        assert read() == prior
        docker("exec", container, "cp", "-R", "/optional/net/FreeSense-pkg-WireGuard/files/usr/local/.", "/check/prefix/")
        installed = read()
        assert "tun_wg0" in installed["availableDevices"] and prior["revision"] != installed["revision"]
        assert put(prior["revision"], "tun_wg0").status == 409
        assert put(installed["revision"], "tun_wg0").json()["data"] == {"role": "opt2", "device": "tun_wg0"}
        page.get_by_role("link", name="Overview", exact=True).click()
        expect(page).to_have_url(origin + "/overview")
        page.go_back()
        expect(page).to_have_url(origin + "/interfaces/assignments")
        expect(page.get_by_role("row").filter(has_text="OPT2")).to_contain_text("tun_wg0")
        grants(grant, "interfaces.assignments:read")
        login()
        expect(page.get_by_role("button", name="Add interface", exact=True)).to_have_count(0)
        current = read()
        assert put(current["revision"], "em3").status == 403
        assert "addOptAssignment" not in page.request.get(origin + "/api/v1/forms", headers=auth).json()["data"]["operations"]
        grants(["page-interfaces"], "interfaces.assignments:write")
        assert page.request.get(path, headers=auth).status == 403
        assert seen == ["PUT"]
        if evidence:
            (evidence / "assignments.json").write_text(json.dumps(
                {"created": ["OPT1 em2", "OPT2 tun_wg0"], "stale": 409, "readonly": 403,
                 "ungranted": 403, "legacyPost": 405, "browserWrites": seen}, indent=2), encoding="utf-8")
    finally:
        page.remove_listener("request", observe)
        docker("exec", container, "rm", "-f", helper, "/check/assignment-nics")
        docker("exec", "-i", container, *config, "restore", input=original)
