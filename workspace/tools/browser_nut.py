"""Drive the installed UPS status, authenticated settings and native-independent validation."""
from xml.etree import ElementTree
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_nut(page,origin,key,container,docker,evidence=None):
    cli=["php","/check/prefix/sbin/freesense-config"]
    original=docker("exec",container,*cli,"backup")
    endpoint=origin+"/api/v1/packages/nut-settings"
    headers={"Authorization":"Bearer "+key}
    def grant(scope,enable=True):
        doc=ElementTree.fromstring(docker("exec",container,*cli,"backup"))
        user=doc.find("./system/user")
        for old in user.findall("priv"): user.remove(old)
        ElementTree.SubElement(user,"priv").text="page-system-restapi-keys"
        if enable: ElementTree.SubElement(user,"priv").text="page-services-nut"
        doc.find("./restapi/apitoken/scopes").text=scope
        docker("exec","-i",container,*cli,"restore",input=ElementTree.tostring(doc))
    def login(path):
        page.goto(origin+path)
        fill_api_key(page, key)
        page.get_by_role("button",name="Sign in",exact=True).click()
    try:
        docker("exec",container,"cp","-R","/optional/sysutils/FreeSense-pkg-nut/files/usr/local/.","/check/prefix/")
        grant("packages.nut:read")
        login("/services/ups")
        expect(page.get_by_text("UPS monitoring is not enabled")).to_be_visible()
        if evidence: page.screenshot(path=evidence/"ups-status.png",full_page=True)
        page.get_by_role("link",name="UPS Settings",exact=True).click()
        expect(page.get_by_role("button",name="Save UPS settings")).to_have_count(0)
        grant("packages.nut:write")
        login("/services/ups/settings")
        form=page.locator("form").filter(has=page.get_by_role("button",name="Save UPS settings"))
        form.get_by_role("combobox",name="UPS type",exact=True).select_option("dummy")
        name=form.get_by_label("UPS name",exact=True)
        name.fill("-leading")
        submit=form.get_by_role("button",name="Save UPS settings")
        sent=[]
        page.on("request",lambda r:sent.append(r.method) if r.url==endpoint+"/settings" and r.method=="PUT" else None)
        submit.click()
        assert not sent
        name.fill("lab_ups")
        prior=page.request.get(endpoint,headers=headers).json()["data"]["values"]["revision"]
        with page.expect_response(lambda r:r.url==endpoint+"/settings" and r.request.method=="PUT") as result: submit.click()
        assert result.value.status==200,result.value.text()
        expect(page.get_by_text("UPS settings saved. Apply to update the monitor.")).to_be_visible()
        current=page.request.get(endpoint,headers=headers).json()["data"]["values"]
        assert current["name"]=="lab_ups" and current["mode"]=="dummy"
        body={"revision":current["revision"],"mode":"dummy","name":"lab_ups","shutdown":"poweroff","alerts":True}
        command=headers|{"Origin":origin,"X-FreeSense-Command":"package.nut-settings.save"}
        refused=page.request.put(endpoint+"/settings",headers=command,data=body)
        assert refused.status==422 and "shutdown" in refused.json()["error"]["fields"],refused.text()
        assert page.request.put(endpoint+"/settings",headers=command,data=body|{"revision":prior}).status==409
        if evidence: page.screenshot(path=evidence/"ups-settings.png",full_page=True)
        grant("packages.nut:write",False)
        assert page.request.get(endpoint,headers=headers).status==403
        assert page.request.get(origin+"/api/v1/packages/nut",headers=headers).status==403
        print("Chromium UPS Services pages, read and write grants, central client/server checks, saved mode and stale revisions passed.")
    finally:
        docker("exec","-i",container,*cli,"restore",input=original)
