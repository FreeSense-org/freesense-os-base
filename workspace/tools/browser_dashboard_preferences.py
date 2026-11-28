"""Exercise the personal polling default and independent layout reset in the routed browser."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
from pathlib import Path
import secrets
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_dashboard_preferences(page, origin, key, container, docker, evidence):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    doc = ET.fromstring(original)
    system = doc.find('system')
    ET.SubElement(system,'hostname').text = 'gateway'
    ET.SubElement(system,'domain').text = 'lab.test'
    owner = system.find('user')
    other = ET.SubElement(system,'user')
    ET.SubElement(other,'name').text = 'second-operator'
    ET.SubElement(other,'priv').text = 'page-system-restapi-keys'
    for account in (owner,other):
        for grant in ('page-system-login-logout','page-status-interfaces'):
            ET.SubElement(account,'priv').text = grant
    scopes = 'system:read status:read'
    doc.find('./restapi/apitoken/scopes').text = scopes

    def credential(account, identifier, readonly=False):
        secret = secrets.token_urlsafe(32)
        token = ET.SubElement(doc.find('restapi'),'apitoken')
        for field,value in [('username',account.findtext('name')),('id',identifier),('scopes',scopes),('hash',sha256(secret.encode()).hexdigest())]:
            ET.SubElement(token,field).text = value
        if readonly:
            ET.SubElement(token,'readonly')
        return 'fsk_'+identifier+'_'+secret

    second = credential(other,'000000000002')
    readonly = credential(owner,'000000000003',True)
    layout = origin+'/api/v1/dashboard/layout'
    prefs = origin+'/api/v1/me/preferences'
    reset = layout+'/reset'
    fixtures = Path(__file__).resolve().parents[1]/'freesense/tests/fixtures'
    ticket_requests = []
    reset_requests = []
    def note(request):
        if request.url == origin+'/api/v1/status/live-ticket':
            ticket_requests.append(request.method)
        if request.url == reset:
            reset_requests.append(request.method)
    page.on('request',note)

    def write_file(path, data):
        docker('exec','-i',container,'php','-r','file_put_contents($argv[1],stream_get_contents(STDIN));',path,input=data)
    def reads():
        return int(docker('exec',container,'php','-r',"echo is_file('/check/live-reads') ? count(file('/check/live-reads')) : 0;"))
    def get(url, token):
        response = page.request.get(url,headers={'Authorization':'Bearer '+token})
        assert response.status==200
        return response.json()['data']
    def write(url, token, command, body):
        return page.request.put(url,headers={'Origin':origin,'Authorization':'Bearer '+token,'X-FreeSense-Command':command},data=body)
    def save_layout(token, rows):
        response = write(layout,token,'saveDashboardLayout',{'revision':get(layout,token)['revision'],'widgets':rows})
        assert response.status==200
        return response.json()['data']
    def login(path, token):
        page.goto(origin+path)
        fill_api_key(page, token)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()

    updates = page.get_by_role('combobox',name='Dashboard updates')
    reset_button = page.get_by_role('button',name='Reset my dashboard',exact=True)
    resume = page.get_by_role('button',name='Resume live updates',exact=True)
    pause = page.get_by_role('button',name='Pause live updates',exact=True)
    def confirm_reset(status):
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_response(lambda response:response.url==reset and response.request.method=='PUT') as result:
            reset_button.click()
        assert result.value.status==status
        return result.value

    try:
        write_file('/check/prefix/FreeSense/web/index.php',(fixtures/'system-widget-browser.php').read_bytes())
        # The fixture router requires the shared fake system facts next to the installed library.
        write_file('/check/prefix/FreeSense/FakeSystemFacts.php',(fixtures.parent/'FakeSystemFacts.php').read_bytes())
        docker('exec',container,'mkdir','/check/system-etc')
        write_file('/check/system-etc/version',b'2.0.0-DEVELOPMENT\n')
        write_file('/check/system-etc/resolv.conf',b'nameserver 192.0.2.53\n')
        write_file('/check/system-sysctl',(fixtures/'system-widget-sysctl.txt').read_bytes())
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        baseline = get(layout,key)
        system_only = [row for row in baseline['widgets'] if row['type']=='system']
        assert len(system_only)==1
        mine = save_layout(key,system_only)
        theirs = save_layout(second,[{'id':'b'*32,'type':'quick-actions','size':'sm','collapsed':False}])
        login('/account/preferences',key)
        expect(updates).to_have_value('on')
        expect(updates.locator('option')).to_have_text(['Live: widgets keep updating','Paused: widgets load once; press Live to resume'])
        expect(reset_button).to_be_enabled()
        browser_title = page.get_by_role('combobox',name='Browser tab text')
        expect(browser_title).to_have_value('system')
        expect(page).to_have_title('gateway.lab.test - Account')
        browser_title.select_option('page')
        order = page.get_by_role('combobox',name='Interface sorting')
        expect(order).to_have_value('assigned')
        order.select_option('name')
        updates.select_option('off')
        with page.expect_response(lambda response:response.url==prefs and response.request.method=='PUT') as response:
            page.get_by_role('button',name='Save',exact=True).click()
        assert response.value.status==200
        saved = response.value.json()['data']
        assert saved['live']=='off' and get(prefs,second)['live']=='on'
        assert saved['browser_title']=='page' and get(prefs,second)['browser_title']=='system'
        expect(page).to_have_title('Account - gateway.lab.test')
        assert get(layout,key)==mine and get(layout,second)==theirs
        if evidence:
            page.screenshot(path=evidence/'preferences.png',full_page=True)

        assert saved['interface_order']=='name' and get(prefs,second)['interface_order']=='assigned'
        path = origin+'/api/v1/status/nics'
        names = ['West 10','East','West 2']
        nics = [{'name':str(i),'description':name,'device':'vtnet'+str(i),'enabled':True}
                for i,name in enumerate(names)]
        page.route(path,lambda route:route.fulfill(json={'data':nics}))
        for token, expected in ((key,['East','West 2','West 10']), (second,names)):
            login('/interfaces',token)
            expect(page.locator('#content tbody tr td:first-child')).to_have_text(expected)
        page.unroute(path)

        login('/dashboard',key)
        card = page.get_by_role('article',name='System',exact=True)
        expect(resume).to_be_visible()
        expect(card.get_by_text('gateway.lab.test',exact=True)).to_be_visible()
        assert reads()==1 and not ticket_requests
        page.wait_for_timeout(5600)
        assert reads()==1 and not ticket_requests
        if evidence:
            page.screenshot(path=evidence/'paused-dashboard.png',full_page=True)
        with page.expect_response(lambda res:res.url==origin+'/api/v1/status/system'):
            resume.click()
        expect(pause).to_be_visible()
        assert reads()==2 and ticket_requests==['POST']
        with page.expect_response(lambda res:res.url==origin+'/api/v1/status/system',timeout=13000):
            pass
        pause.click()
        count = reads()
        page.wait_for_timeout(5600)
        assert count>=3 and reads()==count
        resume.click()
        expect(pause).to_be_visible()
        login('/dashboard',key)
        expect(resume).to_be_visible()
        assert get(prefs,key)['revision']==saved['revision']

        login('/account/preferences',key)
        expect(reset_button).to_be_enabled()
        page.once('dialog',lambda dialog:dialog.dismiss())
        reset_button.click()
        assert not reset_requests and get(layout,key)==mine
        changed = save_layout(key,[dict(system_only[0],size='md')])
        confirm_reset(409)
        assert get(layout,key)==changed and get(layout,second)==theirs
        page.get_by_role('button',name='Refresh',exact=True).click()
        confirm_reset(200)
        expect(page.get_by_text('Your dashboard is back to the default layout.',exact=True)).to_be_visible()
        assert get(layout,key)==baseline and get(layout,second)==theirs
        assert get(prefs,key)['revision']==saved['revision'] and get(prefs,second)['live']=='on'
        assert write(reset,key,'resetDashboardLayout',{'revision':baseline['revision'],'username':'second-operator'}).status==422
        login('/account/preferences',readonly)
        expect(updates).to_be_disabled()
        expect(updates).to_have_value('off')
        expect(reset_button).to_have_count(0)
        assert write(reset,readonly,'resetDashboardLayout',{'revision':baseline['revision']}).status==403
        widget = {'id':'c'*32,'type':'interfaces','size':'lg','collapsed':False,'showDown':True}
        page.route(path,lambda route:route.fulfill(json={'data':nics}))
        for token, expected in ((key,['East','West 2','West 10']), (second,names)):
            save_layout(token,[widget])
            login('/dashboard',token)
            card = page.get_by_role('article',name='Interfaces',exact=True)
            expect(card.locator('.fs-nic strong')).to_have_text(expected)
        page.unroute(path)
        print('Chromium verified natural interface ordering on Status and Dashboard, and one snapshot while paused, resumed polling and ticket request, a temporary Live toggle, '
              'personal defaults, isolated owner layouts, and canceled, stale, successful and forbidden reset commands.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
