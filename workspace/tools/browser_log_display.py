"""Exercise the production log reader, distinct page grants, and central log display command."""
import hashlib
from xml.etree import ElementTree as ET
from playwright.sync_api import expect
from browser_shell import menu_link, nav_link, fill_api_key


def exercise_log_display(page, origin, key, container, docker, evidence=None):
    cli = ('php','/check/prefix/sbin/freesense-config')
    original = docker('exec',container,*cli,'backup')
    doc = ET.fromstring(original)
    settings = '/status/logs/settings'
    static = '/status/logs/firewall'
    url = origin + '/api/v1/logs/settings'
    logs = origin + '/api/v1/logs/firewall'
    base = {'Authorization':'Bearer ' + key}
    command = {**base,'Origin':origin,'X-FreeSense-Command':'saveLogDisplay'}
    user = doc.find('./system/user')
    for priv in user.findall('priv'):
        user.remove(priv)
    for name in ['page-system-restapi-keys','page-diagnostics-logs-settings','page-diagnostics-logs-firewall',
                 'page-diagnostics-logs-firewall-dynamic','api-firewall-rules-lan-read']:
        ET.SubElement(user,'priv').text = name
    token = doc.find('./restapi/apitoken/scopes')
    token.text = 'logs:write firewall.rules:read'
    firewall = ET.SubElement(doc,'firewall')
    ids = ['1'*32, '2'*32]
    trackers = [int(hashlib.sha256(b'FreeSense rule tracker\0'+i.encode()).hexdigest()[:8],16) for i in ids]
    for i,role in enumerate(['lan','wan']):
        ET.SubElement(firewall,'rule',id=ids[i],action='pass',enabled='true',source='any',destination='any',
                      interface=role,description=['LAN <staff>','private WAN rule'][i])
    records = [f'Nov 21 08:10:0{i} edge filterlog[123]: 3,,,{trackers[i%2]},em0,match,pass,in,4,0x00,,64,123,0,none,17,udp,28,192.0.2.2,192.0.2.1,4100{i},53,0' for i in range(8)]
    docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
    docker('exec','-i',container,'php','-r','file_put_contents("/var/log/filter.log",stream_get_contents(STDIN)); chmod("/var/log/filter.log",0600);',
           input=('\n'.join(records)+'\n').encode())

    def login(path):
        page.goto(origin + path)
        fill_api_key(page, key)
        page.get_by_role('button',name='Sign in').click()
        expect(page.locator('#workspace')).to_be_visible()

    def save():
        page.get_by_role('button',name='Save',exact=True).click()
        expect(page.get_by_text('Log display settings saved.',exact=True)).to_be_visible()

    def configure(grants,scopes):
        current = ET.fromstring(docker('exec',container,*cli,'backup'))
        owner = current.find('./system/user')
        for item in owner.findall('priv'):
            owner.remove(item)
        for name in ['page-system-restapi-keys',*grants]:
            ET.SubElement(owner,'priv').text = name
        current.find('./restapi/apitoken/scopes').text = scopes
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(current))

    try:
        login('/overview')
        nav_link(page,'/status/logs/settings').click()  # the System Logs menu entry opens its first log; Settings is a tab
        initial = page.request.get(url,headers=base).json()['data']
        count = page.get_by_label('GUI log entries',exact=True)
        desc = page.locator('select[name=filterdescriptions]')
        raw = page.get_by_label('Show logs as written, without formatting',exact=True)
        expect(count).to_have_value('500')
        count.fill('4')
        sent = []
        page.on('request', lambda req: sent.append(req.method) if req.url == url and req.method == 'PUT' else None)
        page.get_by_role('button',name='Save',exact=True).click()
        expect(count).to_have_attribute('aria-invalid','true')
        assert not sent
        count.fill('5'); desc.select_option('column'); save()
        if evidence: page.screenshot(path=str(evidence/'log-display.png'),full_page=True)
        body = {name:initial[name] for name in ['revision','nentries','rawfilter','reverse','filterdescriptions']}
        assert page.request.put(url,headers=command,data=body).status == 409
        assert page.request.put(url,headers=command,data={**body,'nentries':4}).status == 422
        nav_link(page,'/status/logs/firewall').click()  # the firewall log is a System Logs tab now
        cells = page.locator('#workspace tbody tr')
        expect(cells).to_have_count(5)
        expect(cells.first).to_contain_text('08:10:03')
        expect(cells.last).to_contain_text('08:10:07')
        expect(page.get_by_role('columnheader',name='Current rule description')).to_be_visible()
        expect(page.get_by_role('cell',name='LAN <staff>',exact=True).first).to_be_visible()
        visible = page.request.get(logs+'?presentation=display',headers=base)
        assert 'private WAN rule' not in visible.text() and 'LAN <staff>' in visible.text()
        assert page.request.get(logs+'?presentation=all',headers=base).status == 422
        old = page.request.get(logs,headers=base).json()['data']
        assert len(old['rows']) == 8 and old['rows'][0]['source'].endswith('41007') and 'display' not in old
        page.get_by_role('link',name='Settings',exact=True).click()
        page.get_by_label('Show newest entries first').check()
        desc.select_option('action'); raw.check(); save()
        nav_link(page,'/status/logs/firewall').click()
        expect(page.get_by_label('Raw firewall log')).to_contain_text(records[-1])
        assert page.get_by_label('Raw firewall log').inner_text().splitlines() == records[3:][::-1]
        page.get_by_role('link',name='View live firewall log').click()
        expect(cells).to_have_count(5)
        expect(cells.first).to_contain_text('08:10:07')
        expect(page.get_by_label('Raw firewall log')).to_have_count(0)
        expect(cells.nth(1).locator('td').first).to_contain_text('LAN <staff>')
        assert not any('raw' in row for row in page.request.get(logs+'/live?presentation=display',headers=base).json()['data']['rows'])
        if evidence: page.screenshot(path=str(evidence/'live-rule-descriptions.png'),full_page=True)
        configure(['page-diagnostics-logs-settings'],'logs:read')
        login(settings)
        expect(page.get_by_text('Under the action',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Save',exact=True)).to_have_count(0)
        assert page.request.put(url,headers=command,data=body).status == 403
        assert page.request.get(logs,headers=base).status == 403
        configure(['page-diagnostics-logs-firewall','api-logs-write'],'logs:write')
        login(static)
        assert page.request.get(url,headers=base).status == 403
        assert page.request.put(url,headers=command,data=body).status == 403
        assert 'saveLogDisplay' not in page.request.get(origin+'/api/v1/forms',headers=base).json()['data']['operations']
        expect(page.get_by_role('link',name='Settings',exact=True)).to_have_count(0)
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
        docker('exec',container,'php','-r','@unlink("/var/log/filter.log");')
