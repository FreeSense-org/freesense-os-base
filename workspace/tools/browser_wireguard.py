"""Exercise the installed WireGuard package pages through the shared System AJAX router."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import base64
import secrets
from xml.etree import ElementTree as ET

from browser_shell import menu_link, close_menu, fill_api_key
from playwright.sync_api import expect


def exercise_wireguard(page, origin, credential, container, docker, evidence=None):
    config = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *config, "backup")
    docker("exec", container, "cp", "-R", "/optional/net/FreeSense-pkg-WireGuard/files/usr/local/.", "/check/prefix/")
    # This namespace has no FreeBSD kernel. Supply only the two key operations used when saving.
    docker("exec", container, "sh", "-ec", "test ! -e /usr/bin/wg && install -m 0700 /optional/tests/fixtures/wg-test-tool /usr/bin/wg")
    endpoint = origin + "/api/v1/packages/"
    auth = {"Authorization": "Bearer " + credential}
    tabs = page.get_by_role("navigation", name="WireGuard", exact=True)
    writes = []

    def observe(request):
        if request.method == "PUT" and request.url.startswith(endpoint):
            writes.append(request.url)

    def get(slug):
        response = page.request.get(endpoint + slug, headers=auth)
        assert response.status == 200
        return response.json()["data"]["values"]

    def api(path, operation, body):
        return page.request.put(endpoint + path, data=body, headers=auth | {"Origin": origin, "X-FreeSense-Command": operation})

    def submit(label, route, expected=200):
        with page.expect_response(lambda response: response.url == endpoint + route and response.request.method == "PUT") as result:
            page.get_by_role("button", name=label, exact=True).click()
        assert result.value.status == expected, result.value.status
        if expected == 200:
            expect(page.locator("#feedback")).to_contain_text("Changes saved.")
        return result.value

    def signin(grants, scopes, path):
        doc = ET.fromstring(docker("exec", container, *config, "backup"))
        user = doc.find("./system/user")
        for node in user.findall("priv"):
            user.remove(node)
        for name in ["page-system-restapi-keys", *grants]:
            ET.SubElement(user, "priv").text = name
        doc.find("./restapi/apitoken/scopes").text = scopes
        interfaces = doc.find("./interfaces")
        if interfaces.find("opt1") is None:
            interfaces.append(ET.fromstring("<opt1><enable/><if>tun_wg0</if></opt1>"))
        docker("exec", "-i", container, *config, "restore", input=ET.tostring(doc))
        page.goto(origin + path)
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()

    management = "page-vpn-wireguard"
    status = "page-status-wireguard"
    write = "packages.wireguard:write"
    observe_status = "packages.wireguard-status:read"
    peer_path = "wireguard-peers/peers/save"
    peer_op = "package.wireguard-peers.peer"
    toggle_path = "wireguard-peers/peers/enabled"
    toggle_op = "package.wireguard-peers.setPeerEnabled"
    public = base64.b64encode(secrets.token_bytes(32)).decode()
    psk = base64.b64encode(secrets.token_bytes(32)).decode()
    page.on("request", observe)
    try:
        signin([management, status], write + " " + observe_status, "/vpn/wireguard")
        expect(menu_link(page, "VPN", "VPN: WireGuard")).to_have_attribute("href", "/vpn/wireguard")
        expect(menu_link(page,"Status","Status: WireGuard")).to_have_attribute("href", "/status/wireguard")
        close_menu(page)
        expect(tabs.get_by_role("link")).to_have_count(4)
        expect(tabs.get_by_role("link", name="Tunnels")).to_have_attribute("aria-current", "page")
        page.get_by_role("button", name="Add tunnel", exact=True).click()
        expect(page.get_by_role("combobox", name="Assigned interface")).to_have_value("opt1")
        page.get_by_label("Enable tunnel").check()
        page.get_by_label("Description", exact=True).fill("Branch tunnel")
        page.get_by_label("Tunnel Address", exact=True).fill("10.88.0.1/24")
        first = submit("Save tunnel", "wireguard/tunnel").json()["data"]
        identity = first["tunnel"]["publicKey"]
        assert len(identity) == 44 and first["tunnel"]["role"] == "opt1"
        expect(page.get_by_role("button", name="Edit tunnel")).to_be_visible()

        tabs.get_by_role("link", name="Peers", exact=True).click()
        page.get_by_role("button", name="Add peer", exact=True).click()
        dynamic = page.get_by_label("Dynamic Endpoint", exact=True)
        expect(dynamic).to_be_checked()
        expect(page.get_by_label("Endpoint address or hostname", exact=True)).to_be_hidden()
        dynamic.uncheck()
        page.get_by_label("Description", exact=True).fill("Branch router")
        expect(page.get_by_label("Enable peer", exact=True)).to_be_checked()
        key = page.get_by_label("Public Key", exact=True)
        key.fill(base64.b64encode(bytes(32)).decode())
        page.get_by_label("Endpoint address or hostname", exact=True).fill("198.51.100.2")
        allowed = page.get_by_label("Allowed IPs 1", exact=True)
        allowed.fill("10.88.0.2/32")
        before = len(writes)
        page.get_by_role("button", name="Save peer").click()
        expect(page.locator('[data-error-for="publicKey"]')).to_contain_text("format")
        key.fill(public)
        allowed.fill("10.88.0.2/24")
        page.get_by_role("button", name="Save peer").click()
        expect(page.locator('[data-error-for="allowedIps"]')).to_contain_text("format")
        assert len(writes) == before
        allowed.fill("10.88.0.2/32")
        body = {"revision": first["revision"], "description": "Branch router", "publicKey": public,
                "endpoint": "198.51.100.2", "endpointPort": 51820, "allowedIps": ["10.88.0.2/32"], "keepalive": 0}
        for invalid, field in (({"allowedIps": ["10.88.0.2/24"]}, "allowedIps"), ({"publicKey": "!" * 44}, "publicKey"),
                               ({"allowedIps": ["10.89.0.2/32"]}, "allowedIps"), ({"presharedKey": psk, "clearPreshared": True}, "presharedKey")):
            reply = api(peer_path, peer_op, body | invalid)
            assert reply.status == 422 and field in reply.json()["error"]["fields"] and psk not in reply.text()

        include = page.get_by_label("Include Preshared Key", exact=True)
        secret = page.get_by_label("Preshared Key", exact=True)
        expect(secret).to_have_attribute("type", "password")
        include.check()
        secret.fill(psk)
        include.uncheck()
        assert secret.input_value() == ""
        include.check()
        secret.fill(psk)
        reply = submit("Save peer", peer_path)
        assert psk not in reply.text()
        peers = get("wireguard-peers")
        assert peers["peers"][0]["hasPresharedKey"] and "presharedKey" not in peers["peers"][0]
        expect(page.get_by_role("cell", name="Configured", exact=True)).to_be_visible()
        row = peers["peers"][0]
        assert row["enabled"]
        old = {"revision": peers["revision"], "id": row["id"], "enabled": False}
        wrong = api(toggle_path, toggle_op, old | {"enabled": "false"})
        assert wrong.status == 422 and "enabled" in wrong.json()["error"]["fields"]
        submit("Disable Branch router", toggle_path)
        disabled = get("wireguard-peers")
        assert disabled["peers"][0] == (row | {"enabled": False}) and psk not in str(disabled)
        expect(page.get_by_role("cell", name="Disabled", exact=True)).to_be_visible()
        assert api(toggle_path, toggle_op, old).status == 409
        if evidence:
            page.screenshot(path=str(evidence / "wireguard-peer-disabled.png"))
        page.get_by_role("button", name="Edit Branch router", exact=True).click()
        expect(page.get_by_label("Enable peer", exact=True)).not_to_be_checked()
        page.get_by_label("Enable peer", exact=True).check()
        submit("Save peer", peer_path)
        assert get("wireguard-peers") == peers
        submit("Disable Branch router", toggle_path)
        submit("Enable Branch router", toggle_path)
        assert get("wireguard-peers") == peers
        tabs.get_by_role("link", name="Tunnels", exact=True).click()
        page.get_by_role("button", name="Edit tunnel", exact=True).click()
        page.get_by_label("Enable tunnel", exact=True).uncheck()
        submit("Save tunnel", "wireguard/tunnel")
        tabs.get_by_role("link", name="Peers", exact=True).click()
        expect(page.get_by_role("cell", name="Inactive", exact=True)).to_be_visible()
        signin([management], "packages.wireguard:read", "/vpn/wireguard/peers")
        expect(page.get_by_role("cell", name="Inactive", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Disable Branch router", exact=True)).to_have_count(0)
        assert api(toggle_path, toggle_op, old).status == 403
        signin([management, status], write + " " + observe_status, "/vpn/wireguard")
        page.get_by_role("button", name="Edit tunnel", exact=True).click()
        page.get_by_label("Enable tunnel", exact=True).check()
        submit("Save tunnel", "wireguard/tunnel")
        tabs.get_by_role("link", name="Peers", exact=True).click()
        expect(page.get_by_role("cell", name="Enabled", exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=str(evidence / "wireguard-peers.png"))
            tabs.get_by_role("link", name="Tunnels", exact=True).click()
            expect(page.get_by_role("cell", name="Branch router", exact=True)).to_be_visible()
            viewport = page.viewport_size
            page.set_viewport_size({"width": 1568, "height": 677})
            page.screenshot(path=str(evidence / "wireguard-tunnels.png"))
            page.set_viewport_size(viewport)
            tabs.get_by_role("link", name="Peers", exact=True).click()
        page.get_by_role("button", name="Edit Branch router", exact=True).click()
        expect(secret).to_have_value("")
        expect(include).not_to_be_checked()
        page.get_by_label("Description", exact=True).fill("Branch gateway")
        submit("Save peer", peer_path)
        assert get("wireguard")["peers"][0]["hasPresharedKey"]
        page.get_by_role("button", name="Edit Branch gateway", exact=True).click()
        page.get_by_label("Clear existing preshared key", exact=True).check()
        expect(secret).to_be_hidden()
        submit("Save peer", peer_path)
        current = get("wireguard")
        assert not current["peers"][0]["hasPresharedKey"] and current["tunnel"]["publicKey"] == identity

        tabs.get_by_role("link", name="Settings", exact=True).click()
        refused = submit("Apply saved settings", "wireguard-settings/apply", 409)
        assert refused.json()["error"]["code"] == "activation_conflict"
        expect(page.get_by_text("Confirm the System firewall policy", exact=False)).to_be_visible()
        rotate = "wireguard-settings/identity/rotate"
        assert api(rotate, "package.wireguard-settings.rotate", {"revision": current["revision"], "publicKey": public}).status == 422
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.get_by_role("button", name="Replace tunnel identity").click()
        assert get("wireguard")["tunnel"]["publicKey"] == identity
        page.once("dialog", lambda dialog: dialog.accept())
        submit("Replace tunnel identity", rotate)
        assert get("wireguard")["tunnel"]["publicKey"] != identity
        assert api(peer_path, peer_op, body).status == 409
        tabs.get_by_role("link", name="Peers", exact=True).click()
        page.get_by_role("button", name="Edit Branch gateway").click()
        page.once("dialog", lambda dialog: dialog.accept())
        submit("Remove peer", "wireguard-peers/peers/remove")
        expect(page.get_by_text("No peers configured.", exact=True)).to_be_visible()

        signin([management], "packages.wireguard:read", "/vpn/wireguard")
        expect(tabs.get_by_role("link")).to_have_count(3)
        expect(page.get_by_role("button", name="Save tunnel")).to_have_count(0)
        assert api(peer_path, peer_op, body).status == 403
        assert page.request.get(endpoint + "wireguard-status", headers=auth).status == 403
        signin([status], observe_status, "/status/wireguard")
        expect(tabs.get_by_role("link")).to_have_count(1)
        expect(page.get_by_role("link", name="VPN: WireGuard", exact=True)).to_have_count(0)
        native = get("wireguard-status")
        assert "tunnel" not in native and "revision" not in native
        state = "Unavailable" if not native["available"] else ("Running" if native["running"] else "Stopped")
        expect(page.locator("#content dd").first).to_have_text(state)
        for slug in ("wireguard", "wireguard-peers", "wireguard-settings"):
            assert page.request.get(endpoint + slug, headers=auth).status == 403
        assert api(peer_path, peer_op, body).status == 403
        assert api(toggle_path, toggle_op, old).status == 403
        assert page.request.get(endpoint + "wireguard-status").status == 401
        # The portable namespace cannot bring up a FreeBSD tunnel. Render the documented public observations
        # through the same routed component, including a byte count above JS's safe integer range.
        status_url = endpoint + "wireguard-status"
        def public_observation(route):
            response = route.fetch()
            payload = response.json()
            payload["data"]["values"] = {"available": True, "running": True, "interface": "tun_wg0",
                "publicKey": identity, "listenPort": 51820, "peers": [{"publicKey": public, "endpoint": "198.51.100.2:51820",
                "allowed": ["10.88.0.2/32"], "keepalive": 25, "lastHandshake": 0,
                "received": "9007199254740993", "sent": "321"}], "resolution": [{"description":"Branch","name":"branch.example.test",
                "address":"198.51.100.2","answers":[],"state":"Retained after lookup failure","checkedAt":1800000000}]}
            route.fulfill(response=response, json=payload)
        page.route(status_url, public_observation)
        try:
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.get_by_role("cell", name="9007199254740993", exact=True)).to_be_visible()
            expect(page.get_by_role("cell", name="Never", exact=True)).to_be_visible()
            expect(page.get_by_role("cell", name="Retained after lookup failure", exact=True)).to_be_visible()
            expect(page.get_by_role("cell", name="10.88.0.2/32", exact=True)).to_be_visible()
            if evidence:
                page.screenshot(path=str(evidence / "wireguard-status-rendering.png"))
        finally:
            page.unroute(status_url, public_observation)
        # Configure the public System owner using this private portable fixture. Native routing belongs to VNET tests.
        doc = ET.fromstring(docker("exec", container, *config, "backup"))
        for role, address in (("lan", "2001:db8:1::1"), ("wan", "2001:db8:2::1")):
            link = doc.find("./interfaces/" + role)
            for field, value in (("ipaddrv6", address), ("subnetv6", "64")):
                node = link.find(field)
                if node is None: node = ET.SubElement(link, field)
                node.text = value
        gateway = doc.find("./interfaces/wan/upstream6")
        if gateway is None: gateway = ET.SubElement(doc.find("./interfaces/wan"), "upstream6")
        gateway.text = "fe80::ff"
        if doc.find("routing6") is None:
            ET.SubElement(ET.SubElement(doc, "routing6"), "enable")
        docker("exec", "-i", container, *config, "restore", input=ET.tostring(doc))
        signin([management], write, "/vpn/wireguard")
        page.get_by_role("button", name="Edit tunnel", exact=True).click()
        page.get_by_label("Include Additional tunnel address", exact=True).check()
        page.get_by_label("Additional tunnel address", exact=True).fill("fd12:40::1/64")
        submit("Save tunnel", "wireguard/tunnel")
        expect(page.get_by_role("cell", name="10.88.0.1/24, fd12:40::1/64", exact=True)).to_be_visible()
        tabs.get_by_role("link", name="Peers", exact=True).click()
        page.get_by_role("button", name="Add peer", exact=True).click()
        dynamic = page.get_by_label("Dynamic Endpoint", exact=True)
        expect(dynamic).to_be_checked()
        expect(page.get_by_label("Endpoint address or hostname", exact=True)).to_be_hidden()
        dynamic.uncheck()
        page.get_by_label("Description", exact=True).fill("Dual-stack branch")
        page.get_by_label("Public Key", exact=True).fill(public)
        endpoint_input = page.get_by_label("Endpoint address or hostname", exact=True)
        endpoint_input.fill("fe80::2%em1")
        page.get_by_label("Allowed IPs 1", exact=True).fill("fd12:40::2/128")
        before = len(writes)
        page.get_by_role("button", name="Save peer", exact=True).click()
        expect(page.locator('[data-error-for="endpoint"]')).to_contain_text("format")
        assert len(writes) == before
        endpoint_input.fill("2001:0db8:2:0::2")
        page.get_by_role("button", name="Add item", exact=True).click()
        page.get_by_label("Allowed IPs 2", exact=True).fill("10.88.0.2/32")
        submit("Save peer", peer_path)
        expect(page.get_by_role("cell", name="[2001:db8:2::2]:51820", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="fd12:40::2/128, 10.88.0.2/32", exact=True)).to_be_visible()
        both = get("wireguard-peers")
        record = both["peers"][0]
        assert record["allowedIps"] == ["fd12:40::2/128", "10.88.0.2/32"]
        duplicate = body | {"revision": both["revision"], "id": record["id"],
                            "allowedIps": ["fd12:40::2/128", "fd12:0040:0:0:0:0:0:2/128"]}
        response = api(peer_path, peer_op, duplicate)
        assert response.status == 422 and "allowedIps" in response.json()["error"]["fields"]
        if evidence:
            page.screenshot(path=evidence / "wireguard-dual-stack-peer.png")
        page.get_by_role("button", name="Edit Dual-stack branch", exact=True).click()
        site4, site6 = "10.201.0.0/24", "fd12:71::/64"
        before = len(writes)
        page.get_by_role("button", name="Add item", exact=True).click()
        third = page.get_by_label("Allowed IPs 3", exact=True)
        third.fill("10.201.0.3/24")
        page.get_by_role("button", name="Save peer", exact=True).click()
        expect(page.locator('[data-error-for="allowedIps"]')).to_contain_text("format")
        assert len(writes) == before
        third.fill(site4)
        page.get_by_role("button", name="Add item", exact=True).click()
        page.get_by_label("Allowed IPs 4", exact=True).fill(site6)
        submit("Save peer", peer_path)
        both = get("wireguard-peers")
        assert both["peers"][0]["allowedIps"] == record["allowedIps"] + [site4, site6]
        expect(page.get_by_role("cell", name=", ".join(both["peers"][0]["allowedIps"]), exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / "wireguard-site-prefixes.png")
        rejected = api(peer_path, peer_op, body | {"revision": both["revision"], "id": record["id"],
                       "allowedIps": record["allowedIps"] + ["192.0.2.0/24"]})
        assert rejected.status == 422 and "allowedIps" in rejected.json()["error"]["fields"]
        assert get("wireguard-peers") == both
        page.get_by_role("button", name="Edit Dual-stack branch", exact=True).click()
        expect(dynamic).not_to_be_checked()
        endpoint_input.fill("Branch.Example.Test.")
        submit("Save peer", peer_path)
        expect(page.get_by_role("cell", name="branch.example.test:51820", exact=True)).to_be_visible()
        tabs.get_by_role("link", name="Settings", exact=True).click()
        interval = page.get_by_label("Endpoint Hostname Resolve Interval (seconds)", exact=True)
        expect(interval).to_have_value("300")
        interval.fill("0")
        submit("Save resolve interval", "wireguard-settings/resolution")
        assert get("wireguard-settings")["tunnel"]["resolveInterval"] == 0
        track = page.get_by_label("Track System Resolve Interval", exact=True)
        track.check()
        expect(interval).to_be_hidden()
        submit("Save resolve interval", "wireguard-settings/resolution")
        assert get("wireguard-settings")["effectiveResolveInterval"] == 300
        settings_path = origin + "/api/v1/system/advanced/firewall"
        assert page.request.get(settings_path, headers=auth).status == 403
        signin(["page-system-advanced-firewall"], "system.settings:write", "/system/advanced/firewall")
        system_interval = page.get_by_label("Aliases Hostnames Resolve Interval", exact=True)
        system_interval.wait_for(state="attached")
        page.evaluate("document.querySelectorAll('form details').forEach(panel => { panel.open = true; })")  # accordion panels start closed
        expect(system_interval).to_have_value("")  # empty means the default of 300 seconds
        sys_old = page.request.get(settings_path, headers=auth).json()["data"]
        system_interval.fill("120")
        with page.expect_response(lambda r: r.url == settings_path and r.request.method == "PUT") as changed:
            page.get_by_role("button", name="Save", exact=True).click()
        assert changed.value.status == 200
        expect(system_interval).to_have_value("120")
        if evidence: page.screenshot(path=evidence / "system-hostname-interval.png")
        write_headers = auth | {"Origin":origin, "X-FreeSense-Command":"saveFirewallOptions"}
        stale_body = {k:v for k,v in sys_old.items() if k != "defaults"} | {"aliasesresolveinterval":"90","confirm_disablefilter":False}
        assert page.request.put(settings_path, headers=write_headers,data=stale_body).status == 409
        signin(["page-system-advanced-firewall"], "system.settings:read", "/system/advanced/firewall")
        expect(page.get_by_text("Your key does not allow changing the firewall options.")).to_be_visible()
        assert page.request.put(settings_path, headers=write_headers,data=stale_body).status == 403
        signin(["page-firewall-aliases","page-firewall-alias-edit"], "firewall.aliases:write", "/firewall/aliases")
        alias_field = page.get_by_label("Members 1", exact=True)
        page.get_by_label("Alias name",exact=True).fill("OfficeHost")
        alias_field.fill("branch..example")
        page.get_by_role("button",name="Save alias",exact=True).click()
        expect(page.get_by_role("group",name="Members",exact=True)).to_have_attribute("aria-invalid","true")
        alias_field.fill("Office.Example.Test.")
        alias_url = origin + "/api/v1/firewall/aliases"
        with page.expect_response(lambda r: r.url == alias_url and r.request.method == "PUT") as saved_host:
            page.get_by_role("button",name="Save alias",exact=True).click()
        assert saved_host.value.status == 200
        expect(alias_field).to_have_value("office.example.test")
        if evidence: page.screenshot(path=evidence / "firewall-hostname-alias.png")
        signin([management], write, "/vpn/wireguard/settings")
        expect(page.get_by_text("Following the System alias resolver interval of 120 seconds.")).to_be_visible()
        assert get("wireguard-settings")["tunnel"]["resolveInterval"] == 0
        if evidence: page.screenshot(path=evidence / "wireguard-system-interval.png")
        tabs.get_by_role("link", name="Peers", exact=True).click()
        page.get_by_role("button", name="Edit Dual-stack branch", exact=True).click()
        expect(endpoint_input).to_have_value("branch.example.test")
        dynamic.check()
        expect(page.get_by_label("Endpoint port", exact=True)).to_be_hidden()
        submit("Save peer", peer_path)
        both = get("wireguard-peers")
        roaming = both["peers"][0]
        assert roaming["dynamic"] and "endpoint" not in roaming and "endpointPort" not in roaming
        expect(page.get_by_role("cell", name="Dynamic", exact=True)).to_be_visible()
        rejected = api(peer_path, peer_op, body | {"revision": both["revision"], "id": roaming["id"], "dynamic": True})
        assert rejected.status == 422 and "endpoint" in rejected.json()["error"]["fields"]
        assert get("wireguard-peers") == both
        if evidence:
            page.screenshot(path=evidence / "wireguard-roaming-peer.png")
        tabs.get_by_role("link", name="Tunnels", exact=True).click()
        page.get_by_role("button", name="Edit tunnel", exact=True).click()
        expect(page.get_by_label("Additional tunnel address", exact=True)).to_have_value("fd12:40::1/64")
        if evidence:
            page.screenshot(path=evidence / "wireguard-dual-stack-tunnel.png")
        page.get_by_label("Clear additional tunnel address", exact=True).check()
        response = submit("Save tunnel", "wireguard/tunnel", 422)
        fields = response.json()["error"]["fields"]
        assert "clearAdditional" in fields, list(fields)
        assert get("wireguard-peers") == both, "Rejected removal changed the saved tunnel or peers."
        print("WireGuard Chromium/PHP passed dual-stack tunnel and peer editors with client, server and relational IPv6 validation.")
        print("WireGuard Chromium/PHP passed four routes, role-selected tunnel, per-peer state, stale and unauthorized toggles, key validation and masking, identity rotation, confirmed-policy gate, revision conflicts and separate status permissions.")
    finally:
        page.remove_listener("request", observe)
        docker("exec", "-i", container, *config, "restore", input=original)
