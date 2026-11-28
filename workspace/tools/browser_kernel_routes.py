"""Exercise the scoped Diagnostics Routes view using captured numeric FreeBSD route shapes."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_kernel_routes(page, origin, key, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *cli, "backup")
    index = "/check/prefix/FreeSense/web/index.php"
    docker("exec", container, "cp", index, "/check/original-routes-index")
    fixture = Path(__file__).resolve().parents[1] / "freesense/tests/fixtures/route-browser.php"
    write = lambda path, data: docker("exec", "-i", container, "php", "-r",
        "file_put_contents($argv[1],stream_get_contents(STDIN));", path, input=data)
    write(index, fixture.read_bytes())
    write("/check/route-reads", b"")
    write("/check/route-name-reads", b"")
    endpoint = origin + "/api/v1/diagnostics/routes"
    auth = {"Authorization": "Bearer " + key}
    base = {"flags": "UGS", "nhop-kidx": 12, "metric": 1, "weight": 1}
    def route(destination, gateway, interface):
        return base | {"destination": destination, "gateway": gateway, "interface-name": interface}
    def rows(family, records):
        write("/check/routes-" + str(family), json.dumps(records).encode())
    rows(4, [route("default", "192.0.2.254", "em0"), route("10.33.0.0/16", "198.51.100.2", "em1"),
             route("10.33.0.0/16", "198.51.100.3", "em1"), route("10.33.4.0/24", "198.51.100.3", "em1"),
             route("127.0.0.1", "link#1", "lo0")])
    v6 = [route("default", "fe80::2%em1", "em1"), route("2001:db8:5::/64", "fe80::2%em1", "em1"),
          route("fe80::%tap7/64", "link#7", "tap7")]
    rows(6, v6)
    def count():
        return docker("exec", container, "cat", "/check/route-reads").count(b"\n")
    def login(grant, scope, path):
        doc = ET.fromstring(original)
        user = doc.find("./system/user")
        for item in user.findall("priv"): user.remove(item)
        for name in ("page-system-restapi-keys", grant): ET.SubElement(user, "priv").text = name
        doc.find("./restapi/apitoken/scopes").text = scope
        docker("exec", "-i", container, *cli, "restore", input=ET.tostring(doc))
        page.goto(origin + path)
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in").click()
        expect(page.locator('#workspace')).to_be_visible()
    try:
        login("page-system-staticroutes", "diagnostics:read", "/overview")
        assert page.request.get(endpoint + "?family=bogus", headers=auth).status == 403 and count() == 0
        login("page-diagnostics-routingtables", "routing:read", "/overview")
        assert page.request.get(endpoint, headers=auth).status == 403 and count() == 0
        login("page-diagnostics-routingtables", "diagnostics:read", "/diagnostics/routes")
        expect(page.get_by_role("cell", name="192.0.2.254", exact=True)).to_be_visible()
        assert page.request.get(origin + "/api/v1/diagnostics/states", headers=auth).status == 403
        forms = page.request.get(origin + "/api/v1/forms", headers=auth).json()["data"]
        assert "routes" in forms["reads"] and "states" not in forms["reads"]
        assert set(forms["operations"]) <= {"saveAccountAppearance","saveDashboardLayout","resetDashboardLayout","revokeMyApiKey","bulkRevokeMyApiKeys","bulkDeleteMyApiKeys","revokeOwnApiKeys"}, sorted(forms["operations"])
        submit = page.get_by_role("button", name="Filter", exact=True)
        network = page.get_by_label("Destination in network", exact=True)
        iface = page.get_by_label("Interface", exact=True)
        fam = page.get_by_label("Address family", exact=True)
        limit = page.get_by_label("Row limit", exact=True)
        resolve = page.get_by_label("Resolve names", exact=True)
        expect(resolve).not_to_be_checked()
        assert docker("exec",container,"cat","/check/route-name-reads")==b""
        expect(page.get_by_role("list", name="Route flags")).to_be_visible()
        gateway = page.get_by_role("button",name="Gateway",exact=True)
        gateway.click()
        expect(page.get_by_role("columnheader",name="Gateway")).to_have_attribute("aria-sort","ascending")
        gateway.click()
        expect(page.get_by_role("columnheader",name="Gateway")).to_have_attribute("aria-sort","descending")
        resolve.check()
        submit.click()
        expect(page.get_by_text("primary-gateway.example",exact=True)).to_be_visible()
        expect(page.get_by_text("198.51.100.2",exact=True)).to_be_visible()
        assert "resolve=true" in page.url
        if evidence: page.screenshot(path=evidence / "kernel-routes-resolved.png", full_page=True)
        resolve.uncheck()
        submit.click()
        expect(page.get_by_text("primary-gateway.example",exact=True)).to_have_count(0)
        gateway.click()
        baseline = count()
        network.fill("2001:db8:5::/64")
        submit.click()
        expect(network).to_have_attribute("aria-invalid", "true")
        network.fill("10.33.5.1/16")
        submit.click()
        expect(network).to_have_attribute("aria-invalid", "true")
        assert count() == baseline
        page.goto(origin + "/diagnostics/routes?interface=wan&network=10.33.0.0%2F16&limit=1")
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in").click()
        expect(limit).to_have_value("1")
        expect(page.get_by_text("Showing 1 of 3 matching routes.", exact=True)).to_be_visible()
        assert "interface=wan" in page.url and "network=" in page.url
        expect(page.get_by_role("cell", name="198.51.100.2", exact=True)).to_be_visible()
        if evidence: page.screenshot(path=evidence / "kernel-routes-ipv4.png", full_page=True)
        expect(limit.locator("option[value='10']")).to_have_count(1)
        limit.select_option("all")
        submit.click()
        expect(page.get_by_role("row")).to_have_count(4)
        assert "allRows=true" in page.url and "limit=" not in page.url
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(limit).to_have_value("all")
        if evidence: page.screenshot(path=evidence / "kernel-routes-limits.png", full_page=True)
        fam.select_option("ipv6")
        iface.fill("all")
        network.fill("")
        limit.select_option("100")
        submit.click()
        expect(page.get_by_role("cell", name="fe80::%tap7/64", exact=True)).to_be_visible()
        expect(page.locator("#routes-interface-choices option[value=tap7]")).to_have_count(1)
        if evidence: page.screenshot(path=evidence / "kernel-routes-ipv6.png", full_page=True)
        iface.fill("tap7")
        submit.click()
        expect(page.get_by_role("cell", name="link#7", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="default", exact=True)).to_have_count(0)
        old = count()
        for url, expected in [("?family=ipv4&family=ipv6", 400), ("?network=fe80::1", 422), ("?interface=em1%2F2", 422), ("?limit=1001", 422), ("?resolve=yes", 422)]:
            assert page.request.get(endpoint + url, headers=auth).status == expected
        assert count() == old and page.request.put(endpoint, headers=auth).status == 405
        rows(6, [route("malformed", "link#1", "lo0")])
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("could not be read")
        rows(6, v6)
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_role("cell", name="link#7", exact=True)).to_be_visible()
        print("Chromium verified route-reader grants, actual dispatch, family/network/device filters, limits, sorting, opt-in resolution, failure and recovery.")
    finally:
        docker("exec", container, "cp", "/check/original-routes-index", index)
        docker("exec", "-i", container, *cli, "restore", input=original)
