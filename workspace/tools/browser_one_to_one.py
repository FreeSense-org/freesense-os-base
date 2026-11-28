"""Exercise 1:1 NAT using the real shared AJAX client, grant evaluation and System PHP router."""
from xml.etree import ElementTree
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_one_to_one(page, origin, key, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    original = docker("exec", container, *cli, "backup")
    endpoint = origin + "/api/v1/firewall/nat/one-to-one"
    auth = {"Authorization": "Bearer " + key, "Origin": origin}
    writes = []

    def observe(req):
        if req.method == "PUT" and req.url.startswith(endpoint):
            writes.append(req.url)

    def sign_in(grants):
        xml = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        wan = xml.find("./interfaces/wan")
        if wan.find("ipaddr") is None:
            ElementTree.SubElement(wan, "ipaddr").text = "198.51.100.1"
            ElementTree.SubElement(wan, "subnet").text = "24"
        user = xml.find("./system/user")
        for old in user.findall("priv"):
            user.remove(old)
        for grant in ["page-system-restapi-keys", *grants]:
            ElementTree.SubElement(user, "priv").text = grant
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(xml))
        page.goto(origin + "/firewall/nat/one-to-one")
        fill_api_key(page, key)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="1:1 NAT", exact=True)).to_be_visible()

    def submit(name, path):
        with page.expect_response(lambda r: r.request.method == "PUT" and r.url == endpoint + path) as response:
            page.get_by_role("button", name=name, exact=True).click()
        assert response.value.status == 200
        return response.value.json()["data"]

    try:
        page.on("request", observe)
        sign_in(["page-firewall-nat-1-1-edit"])
        assert page.request.get(endpoint, headers=auth).status == 403
        expect(page.get_by_role("heading", name="Mappings", exact=True)).to_have_count(0)
        inside = page.get_by_label("Internal IP or subnet", exact=True)
        outside = page.get_by_label("External subnet IP", exact=True)
        # Read only this test configuration's assigned LAN. Use an aligned block within it.
        config = ElementTree.fromstring(original)
        ip = config.findtext("./interfaces/lan/ipaddr")
        subnet = ".".join(ip.split(".")[:3]) + ".16/30"
        inside.fill(subnet)
        outside.fill("203.0.113.17")
        page.get_by_role("button", name="Create mapping", exact=True).click()
        expect(outside).to_have_attribute("aria-invalid", "true")
        assert not writes
        schema = page.request.get(origin + "/api/v1/forms", headers=auth).json()["data"]["operations"]["createOneToOne"]
        create_headers = auth | {"X-FreeSense-Command":"createOneToOne"}
        invalid = {"enabled":True, "revision":schema["currentRevision"], "external":"203.0.113.17", "internal":subnet, "destination":"any"}
        assert page.request.put(endpoint + "/create", headers=create_headers, data=invalid).status == 422
        outside.fill("203.0.113.16")
        page.get_by_label("Destination", exact=True).fill("198.51.100.2")
        first = submit("Create mapping", "/create")["id"]
        expect(page.locator("#feedback")).to_contain_text(first)
        sign_in(["page-firewall-nat-1-1", "page-firewall-nat-1-1-edit"])
        table = page.get_by_role("table")
        expect(table).to_contain_text("203.0.113.16/30")
        old = page.request.get(endpoint, headers=auth).json()["data"]
        picker = page.get_by_role("combobox", name="Select 1:1 mapping", exact=True)
        picker.select_option(first)
        page.get_by_label("Destination", exact=True).fill("any")
        submit("Save mapping", "/edit")
        expect(page.locator("#feedback")).to_have_text("1:1 mapping saved.")
        current = page.request.get(endpoint, headers=auth).json()["data"]
        assert current["mappings"][0]["destination"] == "any"
        headers = auth | {"X-FreeSense-Command":"manageOneToOne"}
        assert page.request.put(endpoint + "/manage", headers=headers, data={"revision":old["revision"],"id":first,"operation":"remove"}).status == 409
        picker.select_option(first)
        if evidence:
            page.screenshot(path=str(evidence / "one-to-one.png"), full_page=True)
        sign_in(["page-firewall-nat-1-1"])
        expect(page.get_by_role("button", name="Create mapping")).to_have_count(0)
        picker.select_option(first)
        page.get_by_role("combobox", name="Mapping action", exact=True).select_option("disable")
        page.once("dialog", lambda dialog: dialog.accept())
        submit("Update mapping", "/manage")
        current = page.request.get(endpoint, headers=auth).json()["data"]
        assert current["mappings"][0]["enabled"] is False
        assert page.request.put(endpoint + "/create", headers=create_headers, data=invalid | {"revision":current["revision"]}).status == 403
        sign_in(["page-firewall-nat-1-1", "user-config-readonly"])
        expect(page.get_by_role("table")).to_contain_text(subnet)
        expect(page.get_by_role("button", name="Create mapping")).to_have_count(0)
        assert page.request.put(endpoint + "/manage", headers=headers, data={"revision":current["revision"],"id":first,"operation":"remove"}).status == 403
        print("Chromium verified real 1:1 NAT create/edit, host-bit rejection, known-ID and list permissions, stale revision and read-only refusal.")
    finally:
        page.remove_listener("request", observe)
        docker("exec", "-i", container, *cli, "restore", input=original)
