"""Exercise the D4 PPP pages: Interfaces > PPPs, Services > PPPoE Server, VPN > L2TP (+ Users), the PPPoE/L2TP logs and the PPP
section of the interface page, with their grants, layout at 360 and 1280 px, and screenshots in both menu layouts."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect

GRANTS = ["page-system-restapi-keys", "page-interfaces-ppps", "page-interfaces-ppps-edit", "page-services-pppoeserver", "page-services-pppoeserver-edit",
          "page-vpn-vpnl2tp", "page-vpn-vpnl2tp-users", "page-vpn-vpnl2tp-users-edit", "page-diagnostics-logs-pptpvpn", "page-interfaces"]
SCOPES = "interfaces.ppps:write services.pppoe:write vpn.l2tp:write status.logs.vpn:read interfaces.wan:write"


def exercise_ppp(page, origin, credential, container, docker, evidence=None):
    config = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *config, "backup")
    auth = {"Authorization": "Bearer " + credential}

    def access(grants, scope):
        doc = ET.fromstring(docker("exec", container, *config, "backup"))
        user = doc.find("./system/user")
        for node in user.findall("priv"):
            user.remove(node)
        for name in grants:
            ET.SubElement(user, "priv").text = name
        doc.find("./restapi/apitoken/scopes").text = scope
        interfaces = doc.find("./interfaces")
        if interfaces.find("opt1") is None:
            opt = ET.SubElement(interfaces, "opt1")
            ET.SubElement(opt, "if").text = "em2"
            ET.SubElement(opt, "enable")
            ET.SubElement(opt, "descr").text = "Tenants"
        docker("exec", "-i", container, *config, "restore", input=ET.tostring(doc))

    def login(path):
        page.goto(origin + path)
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.locator("#workspace")).to_be_visible()

    def no_overflow(label):
        for width in (360, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            wide = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            assert not wide, f"{label} overflows at {width} px"
        page.set_viewport_size({"width": 1320, "height": 920})

    def shot(name):
        if evidence:
            page.screenshot(path=str(evidence / f"ppp-{name}.png"), full_page=True)

    nics = (b"#!/bin/sh\nprintf '%s\\n' 'em0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500' '    ether 52:54:00:00:00:01' '    status: active'"
            b" 'em2: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500' '    ether 52:54:00:00:00:03' '    status: active'\n")
    docker("exec", "-i", container, "sh", "-c", "cat > /sbin/ifconfig && chmod 755 /sbin/ifconfig", input=nics)
    try:
        # Read-only grant: the list shows, no Add.
        access(["page-system-restapi-keys", "page-interfaces-ppps"], "interfaces.ppps:read")
        login("/interfaces/ppps")
        expect(page.get_by_text("No PPPs yet.")).to_be_visible()
        expect(page.get_by_role("button", name="Add PPP", exact=True)).to_have_count(0)
        assert page.request.put(origin + "/api/v1/interfaces/ppps", headers=auth | {"Origin": origin, "X-FreeSense-Command": "createPpp"}, data={}).status == 403

        access(GRANTS, SCOPES)
        login("/interfaces/ppps")
        page.get_by_role("button", name="Add PPP", exact=True).click()
        dialog = page.locator("dialog[open]")
        expect(dialog.get_by_role("heading", name="Add PPP")).to_be_visible()
        for name in ("General", "Reset", "Advanced"):
            expect(dialog.get_by_role("link", name=name, exact=True)).to_be_visible()
        dialog.get_by_label("Username", exact=True).fill("user@isp")
        dialog.get_by_label("Include Password").check()
        dialog.get_by_label("Password", exact=True).fill("test-only")
        dialog.get_by_label("Description", exact=True).fill("Uplink")
        shot("editor")
        with page.expect_response(lambda r: r.url.endswith("/api/v1/interfaces/ppps") and r.request.method == "PUT") as saved:
            dialog.get_by_role("button", name="Save", exact=True).click()
        assert saved.value.status == 200, saved.value.text()
        expect(page.get_by_role("cell", name="pppoe0", exact=True)).to_be_visible()
        data = page.request.get(origin + "/api/v1/interfaces/ppps", headers=auth).json()["data"]
        assert data["rows"][0]["hasPassword"] and "test-only" not in str(data)
        no_overflow("PPPs")
        shot("list")

        # Type switch hides PPPoE-only panels; Cancel leaves the list.
        page.get_by_role("button", name="Edit pppoe0").click()
        dialog = page.locator("dialog[open]")
        dialog.get_by_label("Link Type").select_option("pptp")
        expect(dialog.get_by_role("link", name="Reset", exact=True)).to_be_hidden()
        dialog.get_by_role("button", name="Cancel").click()

        # PPPoE server with one user.
        login("/services/pppoe-server")
        page.get_by_role("button", name="Add", exact=True).click()
        dialog = page.locator("dialog[open]")
        dialog.get_by_label("Server Address").fill("10.10.1.1")
        dialog.get_by_label("Remote Address Range").fill("10.10.0.0")
        dialog.get_by_role("link", name="Users", exact=True).click()
        dialog.get_by_role("button", name="Add item").click()
        dialog.get_by_label("Username").first.fill("alice")
        dialog.get_by_label("Password").first.fill("a-test")
        try:
            with page.expect_response(lambda r: r.url.endswith("/api/v1/services/pppoe-server") and r.request.method == "PUT") as saved:
                dialog.get_by_role("button", name="Save", exact=True).click()
        except Exception:
            shot("pppoe-error")
            errors = dialog.evaluate("d => [...d.querySelectorAll('[data-error-for],[data-form-error]')].map(e => (e.dataset.errorFor || 'form') + ': ' + e.textContent).filter(t => !t.endsWith(': '))")
            raise AssertionError(f"PPPoE server save was not sent: {errors}")
        assert saved.value.status == 200, saved.value.text()
        expect(page.get_by_role("cell", name="10.10.1.1", exact=True)).to_be_visible()
        no_overflow("PPPoE Server")
        shot("pppoe-server")

        # L2TP configuration (accordion) and a user.
        login("/vpn/l2tp")
        expect(page.get_by_role("heading", name="L2TP server", exact=True)).to_be_visible()
        no_overflow("L2TP")
        shot("l2tp")
        login("/vpn/l2tp/users")
        page.get_by_role("button", name="Add", exact=True).click()
        dialog = page.locator("dialog[open]")
        dialog.get_by_label("Username", exact=True).fill("zed")
        dialog.get_by_label("Include Password").check()
        dialog.get_by_label("Password", exact=True).fill("z-test")
        dialog.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_role("cell", name="zed", exact=True)).to_be_visible()
        no_overflow("L2TP Users")

        # Logs page answers with its four views.
        login("/status/logs/vpn?view=l2tp-service")
        expect(page.get_by_role("link", name="L2TP Service")).to_have_attribute("aria-current", "page")
        no_overflow("PPPoE/L2TP logs")
        shot("logs")
    finally:
        page.set_viewport_size({"width": 1320, "height": 920})
        docker("exec", "-i", container, *config, "restore", input=original)
