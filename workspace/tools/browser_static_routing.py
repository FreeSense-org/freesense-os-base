"""Check routed gateway and static route management with real Chromium and the production HTTP router."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_static_routing(page, origin, key, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    backup = docker("exec", container, *cli, "backup")
    site = "/system/routing/"
    endpoint = origin + "/api/v1/routing/"
    auth = {"Authorization": "Bearer " + key}
    gwlist, gwedit = "page-system-gateways", "page-system-gateways-editgateway"
    rtlist, rtedit = "page-system-staticroutes", "page-system-staticroutes-editroute"

    def configure(grants, scope="routing:write"):
        doc = ET.fromstring(docker("exec", container, *cli, "backup"))
        user = doc.find("./system/user")
        for privilege in user.findall("priv"):
            user.remove(privilege)
        for grant in ["page-system-restapi-keys", *grants]:
            ET.SubElement(user, "priv").text = grant
        doc.find("./restapi/apitoken/scopes").text = scope
        wan = doc.find("./interfaces/wan")
        for tag, value in {"ipaddr":"198.51.100.5", "subnet":"24", "upstream4":"198.51.100.1"}.items():
            child = wan.find(tag)
            if child is None:
                child = ET.SubElement(wan, tag)
            child.text = value
        docker("exec", "-i", container, *cli, "restore", input=ET.tostring(doc))

    def login(path):
        page.goto(origin + site + path)
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in", exact=True).click()

    def work(area, command, values):
        return page.request.put(endpoint + area, headers=auth | {"Origin":origin, "X-FreeSense-Command":command}, data=values)

    def click(area, button):
        with page.expect_response(lambda response: response.url == endpoint + area and response.request.method == "PUT") as submitted:
            button.click()
        return submitted.value

    def get(area):
        return page.request.get(endpoint + area, headers=auth)

    try:
        configure([gwlist, gwedit, rtlist, rtedit])
        login("gateways")
        expect(page.get_by_role("cell", name="WAN default IPv4", exact=True)).to_be_visible()
        page.get_by_role("link", name="Add gateway", exact=True).click()
        interface = page.get_by_role("combobox", name="Interface", exact=True)
        expect(interface).to_be_visible()
        interface.select_option("lan")
        page.get_by_label("Name", exact=True).fill("Laboratory")
        address = page.get_by_label("Gateway", exact=True)
        save = page.get_by_role("button", name="Save", exact=True)
        sent = []
        page.on("request", lambda r: sent.append((r.method, r.url)) if r.method in ("PUT", "POST") and r.url.startswith(endpoint) else None)
        address.fill("ff02::1")
        save.click()
        expect(address).to_have_attribute("aria-invalid", "true")
        assert not sent
        weight = page.get_by_role("spinbutton", name="Weight", exact=True)
        expect(weight).to_have_value("1")
        weight.fill("31")
        address.fill("192.0.2.31")
        save.click()
        expect(weight).to_have_attribute("aria-invalid", "true")
        assert not sent
        weight.fill("3")
        address.fill("192.0.2.31")
        page.get_by_label("Description", exact=True).fill("Gateway used by the branch")
        receipt = click("gateways", save)
        assert receipt.status == 200, receipt.text()
        gateway = receipt.json()["data"]
        assert set(gateway) == {"revision","id"}
        approved_draft = get("gateways/edit/" + gateway["id"]).json()["data"]["row"]
        assert approved_draft["weight"] == 3
        invalid = work("gateways/edit","editGateway",approved_draft | {"revision":gateway["revision"],"weight":0})
        assert invalid.status == 422 and "weight" in invalid.json()["error"]["fields"]

        set_default = work("gateways/edit","editGateway",approved_draft | {"revision":gateway["revision"],"weight":1})
        assert set_default.status == 200, set_default.text()
        row = get("gateways/edit/" + gateway["id"]).json()["data"]["row"]
        assert "weight" not in row and row["description"] == "Gateway used by the branch"
        gateway = set_default.json()["data"]

        expect(page.get_by_role("cell", name="Laboratory", exact=True)).to_be_visible()
        expect(page.get_by_text('Gateway changes are waiting to be applied.',exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / "gateways.png", full_page=True)
        page.get_by_role("navigation", name="Routing").get_by_role("link", name="Static Routes").click()
        page.get_by_role("link", name="Add static route", exact=True).click()
        destination = page.get_by_label("Destination network", exact=True)
        page.get_by_role("combobox", name="Gateway", exact=True).select_option(gateway["id"])
        count = len(sent)
        destination.fill("203.0.113.1/24")
        save.click()
        expect(destination).to_have_attribute("aria-invalid", "true")
        assert len(sent) == count
        destination.fill("192.0.2.74")
        blocked = click("routes", save)
        assert blocked.status == 422 and "destination" in blocked.json()["error"]["fields"]
        destination.fill("203.0.113.79")
        receipt = click("routes", save)
        assert receipt.status == 200, receipt.text()
        route = receipt.json()["data"]
        expect(page.get_by_role("cell", name="203.0.113.79/32", exact=True)).to_be_visible()
        expect(page.get_by_text('Static route changes are waiting to be applied.',exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / "static-routes.png", full_page=True)
        assert work("routes/remove","removeStaticRoute",{"revision":gateway["revision"],"id":route["id"]}).status == 409
        assert work("gateways/remove","removeGateway",{"revision":route["revision"],"id":gateway["id"]}).status in (409, 422)

        configure([rtedit])
        login("routes/edit/" + route["id"])
        expect(destination).to_have_value("203.0.113.79/32")
        assert get("routes").status == 403 and get("gateways").status == 403
        projection = get("routes/edit/" + route["id"]).json()["data"]
        assert set(projection) == {"revision","row","choices"}
        assert "Gateway used by the branch" not in str(projection)
        assert work("routes/apply","applyStaticRoutes",{"revision":route["revision"]}).status == 403
        destination.fill("203.0.113.128/25")
        receipt = click("routes/edit", save)
        assert receipt.status == 200, receipt.text()
        revision = receipt.json()["data"]["revision"]
        configure([gwlist])
        login("gateways")
        expect(page.get_by_role("button", name="Apply changes", exact=True)).to_be_visible()
        assert "203.0.113" not in get("gateways").text()
        assert get("routes").status == 403 and get("gateways/edit/" + gateway["id"]).status == 403
        assert work("routes/apply","applyStaticRoutes",{"revision":revision}).status == 403
        assert work("routes/rollback","rollbackStaticRoutes",{"revision":revision}).status == 403
        configure([gwedit], "routing:read")
        login("gateways/edit/" + gateway["id"])
        expect(page.get_by_role("cell", name="Laboratory", exact=True)).to_be_visible()
        expect(save).to_have_count(0)
        assert get("gateways").status == 403
        configure(["api-routing-read"], "routing:read")
        login("routes")
        expect(page.get_by_role("cell", name="203.0.113.128/25", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Apply changes")).to_have_count(0)
        assert work("routes/remove","removeStaticRoute",{"id":route["id"],"revision":revision}).status == 403
        configure([rtlist], "status:read")
        assert get("routes").status == 403
        configure([rtlist])
        page.goto(origin + '/overview')
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        page.get_by_role('link', name='Manage routing', exact=True).click()
        page.once("dialog", lambda dialog: dialog.accept())
        removed = click("routes/remove",page.get_by_role("button",name="Delete 203.0.113.128/25",exact=True))
        assert removed.status == 200, removed.text()
        expect(page.get_by_text("No static routes are configured.",exact=True)).to_be_visible()
        assert all(method == "PUT" for method, url in sent)
        print("Chromium verified static routing grants, gateway selectors, local and server checks, draft revisions and row isolation.")
        scope = "page-system-advanced-misc"
        path = "/system/advanced/miscellaneous"
        misc = origin + "/api/v1" + path
        configure([gwlist])
        assert page.request.get(misc, headers=auth).status == 403
        configure([scope], "system.settings:write")
        page.goto(origin + path)
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in", exact=True).click()
        sticky = page.get_by_role("checkbox", name="Use sticky connections", exact=True)
        expect(sticky).not_to_be_checked()
        stale = page.request.get(misc, headers=auth).json()["data"]["revision"]
        sticky.check()
        timeout = page.get_by_role("spinbutton", name="Source tracking timeout (seconds)", exact=True)
        expect(timeout).to_have_value("0")
        timeout.fill("2147483648")
        save.click()
        expect(timeout).to_have_attribute("aria-invalid","true")
        timeout.fill("20")
        with page.expect_response(lambda resp: resp.url == misc and resp.request.method == "PUT") as commit:
            save.click()
        assert commit.value.status == 200, commit.value.text()
        expect(sticky).to_be_checked()
        expect(timeout).to_have_value("20")
        if evidence:
            page.screenshot(path=evidence / "load-balancing.png", full_page=True)
        write = auth | {"Origin":origin,"X-FreeSense-Command":"saveMiscSettings"}
        def whole(data, **over):
            body = {name:data[name] for name in ("revision","proxyurl","proxyuser","lb_use_sticky","schedule_states","remove_failover_states_default",
                "keep_failover_states","gw_down_kill_states","skip_rules_gw_down","dpinger_dont_add_static_routes","use_mfs_tmpvar")}
            body |= {name:data[name] for name in ("proxyport","srctrack","use_mfs_tmp_size","use_mfs_var_size","rrdbackup","dhcpbackup","logsbackup") if name in data}
            return body | over
        current = page.request.get(misc, headers=auth).json()["data"]
        stale_put = page.request.put(misc, headers=write, data=whole(current, revision=stale))
        assert stale_put.status == 409, (stale_put.status, stale_put.text())
        assert current["srctrack"] == 20
        invalid = page.request.put(misc, headers=write, data=whole(current, lb_use_sticky=False, srctrack=7))
        assert invalid.status == 422 and "srctrack" in invalid.json()["error"]["fields"]
        sticky.uncheck()
        expect(timeout).to_be_hidden()
        with page.expect_response(lambda resp: resp.url == misc and resp.request.method == "PUT") as update:
            save.click()
        assert update.value.status == 200 and update.value.json()["data"]["srctrack"] == 20
        configure([scope], "system.settings:read")
        page.goto(origin + path)
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in", exact=True).click()
        # Without the edit grant the page says so (it shows no editable values).
        expect(page.get_by_text("Your key does not allow changing the miscellaneous settings.", exact=True)).to_be_visible()
        expect(save).to_have_count(0)
        assert page.request.put(misc, headers=write, data=whole(current, revision=stale, lb_use_sticky=False)).status == 403
    finally:
        docker("exec", "-i", container, *cli, "restore", input=backup)

