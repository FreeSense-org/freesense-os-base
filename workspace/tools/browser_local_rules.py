"""Exercise local multicast and transit rows through the same authenticated System editor."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from xml.etree import ElementTree

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_local_rules(page, origin, credential, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *cli, "backup")
    endpoint = origin + "/api/v1/firewall/rules"
    headers = {"Authorization": "Bearer " + credential}
    save_headers = headers | {"Origin": origin, "X-FreeSense-Command": "saveRules"}
    writes = []

    def observe(request):
        if request.method == "PUT" and request.url.startswith(endpoint):
            writes.append(request.url)

    def read():
        reply = page.request.get(endpoint, headers=headers)
        assert reply.status == 200
        return reply.json()["data"]

    def sign_in():
        page.goto(origin + "/firewall")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()

    def submit(button, path=endpoint):
        with page.expect_response(lambda r: r.url == path and r.request.method == "PUT") as reply:
            button.click()
        return reply.value

    page.on("request", observe)
    try:
        doc = ElementTree.fromstring(original)
        for name in ["interfaces", "firewall", "aliases"]:
            if doc.find(name) is not None:
                doc.remove(doc.find(name))
        doc.append(ElementTree.fromstring("<interfaces><lan><enable/><if>em1</if><ipaddr>192.0.2.1</ipaddr><subnet>24</subnet></lan>"
                                          "<opt1><enable/><if>em2</if></opt1><opt2><enable/><if>em3</if><ipaddr>198.51.100.113</ipaddr>"
                                          "<subnet>28</subnet></opt2><wan><enable/><if>em0</if></wan></interfaces>"))
        doc.append(ElementTree.fromstring("<aliases><alias><name>TransitUsers</name><member>192.0.2.31</member></alias></aliases>"))
        user = doc.find("./system/user")
        for field in user.findall("priv"):
            user.remove(field)
        for grant in ["page-system-restapi-keys", "api-firewall-rules-write"]:
            ElementTree.SubElement(user, "priv").text = grant
        doc.find("./restapi/apitoken/scopes").text = "firewall.rules:write"
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(doc))
        operations = page.request.get(origin + "/api/v1/forms", headers=headers).json()["data"]["operations"]
        for op, fld in [("saveRules", "rules"), ("importRules", "file")]:
            scope = operations[op]["fields"][fld]
            assert scope["internalRoles"] == ["lan", "opt1", "opt2"] and scope["ipv4IngressRoles"] == ["lan", "opt2"]
            assert scope["broadcastLinks"]["opt2"]["address"] == "198.51.100.127"
            assert scope["aliasNames"] == ["TransitUsers"]
            assert all(value not in json.dumps(scope) for value in ["em0", "em1", "em2", "192.0.2.1", "192.0.2.31"])
        transit = {"enabled": True, "action": "pass", "source": "@TransitUsers", "destination": "any"}
        seed = page.request.put(endpoint, headers=save_headers, data={"revision": read()["revision"], "rules": [transit]})
        assert seed.status == 200
        unchanged = read()["rules"][0]
        sign_in()
        save = page.get_by_role("button", name="Save rules", exact=True)
        approve = r"""
        require '/check/prefix/FreeSense/autoload.php';
        $store = new FreeSense\Config\ConfigurationStore('/check/config');
        $store->editDocument(static function (DOMDocument $doc) use ($store): void {
            $gateway = FreeSense\Network\Routing\StaticRouting::saveGateway($doc,FreeSense\Network\Routing\StaticRouting::draft($doc)['revision'],null,
                ['name'=>'LanGateway','enabled'=>true,'role'=>'lan','address'=>'192.0.2.2'])['id'];
            FreeSense\Network\Gateways\GatewayGroups::save($doc,FreeSense\Network\Gateways\GatewayGroups::draft($doc)['revision'],null,
                ['name'=>'ApprovedSite','members'=>[['gateway'=>$gateway,'tier'=>1]]]);
            $generation = FreeSense\Network\Gateways\GatewayGroupGeneration::candidate($doc,FreeSense\Network\Gateways\GatewayGroups::draft($doc)['revision']);
            $definitions = FreeSense\Network\Routing\StaticRouting::definitions($doc);
            $bindings = FreeSense\Network\Routing\StaticRouting::gatewayBindings($doc,$definitions);
            $receipt = new FreeSense\Network\Routing\StaticRouteReceipt($store->directory);
            $receipt->save(['boot'=>'123.0','active'=>['definitions'=>$definitions,'routes'=>[],'gateways'=>$bindings],
                'previous'=>null,'owned'=>[],'pending'=>null]);
            $journal = new FreeSense\Network\Gateways\GatewayGroupJournal($store->directory);
            $journal->update($journal->read()['revision'],static fn() => $generation);
        });
        """
        docker("exec", container, "php", "-r", approve)
        group_id, choice = next(iter(read()["routing_groups"].items()))
        assert choice == {"name": "ApprovedSite", "family": "inet"}
        sign_in()
        first_rule = page.get_by_role("group", name="Rule 1", exact=True)
        first_rule.get_by_text("Policy routing", exact=True).click()
        gateway = first_rule.get_by_role("combobox", name="Gateway or group", exact=True)
        family = first_rule.get_by_role("combobox", name="Address family", exact=True)
        family.select_option("inet6")
        expect(gateway.get_by_role("option", name="ApprovedSite")).to_have_count(0)
        family.select_option("inet")
        gateway.select_option(group_id)
        first_rule.get_by_role("combobox", name="When all gateways are unavailable", exact=True).select_option("skip")
        if evidence:
            page.screenshot(path=evidence / "gateway-policy-rule.png", full_page=True)
        reply = submit(save)
        assert reply.status == 200, reply.text()
        assert read()["rules"][0]["gateway_unavailable"] == "skip"
        expect(gateway).to_have_value(group_id)
        gateway.select_option("")
        reply = submit(save)
        assert reply.status == 200, reply.text()
        assert read()["rules"][0] == unchanged

        direct_id, target = next((identifier, target) for identifier, target in read()["routing_gateways"].items()
                                 if target["name"] == "LanGateway")
        assert target["family"] == "inet"
        first_rule.get_by_text("Policy routing", exact=True).click()
        gateway.select_option("direct:" + direct_id)
        fallback = first_rule.get_by_role("combobox", name="When the gateway is unconfigured or unusable", exact=True)
        fallback.select_option("skip")
        expect(first_rule.get_by_text("A configured gateway that is measured down uses the default route.")).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / "individual-gateway-rule.png", full_page=True)
        reply = submit(save)
        assert reply.status == 200, reply.text()
        direct = read()["rules"][0]
        assert direct["gateway"] == direct_id and direct["gateway_unavailable"] == "skip" and "gateway_group" not in direct

        unrestricted = docker("exec", container, *cli, "backup")
        scoped = ElementTree.fromstring(unrestricted)
        for priv in scoped.findall('./system/user/priv'):
            if priv.text == 'api-firewall-rules-write':
                priv.text = 'api-firewall-rules-lan-write'
        docker("exec","-i",container,*cli,"restore",input=ElementTree.tostring(scoped))
        sign_in()
        expect(page.get_by_role("columnheader", name="Gateway", exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="LanGateway", exact=True)).to_be_visible()
        restricted = read()
        assert "routing_gateways" not in restricted and "routing_groups" not in restricted
        assert restricted["routing_labels"] == {direct["id"]: "LanGateway"}
        if evidence:
            page.screenshot(path=evidence / "scoped-gateway-column.png", full_page=True)
        docker("exec","-i",container,*cli,"restore",input=unrestricted)
        sign_in()

        expect(gateway).to_have_value("direct:" + direct_id)
        gateway.select_option(group_id)
        reply = submit(save)
        assert reply.status == 200, reply.text()
        grouped = read()["rules"][0]
        assert grouped["gateway_group"] == group_id and "gateway" not in grouped
        gateway.select_option("")
        reply = submit(save)
        assert reply.status == 200, reply.text()
        assert read()["rules"][0] == unchanged

        add = page.get_by_role("button", name="Add rule", exact=True)
        add.click()
        new = page.get_by_role("group", name="Rule 2", exact=True)
        new.get_by_role("combobox", name="Action", exact=True).select_option("pass")
        new.get_by_label("Enabled", exact=True).check()
        new.get_by_role("combobox", name="Traffic direction", exact=True).select_option("local-in")
        role = new.get_by_role("combobox", name="Internal interface", exact=True)
        assert role.locator("option[value=opt1]").is_disabled()
        role.select_option("lan")
        expect(new.get_by_label("Source", exact=True)).to_have_value("link")
        expect(new.get_by_role("combobox", name="Protocol", exact=True)).to_be_disabled()
        new.get_by_label("Log matching traffic", exact=True).check()
        destination = new.get_by_label("Destination", exact=True)
        destination.fill("224.0.1.251")
        new.get_by_label("Destination port", exact=True).fill("5353")
        before = len(writes)
        save.click()
        expect(page.locator('[data-error-for="rules"]')).to_contain_text("link-local multicast")
        assert len(writes) == before
        destination.fill("224.0.0.251")
        assert submit(save).status == 200
        expect(page.locator("#feedback")).to_have_text("Draft saved.")
        state = read()
        assert state["rules"][0] == unchanged
        inbound = state["rules"][1]
        assert (inbound["traffic"], inbound["source"], inbound["interface"], inbound["log"]) == ("local-in", "link", "lan", True)
        new.get_by_role("combobox", name="Traffic direction", exact=True).select_option("local-out")
        new.get_by_role("combobox", name="Address family", exact=True).select_option("inet6")
        new.get_by_role("combobox", name="Action", exact=True).select_option("block")
        expect(new.get_by_label("Source", exact=True)).to_have_value("interface")
        role.select_option("opt1")
        destination.fill("ff02::fb")
        new.get_by_label("Source port", exact=True).fill("5300")
        new.get_by_label("Source port end", exact=True).fill("5353")
        assert submit(save).status == 200
        expect(page.locator("#feedback")).to_have_text("Draft saved.")
        if evidence:
            page.screenshot(path=str(evidence / "local-multicast-rule.png"), full_page=True)
        outbound = read()["rules"][1]
        assert outbound["id"] == inbound["id"] and outbound["source_port_end"] == 5353
        upload = page.get_by_label("JSON rules file")
        send = page.get_by_role("button", name="Import JSON", exact=True)

        def choose(rows):
            upload.set_input_files({"name": "ordered.json", "mimeType": "application/json", "buffer": json.dumps(rows).encode()})

        for overrides, message in [
            ({"destination_port_end": 5354}, "single destination port"),
            ({"source": "link"}, "source associated"), ({"source": "any"}, "source associated"),
            ({"destination": "@TransitUsers"}, "link-local multicast"), ({"destination": "ff0e::fb"}, "link-local multicast"),
            ({"interface": "wan"}, "available internal interface"), ({"protocol": "tcp"}, "requires UDP"),
            ({"gateway": direct_id, "gateway_group": group_id, "gateway_unavailable": "skip"}, "Choose one gateway or one gateway group."),
            ({"traffic": "transit", "source": "any", "interface": "em0"}, "assigned interface or an interface group"), ({"extra": 1}, "Unsupported rule field"),
        ]:
            invalid = outbound | overrides
            choose([unchanged, invalid])
            before = len(writes)
            send.click()
            expect(page.locator('[data-error-for="file"]')).to_contain_text(message)
            assert len(writes) == before
            rejected = page.request.put(endpoint, headers=save_headers, data={"revision": read()["revision"], "rules": [unchanged, invalid]})
            assert rejected.status == 422 and "rules" in rejected.json()["error"]["fields"]
        chosen = [outbound | {"destination_port_end": 5353}, unchanged]
        choose(chosen)
        assert submit(send, endpoint + "/import").status == 200
        expect(page.locator("#feedback")).to_have_text("Draft saved.")
        assert read()["rules"] == [outbound, unchanged]
        stale = read()
        doc = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        doc.find("./interfaces/opt1").append(ElementTree.fromstring("<disabled/>"))
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(doc))
        raced = page.request.put(endpoint, headers=save_headers, data={"revision": stale["revision"], "rules": stale["rules"]})
        assert raced.status == 409
        page.get_by_role("button", name="Refresh", exact=True).click()
        first = page.get_by_role("group", name="Rule 1", exact=True)
        expect(first.get_by_role("combobox", name="Internal interface", exact=True)).to_have_value("opt1")
        first.get_by_label("Enabled", exact=True).uncheck()
        assert submit(save).status == 200
        assert read()["rules"][0] == outbound | {"enabled": False}
        add.click()
        third = page.get_by_role("group", name="Rule 3", exact=True)
        third.get_by_label("Enabled", exact=True).check()
        third.get_by_role("combobox", name="Action", exact=True).select_option("pass")
        traffic = third.get_by_role("combobox", name="Traffic direction", exact=True)
        traffic.select_option("broadcast-in")
        third_role = third.get_by_role("combobox", name="Internal interface", exact=True)
        third_role.select_option("lan")
        target = third.get_by_role("combobox", name="Broadcast destination", exact=True)
        expect(target.locator("option").first).to_have_text("Selected link broadcast · 192.0.2.255")
        target.select_option("255.255.255.255")
        third.get_by_label("Destination port", exact=True).fill("29021")
        assert submit(save).status == 200
        expect(page.locator("#feedback")).to_have_text("Draft saved.")
        received = read()["rules"][2]
        assert (received["source"], received["destination"]) == ("link", "255.255.255.255")
        traffic.select_option("broadcast-out")
        third_role.select_option("opt2")
        source = third.get_by_role("combobox", name="Original source interface", exact=True)
        assert source.locator("option[value=opt2]").is_disabled()
        assert target.locator("option[value='255.255.255.255']").is_disabled()
        expect(third.get_by_role("combobox", name="Address family", exact=True)).to_be_disabled()
        expect(target.locator("option").first).to_have_text("Selected link broadcast · 198.51.100.127")
        source.select_option("lan")
        assert submit(save).status == 200
        expect(page.locator("#feedback")).to_have_text("Draft saved.")
        prior = read()
        relayed = prior["rules"][2]
        assert relayed == received | {"traffic": "broadcast-out", "interface": "opt2", "source": "lan", "destination": "link-broadcast"}
        for override, message in [({"source": "wan"}, "different internal source"), ({"interface": "lan"}, "different internal source"),
                                  ({"destination": "255.255.255.255"}, "selected link broadcast"), ({"family": "inet6"}, "static IPv4")]:
            invalid = [*prior["rules"][:2], relayed | override]
            choose(invalid)
            before = len(writes)
            send.click()
            expect(page.locator('[data-error-for="file"]')).to_contain_text(message)
            assert len(writes) == before
            failed = page.request.put(endpoint, headers=save_headers, data={"revision": prior["revision"], "rules": invalid})
            assert failed.status == 422 and "rules" in failed.json()["error"]["fields"]
        assert read() == prior
        if evidence:
            page.screenshot(path=str(evidence / "broadcast-rule.png"), full_page=True)
        doc = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        doc.find("./restapi/apitoken/scopes").text = "firewall.rules:read"
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(doc))
        sign_in()
        expect(page.get_by_role("row").filter(has_text="Send from firewall")).to_contain_text("OPT1")
        expect(page.get_by_role("row").filter(has_text="Relay broadcast")).to_contain_text("OPT2")
        expect(save).to_have_count(0)
        operations = page.request.get(origin + "/api/v1/forms", headers=headers).json()["data"]["operations"]
        assert not {"saveRules", "importRules", "natMode"}.intersection(operations)
        print("Ordered firewall UI/API passed IPv4 multicast and broadcast receive, original-source relay, IPv6 local send, alias-backed transit, logging, "
              "source ranges, normalized JSON import and ordering, scoped interface choices, withdrawn-role recovery and read-only status.")
    finally:
        page.remove_listener("request", observe)
        docker("exec", "-i", container, *cli, "restore", input=original)
