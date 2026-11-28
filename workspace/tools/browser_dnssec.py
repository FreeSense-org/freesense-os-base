"""DNS Resolver general and advanced DNSSEC controls, through the production form registry."""
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_dnssec(page, url, key, _container, _docker, evidence):
    page.goto(url + "/services/dns/advanced")
    fill_api_key(page, key)
    page.get_by_role("button", name="Sign in").click()
    headers = {"Authorization": "Bearer " + key, "Origin": url}
    general = url + "/api/v1/services/dns/settings"
    advanced = url + "/api/v1/services/dns/advanced"
    view = url + "/api/v1/services/dns/forwarders"
    options = page.request.get(advanced, headers=headers).json()["data"]
    initial = page.request.get(view, headers=headers).json()["data"]["service"]
    assert not initial["dnssec"] and not options["dnssecstripped"]
    messages = []
    page.on("request", lambda req: messages.append(req.url) if req.method == "PUT" else None)
    harden = page.get_by_label("Harden DNSSEC Data", exact=True)
    harden.check()
    page.get_by_role("button", name="Save advanced settings").click()
    expect(page.get_by_text("Enable DNSSEC Support in General Settings before enabling Harden DNSSEC Data.", exact=True)).to_be_visible()
    assert not messages
    official = headers | {"X-FreeSense-Command": "saveResolverOptions"}
    denied = page.request.put(advanced, headers=official, data=options | {"dnssecstripped": True})
    assert denied.status == 422
    assert page.request.get(advanced, headers=headers).json()["data"] == options
    page.get_by_role("link", name="General Settings", exact=True).click()
    page.get_by_label("Enable DNSSEC Support", exact=True).check()
    with page.expect_response(lambda r: r.url == general and r.request.method == "PUT") as saved:
        page.get_by_role("button", name="Save service settings").click()
    assert saved.value.status == 200 and saved.value.json()["data"]["dnssec"] is True
    assert page.request.put(general, headers=headers | {"X-FreeSense-Command":"saveResolverService"}, data=initial).status == 409
    page.get_by_role("link", name="Advanced Settings", exact=True).click()
    labels = ("Prefetch DNS Key Support", "Harden DNSSEC Data", "Aggressive NSEC")
    for label in labels:
        page.get_by_label(label, exact=True).check()
    with page.expect_response(lambda r: r.url == advanced and r.request.method == "PUT") as updated:
        page.get_by_role("button", name="Save advanced settings").click()
    assert updated.value.status == 200
    approved = updated.value.json()["data"]
    for name in ("prefetchkey","dnssecstripped","aggressivensec"):
        assert approved[name] is True
    expect(page.get_by_text("Advanced settings saved.", exact=True)).to_be_visible()
    if evidence:
        page.screenshot(path=evidence / "dnssec-advanced.png", full_page=True)
    page.get_by_role("link", name="General Settings", exact=True).click()
    expect(page.get_by_label("Enable DNSSEC Support", exact=True)).to_be_checked()
    if evidence:
        page.screenshot(path=evidence / "dnssec-general.png", full_page=True)
    page.get_by_label("Enable DNSSEC Support", exact=True).uncheck()
    with page.expect_response(lambda r: r.url == general and r.request.method == "PUT") as disabled:
        page.get_by_role("button", name="Save service settings").click()
    assert disabled.value.status == 200 and disabled.value.json()["data"] == initial
    assert page.request.get(advanced, headers=headers).json()["data"] == approved
    assert page.request.put(advanced, headers=official, data=options | {"revision":approved["revision"]}).json()["data"] == options
    print("Chromium verified central DNSSEC AJAX saves, browser/server hardening prerequisites, stale revisions and reversible independent settings.")

