"""Verify the routed System widget with synthetic FreeBSD observations and the production reader."""
import re
import hashlib
import secrets
import time
from pathlib import Path
from xml.etree import ElementTree as ET
from browser_shell import fill_api_key
from playwright.sync_api import expect

OPTIONS = re.compile('^Options for ')


def exercise_dashboard_system(page,origin,key,container,docker,evidence):
    cli=['php','/check/prefix/sbin/freesense-config']
    original=docker('exec',container,*cli,'backup')
    doc=ET.fromstring(original)
    system=doc.find('system')
    ET.SubElement(system,'hostname').text='gateway'
    ET.SubElement(system,'domain').text='office.test'
    ET.SubElement(system,'timezone').text='Europe/Copenhagen'
    ET.SubElement(system.find('user'),'priv').text='page-system-login-logout'
    doc.find('./restapi/apitoken/scopes').text='system:read'
    secret=secrets.token_urlsafe(32); token=ET.SubElement(doc.find('restapi'),'apitoken')
    for name,value in [('id','000000000002'),('username',system.findtext('./user/name')),('hash',hashlib.sha256(secret.encode()).hexdigest()),('scopes','status:read')]:
        ET.SubElement(token,name).text=value
    narrow='fsk_000000000002_'+secret
    fixtures=Path(__file__).resolve().parents[1]/'freesense/tests/fixtures'
    public=fixtures/'system-widget-sysctl.txt'
    def put(path,data):
        docker('exec','-i',container,'php','-r','file_put_contents($argv[1],stream_get_contents(STDIN));',path,input=data)
    def read_counts(path):
        return int(docker('exec',container,'php','-r',"echo is_file($argv[1])?count(file($argv[1])):0;",path))
    def login(token):
        page.goto(origin+'/dashboard')
        fill_api_key(page, token)
        page.get_by_role('button',name='Sign in').click()
        expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible()
    def get(path,token):
        return page.request.get(origin+path,headers={'Authorization':'Bearer '+token})
    try:
        put('/check/prefix/FreeSense/web/index.php',(fixtures/'system-widget-browser.php').read_bytes())
        # The fixture router requires the shared fake system facts next to the installed library.
        put('/check/prefix/FreeSense/FakeSystemFacts.php',(fixtures.parent/'FakeSystemFacts.php').read_bytes())
        docker('exec',container,'mkdir','/check/system-etc')
        put('/check/system-etc/version',b'2.0.0-DEVELOPMENT\n')
        put('/check/system-etc/resolv.conf',b'nameserver 192.0.2.53\nnameserver 2001:db8::53\n')
        put('/check/system-sysctl',public.read_bytes())
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        login(key)
        card=page.get_by_role('article',name='System',exact=True)
        expect(card.get_by_text('gateway.office.test',exact=True)).to_be_visible()
        expect(card.get_by_text('2.0.0-DEVELOPMENT',exact=True)).to_be_visible()
        expect(card.get_by_text('2 GiB of 4 GiB',exact=True)).to_be_visible()
        expect(card.get_by_text('128 MiB of 1 GiB',exact=True)).to_be_visible()
        expect(card.get_by_text('Kernel',exact=True)).to_be_visible()
        expect(card.get_by_text('Update available',exact=True)).to_have_count(0)
        put('/check/cpu-advance',b'0')
        expect(card.get_by_role('progressbar',name='CPU',exact=True)).to_have_attribute('aria-valuenow','35',timeout=16000)
        expect(card.get_by_text('user 20%, system 10%, interrupt 5%')).to_be_visible()
        # The CPU chart gets the same readings through the live channel's REST fallback.
        expect(card.get_by_role('img',name=re.compile('User [0-9.]+%'))).to_be_visible(timeout=16000)
        assert read_counts('/check/info-reads')==1
        if evidence: page.screenshot(path=evidence/'system-widget.png',full_page=True)
        page.get_by_role('button',name='Pause live updates',exact=True).click()
        before=read_counts('/check/live-reads');time.sleep(5.2)
        assert read_counts('/check/live-reads')==before
        page.get_by_role('button',name='Edit layout').click()
        card.get_by_role('button',name=OPTIONS).click()
        card.get_by_role('menuitemradio',name='Medium',exact=True).click()
        expect(card.get_by_text('Kernel',exact=True)).to_have_count(0)
        card.get_by_role('button',name=OPTIONS).click()
        card.get_by_role('menuitem',name='Settings…',exact=True).click()
        page.get_by_role('dialog').get_by_label('Rows',exact=True).select_option('full')
        page.get_by_role('dialog').get_by_role('button',name='Save',exact=True).click()
        expect(card.get_by_text('Kernel',exact=True)).to_be_visible()
        card.get_by_role('button',name=OPTIONS).click()
        card.get_by_role('menuitem',name='Collapse',exact=True).click()
        expect(card.get_by_text('gateway.office.test')).to_have_count(0)
        page.get_by_role('button',name='Done').click()
        login(key)
        expect(card.get_by_role('heading',name='System')).to_be_visible()
        expect(card.get_by_text('gateway.office.test')).to_have_count(0)
        page.get_by_role('button',name='Edit layout').click()
        card.get_by_role('button',name=OPTIONS).click()
        card.get_by_role('menuitem',name='Settings…',exact=True).click()
        expect(page.get_by_role('dialog').get_by_label('Rows',exact=True)).to_have_value('full')
        page.get_by_role('dialog').get_by_role('button',name='Cancel',exact=True).click()
        card.get_by_role('button',name=OPTIONS).click()
        card.get_by_role('menuitem',name='Expand',exact=True).click()
        expect(card.get_by_text('FreeBSD 15.0-RELEASE',exact=True)).to_be_visible()
        page.set_viewport_size({'width':390,'height':850})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        if evidence: page.screenshot(path=evidence/'system-widget-mobile.png',full_page=True)
        for path in ['/api/v1/system/info','/api/v1/status/system']:
            assert get(path,narrow).status==403
            assert get(path+'?unregistered=yes',key).status==400
        login(narrow)
        expect(page.get_by_role('article',name='System',exact=True)).to_have_count(0)
        page.get_by_role('button',name='Add widget').click()
        expect(page.get_by_role('button',name='System',exact=True)).to_have_count(0)
        assert 'systemInfo' not in get('/api/v1/forms',narrow).json()['data'].get('reads',{})
        print('Chromium verified default System placement, measured CPU usage, memory and swap, cached identity, pause, personal size/Rows/collapse, narrow grants and mobile layout.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
