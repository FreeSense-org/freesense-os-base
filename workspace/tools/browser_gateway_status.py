"""Exercise gateway monitoring status, filters and the separately authorized monitoring preference."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_gateway_status(page, origin, key, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    saved = docker("exec", container, *cli, "backup")
    index = "/check/prefix/FreeSense/web/index.php"
    docker("exec", container, "cp", index, "/check/original-gateway-index")
    def file(path, text):
        docker("exec", "-i", container, "php", "-r", "file_put_contents($argv[1],stream_get_contents(STDIN));", path, input=text)
    file(index, (Path(__file__).resolve().parents[1] / "freesense/tests/fixtures/gateway-status-browser.php").read_bytes())
    file("/check/gateway-reads", b"")
    file("/check/gateway-history-reads", b"")
    file("/check/history-targets", json.dumps({"at":1728003600,"targets":{}}).encode())
    history_url = origin + "/api/v1/status/gateways/history"
    auth = {"Authorization": "Bearer " + key}
    path = "/status/gateways"
    url = origin + "/api/v1/status/gateways"
    common = dict(role="wan", device="em1", family=4, description="", default=False,
                  latency_ms=None, deviation_ms=None, loss_percent=None)
    records = []
    for i, (name, state, reason) in enumerate([("Campus", "online", "Responding"), ("West", "degraded", "High latency"),
                                                ("Branch", "down", "Packet loss"), ("Initial", "pending", "Waiting for a sample"),
                                                ("Spare", "disabled", "Monitoring disabled"), ("Uplink6", "unavailable", "Interface unavailable")]):
        addr = "198.51.100." + str(i + 2) if i != 5 else "fe80::2%em1"
        records.append(common | dict(id="wan4" if i == 0 else f"{i:032x}", name=name, state=state, reason=reason, gateway=addr, monitor=addr,
                                     default=i == 0, description="Office <router>" if i == 0 else "", family=6 if i == 5 else 4))
    records[0].update(latency_ms=1.64, deviation_ms=0.23, loss_percent=0)
    records[1].update(latency_ms=230.5, deviation_ms=31.08, loss_percent=12.5)
    records[2].update(loss_percent=100)
    def samples(rows):
        file("/check/gateway-samples", json.dumps({"gateways": rows, "sampled_at": 1700000222 if rows else None}).encode())
    samples(records)
    def reads():
        return docker("exec", container, "cat", "/check/gateway-reads").count(b"\n")
    def login(grants, scope, target):
        tree = ET.fromstring(docker("exec", container, *cli, "backup"))
        user = tree.find("./system/user")
        for old in user.findall("priv"): user.remove(old)
        for grant in ["page-system-restapi-keys", *grants]: ET.SubElement(user, "priv").text = grant
        tree.find("./restapi/apitoken/scopes").text = scope
        docker("exec", "-i", container, *cli, "restore", input=ET.tostring(tree))
        page.goto(origin + target)
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
    try:
        group_path = path + "/groups"
        group_url = url + "/groups"
        file("/check/gateway-group-reads", b"")
        assert page.request.get(group_url).status == 401
        for grant, scope in [("page-status-gateways", "status:read"), ("page-status-gatewaygroups", "routing:read")]:
            login([grant], scope, "/overview")
            assert page.request.get(group_url + "?filter=unused", headers=auth).status == 403
        assert not docker("exec",container,"cat","/check/gateway-group-reads")
        group = {"name":"EdgeLinks", "description":"Branch <uplinks>", "trigger":"downloss", "state":"online", "tier":2,
                 "members":[{"gateway":"wan4","tier":1,"selected":False},{"gateway":"wan6","tier":2,"selected":True}]}
        members = {"wan4":dict(name="Primary", description="", role="wan", device="em1", address="198.51.100.1", source=None,
                               state="stale",reason="The last observation is older than eight seconds.",sampled_at=1700000222),
                   "wan6":dict(name="Campus6",description="Office <router>",role="opt1",device="em2",address="fe80::5%em2",source="fe80::2%em2",
                               state="online",reason="Responding",sampled_at=1700000233)}
        counts = dict(online=1, degraded=0, down=0, pending=0, absent=0, stale=1, unavailable=0)
        def group_sample(data):
            file("/check/gateway-group-snapshot", json.dumps(data).encode())
        group_sample(dict(pending=True,groups=[group],gateways=members,counts=counts))
        login(["page-status-gatewaygroups"], "status:read", group_path)
        panel = page.get_by_role("region",name="EdgeLinks")
        expect(panel.get_by_role("cell",name="fe80::5%em2",exact=True)).to_be_visible()
        expect(panel.get_by_role("cell",name="fe80::2%em2",exact=True)).to_be_visible()
        expect(panel.get_by_text("Branch <uplinks>",exact=True)).to_be_visible()
        expect(panel.get_by_text("The last observation is older than eight seconds.")).to_be_visible()
        expect(panel.get_by_text("Tier 2 · Selected",exact=True)).to_be_visible()
        expect(page.get_by_text("Saved gateway group changes are awaiting approval.")).to_be_visible()
        expect(page.get_by_role("link",name="Configure gateway groups")).to_have_count(0)
        expect(page.get_by_role("navigation",name="Gateway status pages").get_by_role("link",name="Gateways",exact=True)).to_have_count(0)
        assert page.request.get(group_url+"?killgw=test",headers=auth).status == 400
        assert page.request.put(group_url,headers=auth).status == 405
        for forbidden in [url,history_url,origin+"/api/v1/routing/groups"]:
            assert page.request.get(forbidden,headers=auth).status == 403
        reads_allowed = page.request.get(origin+"/api/v1/forms",headers=auth).json()["data"]["reads"]
        assert "gatewayGroupStatus" in reads_allowed and "gatewayStatus" not in reads_allowed
        if evidence: page.screenshot(path=evidence/"gateway-group-status.png",full_page=True)
        group_sample(dict(pending=False,groups=[],gateways={},counts={key:0 for key in counts}))
        page.get_by_role("button",name="Refresh",exact=True).click()
        expect(page.get_by_text("No gateway groups are approved.")).to_be_visible()
        expect(panel).to_have_count(0)
        docker("exec",container,"php","-r","unlink('/check/gateway-group-snapshot');")
        with page.expect_response(lambda r:r.url==group_url) as unavailable:
            page.get_by_role("button",name="Refresh",exact=True).click()
        assert unavailable.value.status == 503
        print("Chromium verified the independent group-status grant, approved tier and zone, stale reason, pending flag, refresh and read-only menu.")
        login(["page-system-gateways"], "status:read", "/overview")
        assert page.request.get(url + "?state=invalid", headers=auth).status == 403 and reads() == 0
        assert page.request.get(history_url + "?range=invalid", headers=auth).status == 403
        assert page.request.get(history_url + "?range=invalid").status == 401
        login(["page-status-gateways"], "routing:read", "/overview")
        assert page.request.get(url, headers=auth).status == 403 and reads() == 0
        assert page.request.get(history_url, headers=auth).status == 403
        assert not docker("exec",container,"cat","/check/gateway-history-reads")
        login(["page-status-gateways"], "status:read", path)
        expect(page.get_by_text("No gateway history in this window.",exact=True)).to_be_visible()
        fixture = Path(__file__).resolve().parents[1] / "freesense/tests/fixtures/gateway-quality-seed.php"
        file("/check/seed-history.php",fixture.read_bytes())
        docker("exec",container,"php","/check/seed-history.php")
        old_reads=docker("exec",container,"cat","/check/gateway-history-reads")
        for query,code in [("?range=12h",422),("?range=1h&range=7d",400),("?name=old",400)]:
            assert page.request.get(history_url+query,headers=auth).status==code
        assert page.request.delete(history_url,headers=auth).status == 405
        assert old_reads==docker("exec",container,"cat","/check/gateway-history-reads")
        page.get_by_role("button",name="View history",exact=True).click()
        panel=page.locator('[data-gateway-history="detail"]')
        expect(panel.get_by_role("img")).to_have_count(3)
        expect(panel.get_by_role("img",name="Campus Round-trip time ranges from 1.5 ms to 1.5 ms. 30 measured intervals out of 60.",exact=True)).to_be_visible()
        page.get_by_label("History range",exact=True).select_option("30d")
        with page.expect_response(lambda r: r.url.endswith("history?range=30d")) as selected:
            page.get_by_role("button",name="View history",exact=True).focus()
            page.keyboard.press("Enter")
        assert selected.value.status==200
        logged = selected.value.json()["data"]
        assert len(logged["t"])==720 and logged["gateways"]["Campus"]["loss"][-1]==50
        expect(panel.get_by_text("Packet loss · 50%",exact=True)).to_be_visible()
        page.get_by_label("Gateway history for",exact=True).select_option("Spare")
        expect(panel.get_by_text("Packet loss · 100%",exact=True)).to_be_visible()
        if evidence: page.screenshot(path=evidence / "gateway-quality-history.png",full_page=True)
        plots=page.evaluate("""async (payload) => {
            const {gatewaySparklines}=await import('/assets/status/gateways.js');
            const rows=gatewaySparklines(payload);
            return [rows.querySelectorAll('canvas').length, rows.querySelector('canvas').getAttribute('aria-label')];
        }""",logged)
        assert plots[0]==2 and "Campus RTT" in plots[1]
        login(["page-status-gateways"], "status:read", path)
        expect(page.get_by_role("cell", name="Office <router>", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="0%", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="230.5 ms", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="Configure gateways")).to_have_count(0)
        expect(page.get_by_text("Default IPv4", exact=True)).to_be_visible()
        expect(page.get_by_text("Waiting for a sample", exact=True)).to_be_visible()
        page.get_by_role("button", name="Pause live updates").click()
        if evidence: page.screenshot(path=evidence / "gateway-status.png", full_page=True)
        assert page.request.get(origin + "/api/v1/routing/gateways", headers=auth).status == 403
        registered = page.request.get(origin + "/api/v1/forms", headers=auth).json()["data"]["operations"]
        assert set(registered) <= {"saveAccountAppearance", "saveDashboardLayout", "resetDashboardLayout", "revokeMyApiKey", "bulkRevokeMyApiKeys", "bulkDeleteMyApiKeys", "revokeOwnApiKeys"}, sorted(registered)
        submit = page.get_by_role("button", name="Filter", exact=True)
        state = page.get_by_label("Monitor state", exact=True)
        state.select_option("disabled")
        submit.click()
        expect(page.get_by_role("cell", name="Spare", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="0%", exact=True)).to_have_count(0)
        state.select_option("all")
        page.get_by_label("Search gateways", exact=True).fill("FE80::")
        submit.click()
        expect(page.get_by_role("cell", name="Uplink6", exact=True)).to_be_visible()
        before = reads()
        for suffix, code in [("?state=active", 422), ("?filter=a%00b", 422), ("?state=down&state=online", 400)]:
            assert page.request.get(url + suffix, headers=auth).status == code
        assert page.request.put(url, headers=auth).status == 405 and reads() == before
        docker("exec", container, "php", "-r", "unlink('/check/gateway-samples');")
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("status is unavailable")
        samples([])
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(page.get_by_text("No gateways are configured.", exact=True)).to_be_visible()
        login(["page-system-gateways-editgateway", "page-system-gateways"], "routing:write", "/system/routing/gateways/edit")
        toggle = page.get_by_label("Disable gateway monitoring", exact=True)
        expect(toggle).not_to_be_checked()
        page.get_by_label("Name", exact=True).fill("Peer_One")
        page.get_by_label("Gateway", exact=True).fill("192.0.2.8")
        toggle.check()
        with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/v1/routing/gateways")) as created:
            page.get_by_role("button", name="Save", exact=True).click()
        assert created.value.status == 200, created.value.text()
        row_id = created.value.json()["data"]["id"]
        table = page.request.get(origin + "/api/v1/routing/gateways", headers=auth).json()["data"]["rows"]
        assert next(row for row in table if row["id"] == row_id)["monitor_disabled"]
        login(["page-system-gateways-editgateway"], "routing:write", "/system/routing/gateways/edit/" + row_id)
        expect(toggle).to_be_checked()
        if evidence: page.screenshot(path=evidence / "gateway-monitor-setting.png", full_page=True)
        toggle.uncheck()
        with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/v1/routing/gateways/edit")) as changed:
            page.get_by_role("button", name="Save", exact=True).click()
        assert changed.value.status == 200
        row = page.request.get(origin + "/api/v1/routing/gateways/edit/" + row_id, headers=auth).json()["data"]["row"]
        assert row["enabled"] is True and "monitor_disabled" not in row
        assert page.request.get(url, headers=auth).status == 403
        print("Chromium verified scored history windows, 30-day keyboard selection, authorized charts and dashboard sparklines, forbidden history reads, live gateway metrics and scoped monitoring preferences.")
    finally:
        docker("exec", container, "cp", "/check/original-gateway-index", index)
        docker("exec", "-i", container, *cli, "restore", input=saved)
