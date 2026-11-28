"""Check the settings grant can edit only a draft and the preview changes on the next authorized policy review."""
from xml.etree import ElementTree as ET
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_default_rule_log(page,origin,key,container,docker,evidence=None):
    cli = ('php','/check/prefix/sbin/freesense-config')
    original = docker('exec',container,*cli,'backup')
    url = origin+'/api/v1/logs/settings/firewall'
    settings = origin+'/status/logs/settings'
    headers = {'Authorization':'Bearer '+key,'Origin':origin,'X-FreeSense-Command':'saveDefaultRuleLogging'}

    def login():
        page.goto(settings)
        fill_api_key(page, key)
        page.get_by_role('button',name='Sign in').click()
        expect(page.locator('#workspace')).to_be_visible()

    try:
        doc = ET.fromstring(original)
        user = doc.find('./system/user')
        for priv in user.findall('priv'): user.remove(priv)
        for grant in ['page-system-restapi-keys','page-diagnostics-logs-settings']: ET.SubElement(user,'priv').text = grant
        doc.find('./restapi/apitoken/scopes').text = 'logs:write'
        log = ET.SubElement(doc,'syslog'); ET.SubElement(log,'logdefaultblock')
        ET.SubElement(log,'nolognginx'); ET.SubElement(log,'default_log_level').text = 'notice'
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        login()
        block = page.get_by_label('Packets blocked by the default block rule',exact=True)
        passes = page.get_by_label('Packets passed by the default pass rules',exact=True)
        expect(block).to_be_checked(); expect(passes).not_to_be_checked()
        initial = page.request.get(url,headers=headers).json()['data']
        block.uncheck(); passes.check()
        page.get_by_role('button',name='Save firewall logging',exact=True).click()
        expect(page.get_by_text('Firewall logging draft saved.',exact=False)).to_be_visible()
        after = page.request.get(url,headers=headers).json()['data']
        assert after['logdefaultpass'] and not after['logdefaultblock']
        assert page.request.put(url,headers=headers,data=initial).status == 409
        assert page.request.put(url,headers=headers,data={**after,'logdefaultblock':'off'}).status == 422
        assert page.request.put(url,headers=headers,data={**after,'logsnort2c':True}).status == 422
        assert page.request.get(origin+'/api/v1/firewall/rules',headers=headers).status == 403
        assert 'saveRules' not in page.request.get(origin+'/api/v1/forms',headers=headers).json()['data']['operations']
        saved = ET.fromstring(docker('exec',container,*cli,'backup'))
        assert saved.find('./syslog/nologdefaultpass') is not None and saved.find('./syslog/nologdefaultblock') is not None
        assert saved.find('./syslog/nolognginx') is not None and saved.findtext('./syslog/default_log_level') == 'notice'
        if evidence: page.screenshot(path=str(evidence/'what-to-log.png'),full_page=True)
        saved.find('./restapi/apitoken/scopes').text = 'logs:read'
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(saved)); login()
        expect(page.get_by_role('button',name='Save firewall logging')).to_have_count(0)
        expect(page.get_by_text('Default passes',exact=True)).to_be_visible()
        assert page.request.put(url,headers=headers,data=after).status == 403
        saved.find('./system/user').remove(saved.find("./system/user/priv[.='page-diagnostics-logs-settings']"))
        ET.SubElement(saved.find('./system/user'),'priv').text = 'api-logs-write'
        saved.find('./restapi/apitoken/scopes').text = 'logs:write'
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(saved))
        assert page.request.get(url,headers=headers).status == 403 and page.request.put(url,headers=headers,data=after).status == 403
        print('Chromium verified firewall log draft switches, scope, stale revocation boundaries and independent settings.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
