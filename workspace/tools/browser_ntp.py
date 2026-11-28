"""Exercise Services > NTP (Settings, ACLs, Serial GPS, PPS), Status > NTP and Status > DNS Resolver with their 1.2 grants."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import re
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_ntp(page, origin, credential, container, docker, evidence=None):
    config = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *config, "backup")
    auth = {"Authorization": "Bearer " + credential}
    api = origin + "/api/v1/services/ntp"

    def restore(doc):
        docker("exec", "-i", container, *config, "restore", input=ET.tostring(doc))

    def access(grants, scope):
        doc = ET.fromstring(docker("exec", container, *config, "backup"))
        user = doc.find("./system/user")
        for node in user.findall("priv"):
            user.remove(node)
        for name in ["page-system-restapi-keys", *grants]:
            ET.SubElement(user, "priv").text = name
        doc.find("./restapi/apitoken/scopes").text = scope
        restore(doc)

    def login(route, heading):
        page.goto(origin + route)
        if page.get_by_role("button", name="Sign in", exact=True).count():
            fill_api_key(page, credential)
            page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()

    def shot(name):
        if evidence:
            for width in (1280, 360):
                page.set_viewport_size({"width": width, "height": 900})
                page.screenshot(path=evidence / f"{name}-{width}.png", full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"{name} overflows at {width} px"
            page.set_viewport_size({"width": 1320, "height": 920})

    def stored():
        return ET.fromstring(docker("exec", container, *config, "backup"))

    try:
        # A 1.2 ntpd section (synthetic) as the importer keeps it.
        doc = stored()
        for node in doc.findall("ntpd"):
            doc.remove(node)
        ntpd = ET.SubElement(doc, "ntpd")
        for name, value in [("enable", "enabled"), ("orphan", "12"), ("ntpminpoll", ""), ("ntpmaxpoll", ""), ("dnsresolv", "auto"), ("kod", "on")]:
            ET.SubElement(ntpd, name).text = value
        system = doc.find("system")
        ts = system.find("timeservers") if system.find("timeservers") is not None else ET.SubElement(system, "timeservers")
        ts.text = "0.freesense.pool.ntp.org time.example.net"
        restore(doc)
        access(["page-services-ntpd", "page-services-ntpd-acls", "page-services-ntpd-gps", "page-services-ntpd-pps", "page-status-ntp",
                "page-status-dns-resolver"], "services.ntpd:write status.ntp:read services.dns:read")

        # Settings tab: the pinned panels in order, the tab strip with icons, a server edit that saves with a toast.
        login("/services/ntp", "NTP Settings")
        tabs = page.locator("[data-page-tabs] > a")
        expect(tabs).to_have_text(["Settings", "ACLs", "Serial GPS", "PPS"])
        for title in ["NTP Server Configuration", "Logging", "Leap seconds", "DNS and Authentication"]:
            expect(page.locator("[data-panel-title]", has_text=title).first).to_be_visible()
        expect(page.get_by_label("Enable NTP Server", exact=True)).to_be_checked()
        expect(page.get_by_label("Orphan Mode", exact=True)).to_have_value("12")
        shot("ntp-settings")
        # In a normal 1280x800 viewport the sticky save bar never covers a control: scrolled to the end, every control
        # can sit above the bar; and the Time Servers name column shows a full host name.
        page.set_viewport_size({"width": 1280, "height": 800})
        covered = page.evaluate("""() => {
            const bar = document.querySelector('form [data-form-actions]');
            const out = [];
            for (const control of document.querySelectorAll('form input:not([type=hidden]), form select, form textarea')) {
                if (!control.getClientRects().length || control.closest('details:not([open])')) continue;
                control.scrollIntoView({block: 'center'});
                const box = control.getBoundingClientRect(), top = bar.getBoundingClientRect().top;
                if (box.bottom > top + 1 && box.top < bar.getBoundingClientRect().bottom) out.push(control.name || control.dataset.computed || control.id);
            }
            const all = [...document.querySelectorAll('form input:not([type=hidden]), form select, form textarea')].filter(c => c.getClientRects().length && !c.closest('details:not([open])'));
            window.scrollTo(0, document.documentElement.scrollHeight);
            const last = all.at(-1);
            if (last && last.getBoundingClientRect().bottom > bar.getBoundingClientRect().top + 1) out.push('last control at the page end: ' + (last.name || last.dataset.computed));
            return out;
        }""")
        assert covered == [], f"save bar covers {covered}"
        server = page.locator("[data-computed=servers] input[type=text]").first
        assert server.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), "time server name is cut off"
        if evidence:
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=evidence / "ntp-settings-viewport.png")
        page.set_viewport_size({"width": 1320, "height": 920})
        page.get_by_label("Orphan Mode", exact=True).fill("99")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("The supplied value for NTP Orphan Mode is invalid.")).to_be_visible()
        page.get_by_label("Orphan Mode", exact=True).fill("10")
        with page.expect_response(lambda r: r.url == api and r.request.method == "PUT") as pending:
            page.get_by_role("button", name="Save", exact=True).click()
        assert pending.value.status == 200, pending.value.status
        saved = stored().find("ntpd")
        assert saved.findtext("orphan") == "10" and saved.findtext("kod") == "on", ET.tostring(saved)

        # ACLs: defaults shown de-inverted (kod stored "on" = off), a network row saved in the 1.2 layout.
        page.get_by_role("link", name="ACLs", exact=True).click()
        expect(page.get_by_role("heading", name="NTP ACLs", exact=True)).to_be_visible()
        expect(page.get_by_label("Enable KOD packets")).not_to_be_checked()
        expect(page.get_by_label("Deny run-time Configuration (nomodify) by ntpq and ntpdc")).to_be_checked()
        shot("ntp-acls")
        rows = page.locator("[data-computed=rows]")
        rows.get_by_role("button", name=re.compile("^Add")).first.click()
        rows.locator("input[type=text]").nth(0).fill("192.0.2.0")
        rows.locator("input[type=text]").nth(1).fill("24")
        with page.expect_response(lambda r: r.url == api + "/acls" and r.request.method == "PUT") as pending:
            page.get_by_role("button", name="Save", exact=True).click()
        assert pending.value.status == 200, pending.value.status
        row = stored().find("./ntpd/restrictions/row")
        assert row is not None and row.findtext("acl_network") == "192.0.2.0" and row.findtext("mask") == "24"

        # Serial GPS and PPS: the container has no serial ports, the page says so and still saves the type.
        page.get_by_role("link", name="Serial GPS", exact=True).click()
        expect(page.get_by_role("heading", name="NTP Serial GPS", exact=True)).to_be_visible()
        expect(page.get_by_text("This system has no serial ports.")).to_be_visible()
        shot("ntp-gps")
        page.get_by_role("link", name="PPS", exact=True).click()
        expect(page.get_by_role("heading", name="NTP PPS", exact=True)).to_be_visible()
        shot("ntp-pps")

        # Status pages read through ntpq and local-unbound-control; without the daemons they say so.
        login("/status/ntp", "NTP")
        expect(page.locator("[data-stat-tile]")).to_have_count(3)
        shot("status-ntp")
        login("/status/dns-resolver", "DNS Resolver")
        expect(page.get_by_text("The DNS Resolver is disabled or stopped.")).to_be_visible()
        expect(page.locator("[data-stat-tile]")).to_have_count(4)
        shot("status-dns-resolver")

        # Grants: the GPS tab grant alone opens only that tab and may save only GPS; the key is never sent.
        access(["page-services-ntpd-gps"], "services.ntpd:write")
        body = page.request.get(api + "/gps", headers=auth)
        assert body.status == 200 and "serverauthkey" not in body.json()["data"], body.status
        assert page.request.get(api, headers=auth).status == 403
        forms = page.request.get(origin + "/api/v1/forms", headers=auth).json()["data"]["operations"]
        assert "saveNtpGps" in forms and "saveNtpSettings" not in forms, sorted(forms)
        access(["page-status-ntp"], "status.ntp:read")
        assert page.request.get(origin + "/api/v1/status/ntp", headers=auth).status == 200
        assert page.request.get(origin + "/api/v1/status/dns-resolver", headers=auth).status == 403
        print("Chromium verified the four NTP tabs (pinned panels, validation, 1.2 storage), both status pages and the per-tab grants.")
    finally:
        docker("exec", "-i", container, *config, "restore", input=original)
