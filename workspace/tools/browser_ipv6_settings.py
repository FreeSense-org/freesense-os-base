"""Verify the saved static IPv6 interface settings through Chromium and the real PHP router."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import re
from xml.etree import ElementTree

from browser_shell import menu_link
from playwright.sync_api import expect


def exercise_ipv6_settings(page, origin, credential, container, docker, evidence=None):
    endpoint = "/api/v1/interfaces/ipv6"
    headers = {"Authorization": "Bearer " + credential}
    command = headers | {"Origin": origin, "X-FreeSense-Command": "saveStaticIpv6"}

    def state():
        result = page.request.get(origin + endpoint, headers=headers)
        assert result.status == 200
        return result.json()["data"]

    def stored_xml():
        raw = docker("exec", container, "php", "/check/prefix/sbin/freesense-config", "backup")
        return ElementTree.canonicalize(xml_data=raw.decode(), strip_text=True)

    def write(fields):
        return page.request.put(origin + endpoint, headers=command, data=fields)

    def save():
        with page.expect_response(lambda r: r.url.endswith(endpoint) and r.request.method == "PUT") as result:
            submit.click()
        return result.value

    menu_link(page, "Interfaces", "Static IPv6").click()
    expect(page).to_have_url(origin + "/interfaces/ipv6")
    form = page.locator("form").filter(has=page.get_by_role("button", name="Save IPv6 settings", exact=True))
    checkbox = form.get_by_label("Enable static IPv6 routing", exact=True)
    revision = form.locator('[name="revision"]')
    submit = form.locator('button[type="submit"]')
    fields = {name: form.get_by_label(label, exact=True) for name, label in {
        "lanCidr": "LAN IPv6 address and prefix", "wanCidr": "WAN IPv6 address and prefix", "gateway": "WAN IPv6 gateway"
    }.items()}
    before = state()
    original_xml = stored_xml()
    original = ElementTree.fromstring(original_xml)
    assert original.findtext("./interfaces/lan/if") == "em0" and original.findtext("./interfaces/wan/if") == "em1"
    assert before["enabled"] is False
    expect(checkbox).not_to_be_checked()
    checkbox.check()
    values = {"lanCidr": "2001:db8:10::1/64", "wanCidr": "2001:db8:20::2/64", "gateway": "2001:db8:20::1"}
    for name, value in values.items():
        fields[name].fill(value)

    browser_writes = []

    def observe(request):
        if request.method == "PUT" and request.url.endswith(endpoint):
            browser_writes.append(request)

    page.on("request", observe)

    def invalid(changes, error_fields):
        for name, value in changes.items():
            fields[name].fill(value)
        submit.click()
        selector = ", ".join('[data-error-for="' + field + '"]' for field in error_fields)
        errors = form.locator(selector).filter(has_text=re.compile(r"\S"))
        expect(errors.first).to_be_visible()
        assert not browser_writes, "The browser submitted invalid IPv6 settings"
        result = write(values | changes | {"enabled": True, "revision": before["revision"]})
        assert result.status == 422 and set(result.json()["error"]["fields"]).intersection(error_fields)
        assert state() == before
        if evidence and changes.get("gateway") == "2001:db8:99::1":
            page.screenshot(path=str(evidence / "static-ipv6-gateway-error.png"), full_page=True)
        for name in changes:
            fields[name].fill(values[name])

    invalid({"lanCidr": "2001:db8:10::1/63"}, ["lanCidr"])
    invalid({"wanCidr": "2001:db8:10::2/64", "gateway": "2001:db8:10::3"}, ["lanCidr", "wanCidr"])
    invalid({"gateway": "2001:db8:20::2"}, ["gateway"])
    invalid({"gateway": "2001:db8:99::1"}, ["gateway"])
    page.remove_listener("request", observe)
    assert stored_xml() == original_xml

    saved = save()
    assert saved.status == 200
    current = state()
    assert current["enabled"] is True and all(current[key] == value for key, value in values.items())
    expect(revision).to_have_value(current["revision"])
    xml = ElementTree.fromstring(stored_xml())
    assert xml.find("./routing6/enable") is not None
    assert xml.findtext("./interfaces/lan/ipaddrv6") == "2001:db8:10::1"
    assert xml.findtext("./interfaces/wan/ipaddrv6") == "2001:db8:20::2"
    assert xml.findtext("./interfaces/lan/subnetv6") == xml.findtext("./interfaces/wan/subnetv6") == "64"
    assert xml.findtext("./interfaces/wan/upstream6") == values["gateway"]
    if evidence:
        page.screenshot(path=str(evidence / "static-ipv6.png"), full_page=True)

    # Two authenticated editors hold the same revision until one changes the saved gateway.
    peer_gateway = "fe80::1"
    peer = write(dict(values, gateway=peer_gateway, enabled=True, revision=current["revision"]))
    assert peer.status == 200
    fields["gateway"].fill("2001:db8:20::4")
    assert save().status == 409 and state()["gateway"] == peer_gateway

    menu_link(page,"Interfaces","WAN IPv4").click()
    menu_link(page, "Interfaces", "Static IPv6").click()
    expect(fields["gateway"]).to_have_value(peer_gateway)
    checkbox.uncheck()
    cleared = save()
    assert cleared.status == 200 and set(cleared.request.post_data_json) == {"revision", "enabled"}
    assert state() == before
    assert stored_xml() == original_xml
    print("IPv6 settings browser verified /64 prefixes, separate links, on-link gateway, persistence, a stale edit and clearing.")
