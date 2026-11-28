"""Exercise the installed Optional TFTP settings through the real management router."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from browser_shell import menu_link
from playwright.sync_api import expect


def exercise_tftp_settings(page, origin, credential, container, docker, evidence=None):
    endpoint = "/api/v1/packages/tftp-settings"
    write = endpoint + "/settings"
    headers = {"Authorization": "Bearer " + credential}

    def open_settings():
        menu_link(page,"Services","Services: TFTP Server").click()
        expect(page).to_have_url(origin + "/packages/tftp-settings")
        expect(page.get_by_role("heading", name="TFTP listener", exact=True)).to_be_visible()

    def state():
        response = page.request.get(origin + endpoint, headers=headers)
        assert response.status == 200
        return response.json()["data"]["values"]

    def save():
        with page.expect_response(lambda r: r.url.endswith(write) and r.request.method == "PUT") as saved:
            page.get_by_role("button", name="Save listener settings", exact=True).click()
        return saved.value

    open_settings()
    enabled = page.get_by_label("Enable read-only TFTP", exact=True)
    address = page.get_by_label("Local listening address", exact=True)
    block = page.get_by_label("Maximum block size (bytes)", exact=True)
    logging = page.get_by_label("Log transfer requests", exact=True)
    expect(enabled).not_to_be_checked()
    expect(address).to_have_value("127.0.0.1")
    expect(block).to_have_value("512")
    expect(page.get_by_role("button", name="Apply saved listener settings", exact=True)).to_be_visible()
    enabled.check()
    address.fill("192.0.2.243")
    failure = save()
    assert failure.status == 422 and "address" in failure.json()["error"]["fields"]
    expect(page.locator('[data-error-for="address"]')).to_contain_text("currently configured")
    assert not state()["enabled"]
    address.fill("127.0.0.1")
    block.fill("1468")
    logging.check()
    with page.expect_response(lambda r: r.url.endswith(endpoint) and r.request.method == "GET"):
        assert save().status == 200
    expect(block).to_have_value("1468")
    current = state()
    assert current["enabled"] and current["logging"] and current["address"] == "127.0.0.1" and current["blockSize"] == 1468
    if evidence:
        page.screenshot(path=str(evidence / "tftp-listener-settings.png"), full_page=True)

    # A second authenticated writer changes the settings while the browser still holds its first revision.
    concurrent = page.request.put(origin + write, headers=headers | {"Origin": origin,
        "X-FreeSense-Command": "package.tftp-settings.save"}, data=current | {"blockSize": 1024})
    assert concurrent.status == 200
    block.fill("512")
    assert save().status == 409
    assert state()["blockSize"] == 1024

    # Simulate a saved assignment whose NIC is now absent in this isolated namespace.
    docker("exec", container, "php", "-r", """
require "/check/prefix/FreeSense/autoload.php";
$s = new FreeSense\\Config\\ConfigurationStore(getenv("FREESENSE_CONFIG_DIR"));
$s->editDocument(static function(DOMDocument $d): void {
    $n=(new DOMXPath($d))->query("/freesense/installedpackages/tftpd/listener")->item(0);
    if (!$n instanceof DOMElement || $n->getAttribute("address")!=="127.0.0.1") exit(1);
    $n->setAttribute("address","192.0.2.243");
});""")
    open_settings()
    expect(address).to_have_value("192.0.2.243")
    enabled.uncheck()
    assert save().status == 200
    disabled = state()
    assert disabled["enabled"] is False and disabled["address"] == "192.0.2.243" and disabled["blockSize"] == 1024
    print("TFTP listener browser saved a local address, rejected an absent address at its field, detected a concurrent save and disabled an unavailable assignment.")
