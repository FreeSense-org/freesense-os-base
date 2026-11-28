"""Exercise real browser and PHP management routes for the ordered WAN translation catalog."""
from xml.etree import ElementTree
from browser_aliases import seed_hostname_alias
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_outbound(page, origin, key, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *cli, "backup")
    endpoint = origin + "/api/v1/firewall/nat/outbound"
    headers = {"Authorization":"Bearer " + key, "Origin":origin}
    sent = []

    def observe(request):
        if request.method == "PUT" and request.url.startswith(endpoint):
            sent.append(request.url)

    def sign_in(*grants):
        xml = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        user = xml.find("./system/user")
        for child in user.findall("priv"):
            user.remove(child)
        for grant in ["page-system-restapi-keys", *grants]:
            ElementTree.SubElement(user, "priv").text = grant
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(xml))
        page.goto(origin + "/firewall/nat/outbound")
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Outbound NAT", exact=True)).to_be_visible()

    def click(title, url):
        with page.expect_response(lambda response: response.request.method == "PUT" and response.url == url) as response:
            page.get_by_role("button", name=title, exact=True).click()
        return response.value

    try:
        seed_hostname_alias(container,docker,"ClientsFromDNS",["client.nat.test"])
        seed_hostname_alias(container,docker,"UpstreamFromDNS",["upstream.nat.test"])
        page.on("request", observe)
        sign_in("page-firewall-nat-outbound-edit")
        assert page.request.get(endpoint, headers=headers).status == 403
        expect(page.get_by_role("button", name="Save mode", exact=True)).to_have_count(0)
        source = page.get_by_label("Source address", exact=True)
        source.fill("192.0.2.5/24")
        page.get_by_role("button", name="Create mapping", exact=True).click()
        expect(source).to_have_attribute("aria-invalid","true")
        assert sent == []
        source.fill("@ClientsFromDNS")
        page.get_by_label("Destination address",exact=True).fill("@UpstreamFromDNS")
        page.get_by_role("combobox", name="Protocol", exact=True).select_option("udp")
        page.get_by_label("Include Destination first port", exact=True).check()
        page.get_by_label("Destination first port", exact=True).fill("53")
        page.get_by_role("combobox", name="Translation", exact=True).select_option("no_nat")
        result = click("Create mapping", endpoint + "/create")
        assert result.status == 200
        first = result.json()["data"]["id"]
        expect(page.locator("#feedback")).to_contain_text(first)
        edit = headers | {"X-FreeSense-Command":"editOutboundMapping"}
        hidden = {"revision":result.json()["data"]["revision"], "id":first, "enabled":True, "action":"no_nat", "protocol":"udp",
                  "source":"@ClientsFromDNS", "destination":"@UpstreamFromDNS", "description":"DNS exemption"}
        edited = page.request.put(endpoint + "/edit", headers=edit, data=hidden)
        assert edited.status == 200 and "mappings" not in edited.json()["data"]
        sign_in("page-firewall-nat-outbound", "page-firewall-nat-outbound-edit")
        listing = page.request.get(endpoint, headers=headers).json()["data"]
        assert len(listing["mappings"]) == 1 and listing["mappings"][0]["destination_port"] == 53
        assert listing["mappings"][0]["description"] == "DNS exemption"
        assert (listing["mappings"][0]["source"],listing["mappings"][0]["destination"]) == ("@ClientsFromDNS","@UpstreamFromDNS")
        mode = page.get_by_role("combobox", name="NAT mode", exact=True)
        expect(mode.locator("option")).to_have_count(4)
        mode.select_option("hybrid")
        assert click("Save mode", endpoint + "-mode").status == 200
        expect(page.locator("#feedback")).to_have_text("Outbound NAT mode saved.")
        expect(page.get_by_role("heading",name="Automatic rules")).to_be_visible()
        automatic = page.request.get(endpoint, headers=headers).json()["data"]["automatic_mapping"]
        assert automatic and automatic["network"] in page.locator("#content").inner_text()
        second = click("Create mapping", endpoint + "/create").json()["data"]["id"]
        select = page.get_by_role("combobox", name="Select outbound mapping", exact=True)
        select.select_option(second)
        page.get_by_role("combobox", name="Mapping action", exact=True).select_option("move up")
        page.once("dialog", lambda dialog: dialog.accept())
        assert click("Update mapping",endpoint + "/manage").status == 200
        snapshot = page.request.get(endpoint,headers=headers).json()["data"]
        assert snapshot["mode"] == "hybrid" and [row["id"] for row in snapshot["mappings"]] == [second,first]
        stale = page.request.put(endpoint + "/manage", headers=headers | {"X-FreeSense-Command":"manageOutboundMapping"},
                                 data={"revision":listing["revision"],"operation":"remove","id":second})
        assert stale.status == 409
        select.select_option(first)
        if evidence:
            page.screenshot(path=str(evidence/"outbound-mappings.png"),full_page=True)
        cleared = page.request.put(endpoint + "/edit", headers=edit,
                                   data=hidden | {"revision":snapshot["revision"], "replace_selectors":True})
        assert cleared.status == 200
        latest = page.request.get(endpoint,headers=headers).json()["data"]
        assert "destination_port" not in next(row for row in latest["mappings"] if row["id"] == first)
        sign_in("page-firewall-nat-outbound","user-config-readonly")
        expect(page.get_by_role("heading",name="Mappings",exact=True)).to_be_visible()
        expect(page.get_by_role("button",name="Create mapping",exact=True)).to_have_count(0)
        assert page.request.put(endpoint + "/manage", headers=headers | {"X-FreeSense-Command":"manageOutboundMapping"},
                                data={"revision":snapshot["revision"],"id":first,"operation":"remove"}).status == 403
        print("Chromium and PHP verified outbound mapping grants, invalid AJAX fields, exemption creation, hybrid mode, ordering, stale edits and read-only access.")
    except Exception:
        if evidence:
            (evidence/"workspace.txt").write_text(page.locator("#workspace").inner_text(), encoding="utf-8")
        raise
    finally:
        page.remove_listener("request", observe)
        docker("exec","-i",container,*cli,"restore",input=original)
