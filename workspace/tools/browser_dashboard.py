"""Verify the personal dashboard through the routed shell, production API and authorized snapshots."""
import re
import hashlib
import json
import secrets
from pathlib import Path
from xml.etree import ElementTree as ET
from browser_shell import fill_api_key
from playwright.sync_api import expect

OPTIONS = re.compile('^Options for ')


def exercise_dashboard(page, origin, key, container, docker, evidence):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    doc = ET.fromstring(original)
    doc.find('./interfaces/lan/if').text = 'igc1'
    doc.find('./interfaces/wan/if').text = 'igc0'
    owner = doc.find('./system/user')
    for grant in ('page-system-login-logout','page-status-interfaces','page-status-dhcpleases'):
        ET.SubElement(owner,'priv').text = grant
    doc.find('./restapi/apitoken/scopes').text = 'status:read'
    other = ET.SubElement(doc.find('system'),'user')
    ET.SubElement(other,'name').text = 'independent-operator'
    for grant in ('page-system-restapi-keys','page-system-login-logout','page-status-interfaces'):
        ET.SubElement(other,'priv').text=grant

    def credential(username, suffix, scope, readonly=False):
        secret=secrets.token_urlsafe(32)
        token=ET.SubElement(doc.find('restapi'),'apitoken')
        for field,value in [('username',username),('id',suffix),('hash',hashlib.sha256(secret.encode()).hexdigest()),('scopes',scope)]:
            ET.SubElement(token,field).text=value
        if readonly: ET.SubElement(token,'readonly')
        return 'fsk_'+suffix+'_'+secret

    narrow=credential(owner.findtext('name'),'000000000002','firewall.rules:read')
    independent=credential('independent-operator','000000000003','status:read')
    readonly=credential(owner.findtext('name'),'000000000004','status:read',True)
    url=origin+'/api/v1/dashboard/layout'
    def headers(token,command=None):
        result={'Origin':origin,'Authorization':'Bearer '+token}
        if command:result['X-FreeSense-Command']=command
        return result
    def read(token):
        res=page.request.get(url,headers=headers(token));assert res.status==200
        return res.json()['data']
    def save(token,body,reset=False):
        return page.request.put(url+('/reset' if reset else ''),headers=headers(token,'resetDashboardLayout' if reset else 'saveDashboardLayout'),data=body)
    def login(token):
        page.goto(origin+'/dashboard')
        fill_api_key(page, token)
        page.get_by_role('button',name='Sign in').click()
        expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible()
    def card(title):
        return page.get_by_role('article',name=title,exact=True)
    def saving(action):
        # A menu choice, a drag in edit mode or an added widget is saved at once (one PUT of the layout).
        with page.expect_response(lambda res:res.url==url and res.request.method=='PUT') as response:
            action()
        assert response.value.status==200
        expect(page.locator('.fs-dash [inert]')).to_have_count(0)
        return read(key)
    def add(title):
        page.get_by_role('button',name='Add widget',exact=True).click()
        drawer=page.get_by_role('dialog',name='Add widget')
        return saving(lambda: drawer.get_by_role('button',name=title,exact=True).click())
    def choose(title,item,role='menuitem'):
        card(title).get_by_role('button',name=OPTIONS).click()
        return saving(lambda: card(title).get_by_role(role,name=item,exact=True).click())
    def done():
        page.get_by_role('button',name='Done',exact=True).click()
        expect(page.get_by_role('button',name='Done',exact=True)).to_have_count(0)
        return read(key)

    try:
        fixtures=Path(__file__).resolve().parents[1]/'freesense/tests/fixtures'
        index='/check/prefix/FreeSense/web/index.php'
        def file(path, data):
            docker('exec','-i',container,'php','-r','file_put_contents($argv[1],stream_get_contents(STDIN));',path,input=data)
        file(index,(fixtures/'gateway-status-browser.php').read_bytes())
        # Supply the repository's synthetic FreeBSD observation to the normal backend in this disposable Linux container.
        fixture = Path(__file__).resolve().parents[1] / 'freesense/tests/fixtures/interfaces.txt'
        docker('exec','-i',container,'sh','-ec','test ! -e /sbin/ifconfig && cat > /check/ifconfig.txt && printf \'%s\\n\' \'#!/bin/sh\' \'exec cat /check/ifconfig.txt\' > /sbin/ifconfig && chmod 755 /sbin/ifconfig',input=fixture.read_bytes())
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        saved_xml=docker('exec',container,*cli,'backup')
        login(key)
        baseline=read(key)
        expect(card('Interfaces')).to_have_count(1)
        expect(card('Interfaces').get_by_text('igc1',exact=True)).to_be_visible()
        add('Quick actions'); add('DHCP leases')
        expect(card('DHCP leases').get_by_text('The DHCPv4 server is stopped.')).to_be_visible()
        # Dragging needs edit mode; the drop is saved at once.
        page.get_by_role('button',name='Edit layout',exact=True).click()
        assert [r['type'] for r in saving(lambda: card('DHCP leases').get_by_role('heading').drag_to(card('Quick actions')))['widgets']]==['interfaces','dhcp-leases','quick-actions']
        done()
        # The widget menu works without edit mode; the keyboard reaches its items.
        card('DHCP leases').get_by_role('button',name=OPTIONS).click()
        item=card('DHCP leases').get_by_role('menuitem',name='Move later',exact=True)
        expect(item).to_be_visible()
        item.focus()
        assert [r['type'] for r in saving(lambda: page.keyboard.press('Enter'))['widgets']]==['interfaces','quick-actions','dhcp-leases']
        if evidence:
            page.screenshot(path=evidence/'dashboard.png',full_page=True)
        choose('Quick actions','Move earlier')
        choose('Interfaces','Full width','menuitemradio')
        card('Interfaces').get_by_role('button',name=OPTIONS).click()
        card('Interfaces').get_by_role('menuitem',name='Settings…',exact=True).click()
        settings=page.get_by_role('dialog',name='Interfaces settings')
        settings.get_by_role('checkbox',name='Show interfaces without link').uncheck()
        saving(lambda: settings.get_by_role('button',name='Save',exact=True).click())
        configured=choose('Interfaces','Collapse')
        assert [r['type'] for r in configured['widgets']]==['quick-actions','interfaces','dhcp-leases']
        assert configured['widgets'][1]['size']=='full' and configured['widgets'][1]['collapsed'] and not configured['widgets'][1]['showDown']
        page.get_by_role('button',name='Pause live updates',exact=True).click()
        expect(page.get_by_role('button',name='Resume live updates',exact=True)).to_have_text('Paused')
        login(key);expect(card('Quick actions').get_by_role('link',name='Interfaces',exact=True)).to_be_visible()
        if evidence:page.screenshot(path=evidence/'persisted-layout.png',full_page=True)
        assert save(key,{'revision':baseline['revision'],'widgets':[]}).status==409
        assert save(key,{'revision':configured['revision'],'widgets':[configured['widgets'][1]]*2}).status==422
        assert read(independent)['widgets']==baseline['widgets']
        partial=read(narrow)
        assert set(partial['catalogue'])=={'quick-actions'} and [r['type'] for r in partial['widgets']]==['quick-actions']
        assert save(narrow,{'revision':partial['revision'],'widgets':[configured['widgets'][1]]}).status==422
        assert save(narrow,{'revision':partial['revision']},True).status==200
        remaining=read(key)
        assert remaining['widgets']==configured['widgets'][1:]
        login(readonly)
        expect(page.locator('#workspace')).to_be_visible()
        expect(page.get_by_role('button',name='Edit layout')).to_have_count(0)
        assert save(readonly,{'revision':remaining['revision'],'widgets':[]}).status==403
        assert save(key,{'revision':remaining['revision'],'username':'independent-operator','widgets':[]}).status==422
        login(key)
        choose('Interfaces','Expand')
        choose('DHCP leases','Remove widget')
        saved=add('Interfaces'); assert card('Interfaces').count()==2 and len(saved['widgets'])==2
        page.get_by_role('button',name='Edit layout').click()
        if evidence:page.screenshot(path=evidence/'edit-layout.png',full_page=True)
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_response(lambda res:res.url==url+'/reset') as reset:
            page.get_by_role('button',name='Reset to default').click()
        assert reset.value.status==200 and reset.value.json()['data']==baseline
        assert docker('exec',container,*cli,'backup')==saved_xml
        # Join the normal router with the gateway status fixture when exercising its independent history integration.
        ET.SubElement(owner,'priv').text='page-status-gateways'
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        file('/check/gateway-samples',json.dumps({'sampled_at':1700000222,'gateways':[{
            'id':'wan4','name':'Campus','role':'wan','device':'igc0','family':4,'gateway':'192.0.2.254','monitor':'192.0.2.254',
            'description':'Campus uplink','default':True,'state':'online','reason':'Responding','latency_ms':1.5,'deviation_ms':0.25,'loss_percent':0}]}).encode())
        history=(fixtures/'gateway-quality-seed.php')
        if history.is_file():
            file('/check/seed-dashboard-history.php',history.read_bytes())
            docker('exec',container,'php','/check/seed-dashboard-history.php')
        login(key)
        expect(card('Gateways').get_by_role('cell',name='Campus',exact=True)).to_be_visible()
        expect(card('Gateways').get_by_role('cell',name='1.5 ms',exact=True)).to_be_visible()
        if history.is_file():
            expect(card('Gateways').get_by_role('img')).to_have_count(2)
        if evidence:
            page.screenshot(path=evidence/'dashboard-gateways.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            expect(card('Interfaces')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=evidence/'dashboard-mobile.png',full_page=True)
        print('Chromium verified widget add, copies, order, size, collapse, link option, live pause, remove, persistence, reset, owner isolation and scoped/readonly controls.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)

