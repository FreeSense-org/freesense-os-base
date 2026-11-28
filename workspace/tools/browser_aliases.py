"""Exercise address aliases through the installed System router and the shared WebUI form client."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import re
import json
from xml.etree import ElementTree

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def seed_hostname_alias(container, docker, name, members):
    cli = ["php", "/check/prefix/sbin/freesense-alias"]
    revision = json.loads(docker("exec", container, *cli, "list"))["revision"]
    docker("exec", "-i", container, *cli, "set", revision, name, input=json.dumps(members).encode())


def exercise_alias_authority(page, origin, credential, container, docker):
    """Exercise the two distinct page grants using the same scoped key."""
    cli = ["php", "/check/prefix/sbin/freesense-config"]
    def backup():
        return docker("exec", container, *cli, "backup")
    original = backup()

    def sign_in(grant):
        document = ElementTree.fromstring(backup())
        user = document.find("./system/user")
        for field in user.findall("priv"):
            user.remove(field)
        for privilege in ["page-system-restapi-keys", grant]:
            ElementTree.SubElement(user, "priv").text = privilege
        docker("exec", "-i", container, *cli, "restore", input=ElementTree.tostring(document))
        page.goto(origin + "/firewall/aliases")
        fill_api_key(page, credential)
        page.get_by_role("button", name="Sign in", exact=True).click()

    headers = {"Authorization": "Bearer " + credential}
    endpoint = origin + "/api/v1/firewall/aliases"
    try:
        sign_in("page-firewall-alias-edit")
        expect(page.get_by_role("button", name="Save alias", exact=True)).to_be_visible()
        expect(page.get_by_role("combobox", name="Saved alias")).to_have_count(0)
        assert page.request.get(endpoint, headers=headers).status == 403
        page.get_by_label("Alias name", exact=True).fill("ScopedMembers")
        page.get_by_label("Members 1", exact=True).fill("192.0.2.91")
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda r: r.url == endpoint and r.request.method == "PUT") as saved:
            page.get_by_role("button", name="Save alias", exact=True).click()
        assert saved.value.status == 200 and set(saved.value.json()["data"]) == {"revision"}
        sign_in("page-firewall-aliases")
        page.get_by_role("combobox", name="Saved alias").select_option("ScopedMembers")
        expect(page.get_by_role("button", name="Save alias", exact=True)).to_have_count(0)
        expect(page.get_by_text("192.0.2.91", exact=True)).to_be_visible()
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda r: r.url == endpoint + "/remove" and r.request.method == "PUT") as removed:
            page.get_by_role("button", name="Remove alias", exact=True).click()
        assert removed.value.status == 200
    finally:
        docker("exec", "-i", container, *cli, "restore", input=original)


def exercise_aliases(page, origin, credential, evidence=None):
    endpoint = "/api/v1/firewall/aliases"
    policy = "/api/v1/firewall/rules"
    grant = {"Authorization": "Bearer " + credential}

    def state(path=endpoint):
        response = page.request.get(origin + path, headers=grant)
        assert response.status == 200
        return response.json()["data"]

    def put(button, path):
        with page.expect_response(lambda r: r.url == origin + path and r.request.method == "PUT") as response:
            button.click()
        return response.value

    alias_link = lambda: menu_link(page, "Firewall", "Address aliases")
    alias_link().click()
    chooser = page.get_by_role("combobox", name="Saved alias")
    alias = "Lab_West"
    save = page.get_by_role("button", name="Save alias", exact=True)
    editor = page.locator("form").filter(has=save)
    name = editor.get_by_label("Alias name", exact=True)
    group = editor.get_by_role("group", name="Members", exact=True)
    first = group.get_by_label("Members 1", exact=True)
    second = group.get_by_label("Members 2", exact=True)
    initial = state()
    old_rules = state(policy)["rules"]
    assert alias not in [entry["name"] for entry in initial["aliases"]]

    writes = []

    def observe(request):
        if request.method == "PUT" and request.url.startswith(origin + endpoint):
            writes.append(request)

    page.on("request", observe)
    name.fill("port")
    first.fill("192.0.2.11")
    save.click()
    expect(editor.locator('[data-error-for="name"]')).to_have_text(re.compile(r".+"))
    name.fill(alias)
    first.fill("192.0.2.11/24")
    save.click()
    expect(group).to_have_attribute("aria-invalid", "true")
    assert not writes and state() == initial
    first.fill("@Lab_Missing")
    refused = put(save, endpoint)
    assert refused.status == 422 and "members" in refused.json()["error"]["fields"] and state() == initial
    first.fill("192.0.2.11")
    group.get_by_role("button", name="Add item").click()
    second.fill("2001:db8::/64")
    assert put(save, endpoint).status == 200
    expect(chooser).to_have_value(alias)
    expect(name).to_have_attribute("readonly", "")
    group.get_by_role("button", name="Move up").nth(1).click()
    assert put(save, endpoint).status == 200
    def lookup(draft):
        return next(entry["members"] for entry in draft["aliases"] if entry["name"] == alias)

    ordered = state()
    assert lookup(ordered) == ["2001:db8::/64", "192.0.2.11"]
    expect(editor.locator('[name="revision"]')).to_have_value(ordered["revision"])
    expect(first).to_have_value("2001:db8::/64")

    # Another authenticated writer updates the same alias after the editor obtained its revision.
    second_writer = ["192.0.2.12", "2001:db8::/64"]
    changed = page.request.put(origin + endpoint, headers=grant | {"Origin": origin, "X-FreeSense-Command": "saveAddressAlias"},
                               data={"name": alias, "revision": state()["revision"], "members": second_writer})
    assert changed.status == 200
    first.fill("192.0.2.13")
    assert put(save, endpoint).status == 409 and lookup(state()) == second_writer

    menu_link(page,"Firewall","Firewall").click()
    page.get_by_role("button", name="Add rule", exact=True).click()
    rule = page.get_by_role("group", name=f"Rule {len(old_rules) + 1}", exact=True)
    rule.get_by_role("combobox", name="Action", exact=True).select_option("pass")
    rule.get_by_label("Source", exact=True).fill("@" + alias)
    rule.get_by_label("Destination", exact=True).fill("192.0.2.254")
    rule.get_by_label("Enabled", exact=True).check()
    save_rules = page.get_by_role("button", name="Save rules", exact=True)
    assert put(save_rules, policy).status == 200
    assert state(policy)["rules"][-1]["source"] == "@" + alias

    alias_link().click()
    chooser.select_option(alias)
    expect(first).to_have_value(second_writer[0])
    if evidence:
        page.screenshot(path=str(evidence / "firewall-address-alias.png"), full_page=True)
    remove = page.get_by_role("button", name="Remove alias", exact=True)
    before_cancel = len(writes)
    page.once("dialog", lambda dialog: dialog.dismiss())
    remove.click()
    expect(remove).to_be_enabled()
    assert len(writes) == before_cancel and lookup(state()) == second_writer

    def accept(dialog):
        assert alias in dialog.message
        dialog.accept()

    page.once("dialog", accept)
    denied = put(remove, endpoint + "/remove")
    assert denied.status == 422 and set(denied.json()["error"]["fields"]) == {"name"}
    expect(page.locator("form").filter(has=remove).locator('[data-error-for="name"]')).to_have_text(re.compile(r".+"))
    assert lookup(state()) == second_writer

    menu_link(page,"Firewall","Firewall").click()
    rule.locator(":scope > [data-row-actions]").get_by_role("button", name="Remove", exact=True).click()
    assert put(save_rules, policy).status == 200 and state(policy)["rules"] == old_rules
    alias_link().click()
    chooser.select_option(alias)
    page.once("dialog", accept)
    assert put(remove, endpoint + "/remove").status == 200 and state() == initial
    expect(chooser).to_have_value("")
    page.remove_listener("request", observe)
    print("Real address aliases passed syntax, nested-reference rejection, order, conflict, saved rule reference, and confirmed removal.")
