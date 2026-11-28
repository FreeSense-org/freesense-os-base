"""Exercise the central LAN DHCP mapping form, list and separate grants."""
from xml.etree import ElementTree
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_dhcp_mappings(page, origin, key, container, docker, evidence=None):
    cli = ["php","/check/prefix/sbin/freesense-config"]
    backup = docker("exec",container,*cli,"backup")
    endpoint = origin + "/api/v1/services/dhcp/lan/mappings"
    site = "/services/dhcp/lan/mappings"
    auth = {"Authorization":"Bearer "+key}
    pool = "page-services-dhcpserver"
    editor = pool+"-editstaticmapping"

    def grant(names, scope):
        doc = ElementTree.fromstring(docker("exec",container,*cli,"backup"))
        user = doc.find("./system/user")
        for old in user.findall("priv"): user.remove(old)
        for name in ["page-system-restapi-keys",*names]: ElementTree.SubElement(user,"priv").text = name
        doc.find("./restapi/apitoken/scopes").text = scope
        if doc.find("dhcpd") is None:
            doc.append(ElementTree.fromstring("<dhcpd><lan><enable/><range><from>192.0.2.10</from><to>192.0.2.20</to></range></lan></dhcpd>"))
        docker("exec","-i",container,*cli,"restore",input=ElementTree.tostring(doc))
    def login(path):
        page.goto(origin+path)
        fill_api_key(page, key)
        page.get_by_role("button",name="Sign in",exact=True).click()
    def write(command, suffix, data):
        return page.request.put(endpoint+suffix,headers=auth|{"Origin":origin,"X-FreeSense-Command":command},data=data)
    try:
        settings = "page-services-dhcpserver-settings"
        global_url = origin + "/api/v1/services/dhcp/registration"
        grant([settings],"services.dhcp:read")
        login("/services/dhcp/settings")
        expect(page.get_by_text("System domain",exact=True)).to_be_visible()
        expect(page.get_by_role("button",name="Save registration settings")).to_have_count(0)
        assert page.request.get(endpoint,headers=auth).status==403
        grant([settings],"services.dhcp:write")
        login("/services/dhcp/settings")
        page.get_by_label("Enable early DNS registration",exact=True).check()
        before=page.request.get(global_url,headers=auth).json()["data"]
        with page.expect_response(lambda r:r.url==global_url and r.request.method=="PUT") as registration:
            page.get_by_role("button",name="Save registration settings").click()
        assert registration.value.status==200,registration.value.text()
        assert page.request.get(global_url,headers=auth).json()["data"]["enabled"] is True
        stale=page.request.put(global_url,headers=auth|{"Origin":origin,"X-FreeSense-Command":"saveDhcpRegistration"},
                               data={"revision":before["revision"],"enabled":False})
        assert stale.status==409
        if evidence: page.screenshot(path=evidence/"dhcp-early-dns-registration.png",full_page=True)
        grant([editor,pool],"services.dhcp:write")
        assert page.request.get(global_url,headers=auth).status==403
        login("/services/dhcp/lan")
        page.get_by_role("link",name="Static Mappings",exact=True).click()
        page.get_by_role("link",name="Add static mapping").click()
        submit=page.get_by_role("button",name="Save mapping",exact=True)
        sent=[]
        page.on("request", lambda r:sent.append(r.method) if r.url.startswith(endpoint) and r.method=="PUT" else None)
        submit.click()
        expect(page.get_by_text("Supply at least one of these two fields.")).to_be_visible()
        for name,value in [("MAC address","02-00-00-00-AB-CD"),("IP address","192.0.2.15"),("Hostname","Office-Print"),
                           ("Description","Office <printer>")]:
            page.get_by_label("Include "+name,exact=True).check()
            page.get_by_label(name,exact=True).fill(value)
        submit.click()
        expect(page.get_by_text("Choose an address outside the dynamic DHCP pool.")).to_be_visible()
        assert not sent
        page.get_by_label("IP address",exact=True).fill("192.0.2.100")
        page.get_by_label("Include DNS registration domain",exact=True).check()
        page.get_by_label("DNS registration domain",exact=True).fill("invalid..name")
        submit.click()
        assert not sent
        page.get_by_label("DNS registration domain",exact=True).fill("Map.Example")
        page.get_by_label("Include Early DNS registration",exact=True).check()
        page.get_by_role("combobox",name="Early DNS registration",exact=True).select_option("enable")
        with page.expect_response(lambda r:r.url==endpoint and r.request.method=="PUT") as response: submit.click()
        assert response.value.status==200,response.value.text()
        expect(page.get_by_role("cell",name="office-print",exact=True)).to_be_visible()
        draft=page.request.get(endpoint,headers=auth).json()["data"]
        row,=draft["mappings"]
        assert row["mac"]=="02:00:00:00:ab:cd" and row["address"]=="192.0.2.100"
        assert row["registrationDomain"]=="map.example" and row["registrationPolicy"]=="enable"
        rejected=write("createDhcpMapping","",{"revision":draft["revision"],"clientId":"01:02","hostname":"x"*63,
                                              "registrationDomain":("a"*63+".")*3+"zone"})
        assert rejected.status==422 and "registrationDomain" in rejected.json()["error"]["fields"],rejected.text()
        for extra,field in [({"mac":row["mac"]},"mac"),({"clientId":"00:10","address":"192.0.2.15"},"address"),
                            ({"mac":"01:00:00:00:00:01"},"mac"),({"clientId":"0x0"},"clientId")]:
            reject=write("createDhcpMapping","",{"revision":draft["revision"]}|extra)
            assert reject.status==422 and field in reject.json()["error"]["fields"],reject.text()
        if evidence: page.screenshot(path=evidence/"dhcp-static-mappings.png",full_page=True)
        page.get_by_role("link",name="Edit",exact=True).click()
        expect(page.get_by_label("Hostname",exact=True)).to_have_value("office-print")
        if evidence: page.screenshot(path=evidence/"dhcp-static-mapping-editor.png",full_page=True)
        page.get_by_label("Include MAC address",exact=True).uncheck()
        page.get_by_label("Include Client identifier",exact=True).check()
        page.get_by_label("Client identifier",exact=True).fill("0x010A0B")
        with page.expect_response(lambda r:r.url==endpoint+"/edit" and r.request.method=="PUT") as edited: submit.click()
        assert edited.value.status==200,edited.value.text()
        expect(page.get_by_role("cell",name="01:0a:0b",exact=True)).to_be_visible()
        assert write("removeDhcpMapping","/remove",{"revision":draft["revision"],"id":row["id"]}).status==409
        grant([editor],"services.dhcp:write")
        assert page.request.get(endpoint,headers=auth).status==403
        one=page.request.get(endpoint+"/edit/"+row["id"],headers=auth)
        assert one.status==200 and one.json()["data"]["mapping"]["id"]==row["id"]
        rev=one.json()["data"]["revision"]
        assert write("removeDhcpMapping","/remove",{"revision":rev,"id":row["id"]}).status==403
        grant([pool],"services.dhcp:read")
        login(site)
        expect(page.get_by_role("cell",name="office-print",exact=True)).to_be_visible()
        expect(page.get_by_role("button",name="Delete office-print")).to_have_count(0)
        assert page.request.get(endpoint+"/edit/"+row["id"],headers=auth).status==403
        grant([pool],"services.dhcp:write")
        login(site)
        expect(page.get_by_role("link",name="Edit",exact=True)).to_have_count(0)
        page.on("dialog",lambda dialog:dialog.accept())
        page.get_by_role("button",name="Delete office-print").click()
        expect(page.get_by_text("No static mappings on this interface yet.")).to_be_visible()
        print("Chromium DHCP mappings and DNS registration passed authorized global settings, per-mapping controls, client/server validation, lifecycle and stale revisions.")
    finally:
        docker("exec","-i",container,*cli,"restore",input=backup)
