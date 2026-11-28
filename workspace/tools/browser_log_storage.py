"""Save rotation policy through the AJAX command and inspect the exact private newsyslog rule."""
from xml.etree import ElementTree as ET
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_log_storage(page,origin,key,container,docker,evidence=None):
    cli = ('php','/check/prefix/sbin/freesense-config')
    original = docker('exec',container,*cli,'backup')
    path = '/status/logs/settings'
    endpoint = origin+'/api/v1/logs/settings/storage'
    headers = {'Authorization':'Bearer '+key,'Origin':origin,'X-FreeSense-Command':'saveLogStorage'}
    name = '/var/run/freesense-filterlog/rotation.conf'
    def login():
        page.goto(origin+path)
        fill_api_key(page, key)
        page.get_by_role('button',name='Sign in').click()
        expect(page.locator('#workspace')).to_be_visible()
    def expand():
        page.get_by_text('Storage and rotation',exact=True).click()
    try:
        doc = ET.fromstring(original)
        user = doc.find('./system/user')
        ET.SubElement(user,'priv').text = 'page-diagnostics-logs-settings'
        doc.find('./restapi/apitoken/scopes').text = 'logs:write'
        syslog = ET.SubElement(doc,'syslog')
        ET.SubElement(syslog,'format').text = 'rfc5424'
        ET.SubElement(syslog,'remoteserver').text = 'logs.example.test'
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        login(); expand()
        count = page.get_by_label('Log retention count',exact=True)
        size = page.get_by_label('Log rotation size (bytes)',exact=True)
        codec = page.locator('select[name=logcompressiontype]')
        save = page.get_by_role('button',name='Save storage settings')
        expect(count).to_have_value('7'); expect(size).to_have_value('512000'); expect(codec).to_have_value('bzip2')
        old = page.request.get(endpoint,headers=headers).json()['data']
        body = {key:old[key] for key in ['revision','rotatecount','logfilesize','logcompressiontype']}
        count.fill('100'); save.click(); expect(count).to_have_attribute('aria-invalid','true')
        assert page.request.put(endpoint,headers=headers,data={**body,'logcompressiontype':'zip'}).status == 422
        assert page.request.put(endpoint,headers=headers,data={**body,'format':'rfc3164'}).status == 422
        count.fill('12'); size.fill('1048576'); codec.select_option('gzip'); save.click()
        expect(page.get_by_text('Log rotation settings saved.',exact=True)).to_be_visible()
        assert page.request.put(endpoint,headers=headers,data=body).status == 409
        assert docker('exec',container,'cat',name).strip() == b'/var/log/filter.log root:wheel 600 12 1024 * ZC'
        actual = ET.fromstring(docker('exec',container,*cli,'backup'))
        assert actual.findtext('./syslog/format') == 'rfc5424' and actual.findtext('./syslog/remoteserver') == 'logs.example.test'
        expand(); expect(count).to_have_value('12')
        expect(page.get_by_text('Effective firewall log policy:',exact=False)).to_contain_text('gzip, 12 retained generations')
        if evidence: page.screenshot(path=str(evidence/'storage-and-rotation.png'),full_page=True)
        # Exercise a failed native write without changing the unrelated target. A retry of the saved revision repairs it.
        docker('exec',container,'php','-r',
               'rename($argv[1],$argv[1].".prior"); symlink($argv[1].".prior",$argv[1]);',name)
        revision = page.request.get(endpoint,headers=headers).json()['data']['revision']
        retry = {**body,'revision':revision,'logcompressiontype':'none'}
        refused = page.request.put(endpoint,headers=headers,data=retry)
        assert refused.status == 503 and refused.json()['error']['code'] == 'rotation_pending'
        assert docker('exec',container,'cat',name+'.prior').strip() == b'/var/log/filter.log root:wheel 600 12 1024 * ZC'
        docker('exec',container,'php','-r','unlink($argv[1]);',name)
        retry['revision'] = page.request.get(endpoint,headers=headers).json()['data']['revision']
        assert page.request.put(endpoint,headers=headers,data=retry).status == 200
        assert docker('exec',container,'cat',name).strip() == b'/var/log/filter.log root:wheel 600 7 500 * C'
        actual = ET.fromstring(docker('exec',container,*cli,'backup'))
        actual.find('./restapi/apitoken/scopes').text = 'logs:read'
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(actual))
        login(); expand()
        expect(page.get_by_text('Effective firewall log policy:',exact=False)).to_be_visible()
        expect(save).to_have_count(0)
        assert page.request.put(endpoint,headers=headers,data=body).status == 403
        actual.find('./system/user').remove(actual.find("./system/user/priv[.='page-diagnostics-logs-settings']"))
        ET.SubElement(actual.find('./system/user'),'priv').text = 'api-logs-write'
        actual.find('./restapi/apitoken/scopes').text = 'logs:write'
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(actual))
        assert page.request.get(endpoint,headers=headers).status == 403
        assert page.request.put(endpoint,headers=headers,data=body).status == 403
        print('Chromium confirmed storage draft, scoped save, private newsyslog rule, persisted fields and read-only access.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
