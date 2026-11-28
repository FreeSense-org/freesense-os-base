"""Exercise the registered Optional UDP relay editor through the real central browser commands."""
from xml.etree import ElementTree

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_udp_relay(page, origin, credential, container, docker, evidence=None):
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    docker("exec", container, "cp", "-R", "/optional/net/FreeSense-pkg-udpbroadcastrelay/files/usr/local/.", "/check/prefix/")
    original = docker("exec", container, *cli, "backup")
    url = origin + "/api/v1/packages/udp-broadcast-relay"
    auth = {"Authorization": "Bearer " + credential}

    def login(scope):
        root = ElementTree.fromstring(docker("exec", container, *cli, "backup"))
        user = root.find("./system/user")
        for priv in user.findall("priv"):
            user.remove(priv)
        for value in ["page-system-restapi-keys", "page-services-udpbroadcastrelay"]:
            ElementTree.SubElement(user, "priv").text = value
        root.find("./restapi/apitoken/scopes").text = scope
        interfaces = root.find("interfaces")
        if interfaces.find("opt1") is None:
            interfaces.append(ElementTree.fromstring("<opt1><enable/><if>em2</if></opt1>"))
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(root))
        page.goto(origin + "/packages")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()
        menu_link(page,"Services","Services: UDP Broadcast Relay").click()
        expect(page.get_by_role("heading", name="UDP Broadcast Relay", exact=True)).to_be_visible()

    def send(button, path, status=200):
        with page.expect_response(lambda r: r.url == url + path and r.request.method == "PUT") as reply:
            button.click()
        assert reply.value.status == status
        if status == 200:
            expect(page.get_by_text("Changes saved.", exact=True)).to_be_visible()

    def values():
        return page.request.get(url, headers=auth).json()["data"]["values"]

    def editor(name):
        page.get_by_role("button", name=name, exact=True).click()
        save = page.get_by_role("button", name="Save instance", exact=True)
        return page.locator("form").filter(has=save), save

    try:
        login("packages.udp-broadcast-relay:write")
        enabled = page.get_by_label("Enable UDP Broadcast Relay", exact=True)
        enabled.check()
        send(page.get_by_role("button", name="Save general settings"), "/settings")
        recorded = []
        page.on("request", lambda r: recorded.append(r.method) if r.url == url + "/instances/save" else None)
        for index, port in [(1, 41000), (2, 41001)]:
            form, save = editor("Add New Instance")
            form.get_by_label("Description", exact=True).fill("Lab relay " + str(index))
            form.get_by_label("UDP Port", exact=True).fill(str(port))
            group = form.get_by_role("group", name="Network Interfaces", exact=True)
            group.get_by_role("button", name="Add item").click()
            select = group.get_by_role("combobox").first
            assert select.locator("option").all_text_contents() == ["Choose an internal interface", "lan (em0)", "opt1 (em2)"]
            select.select_option("lan")
            save.click()
            expect(group.get_by_text("Select 2 to 8 items.")).to_be_visible()
            expect(form.locator('[data-error-for="roles"]')).to_contain_text("selection count")
            assert recorded == ["PUT"] * (index - 1)
            group.get_by_role("button", name="Add item").click()
            group.get_by_role("combobox").last.select_option("opt1")
            send(save, "/instances/save")
        assert [r["id"] for r in values()["instances"]] == [1, 2]
        form, save = editor("Edit instance 2")
        form.get_by_label("UDP Port", exact=True).fill("41000")
        send(save, "/instances/save", 422)
        expect(form.locator('[data-error-for="port"]')).to_contain_text("Another enabled instance")
        form.get_by_label("Enable this instance", exact=True).uncheck()
        send(save, "/instances/save")
        assert values()["instances"][1]["enabled"] is False
        editor("Edit instance 1")
        page.once("dialog", lambda dialog: dialog.accept())
        send(page.get_by_role("button", name="Delete instance", exact=True), "/instances/remove")
        assert [r["id"] for r in values()["instances"]] == [2]
        login("packages.udp-broadcast-relay:read")
        expect(page.get_by_role("cell", name="Lab relay 2", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Add New Instance")).to_have_count(0)
        response = page.request.put(url + "/settings", headers=auth | {"Origin": origin, "X-FreeSense-Command": "package.udp-broadcast-relay.settings"},
                                    data={"revision": values()["revision"], "enabled": False})
        assert response.status == 403
        print("UDP relay browser workflow passed Services navigation, delegated AJAX edits, client/server errors, two instances, delete and read-only grants.")
    finally:
        docker("exec", "-i", container, *cli, "restore", input=original)
