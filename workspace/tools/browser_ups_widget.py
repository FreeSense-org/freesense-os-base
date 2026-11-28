"""Exercise installed UPS discovery, conditional readings, layout visibility and native-independent rendering."""
import re
from xml.etree import ElementTree as ET

OPTIONS = re.compile('^Options for ')

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_ups_widget(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    prefix = '/check/prefix'
    package = '/optional/sysutils/FreeSense-pkg-nut/files/usr/local'
    endpoint = origin + '/api/v1/packages/nut'
    auth = {'Authorization':'Bearer '+key}
    requests = []
    page.on('request', lambda request: requests.append(request.url) if request.url == endpoint else None)

    def account(scope, grant=True, readonly=False):
        doc=ET.fromstring(original)
        user=doc.find('./system/user')
        for priv in user.findall('priv'):
            user.remove(priv)
        for privilege in ['page-system-restapi-keys','page-dashboard-all'] + (['page-services-nut'] if grant else []):
            ET.SubElement(user, 'priv').text=privilege
        if readonly:
            ET.SubElement(user,'priv').text='user-config-readonly'
        doc.find('./restapi/apitoken/scopes').text=scope
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        page.goto(origin+'/dashboard')
        fill_api_key(page, key)
        page.get_by_role('button',name='Sign in').click()
        expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible()

    def sample(route):
        # Real package auth, provider and layout are checked independently using the disabled status response.
        response=route.fetch()
        data=response.json()
        if response.status==200:
            data['data']['values']=observation
        route.fulfill(response=response,json=data)

    def layout():
        return page.request.get(origin+'/api/v1/dashboard/layout',headers=auth).json()['data']

    observation={'state':'alert','summary':'On battery, Low battery','variables':{
        'ups.status':'OB LB','ups.alarm':'Low charge','ups.mfr':'Example','ups.model':'Lab UPS',
        'battery.runtime':'3723','ups.load':'97','battery.charge':'24','input.voltage':'0',
        'output.voltage':'230.5','battery.voltage':'12.4'}}
    page.clock.install()
    try:
        account('packages.nut:read')
        assert 'pkg-nut' not in layout()['catalogue'] and not requests
        docker('exec',container,'cp','-R',package+'/.',prefix+'/')
        account('packages.nut:read')
        assert layout()['catalogue']['pkg-nut']['every']==10 and not requests
        actual=page.request.get(endpoint,headers=auth)
        assert actual.status==200 and actual.json()['data']['values']['state']=='disabled'
        page.get_by_role('button',name='Add widget').click()
        page.get_by_role('button',name='UPS',exact=True).click()
        card=page.get_by_role('article',name='UPS',exact=True)
        expect(card.get_by_text('UPS monitoring is not enabled')).to_be_visible()
        page.route(endpoint,sample)
        page.get_by_role('button',name='Pause live updates',exact=True).click()
        page.get_by_role('button',name='Resume live updates',exact=True).click()
        expect(card.get_by_text('Example Lab UPS')).to_be_visible()
        expect(card.get_by_text('The UPS requires attention')).to_be_visible()
        for expected in ['Low charge','1:02:03 (H:M:S)','0 V','230.5 V','12.4 V','24%','97%']:
            expect(card.get_by_text(expected,exact=True)).to_be_visible()
        expect(card.get_by_role('meter',name='Load')).to_have_attribute('value','97')
        expect(card.get_by_role('link',name='UPS Status')).to_have_attribute('href','/services/ups')
        card.get_by_role('button',name=OPTIONS).click()
        card.get_by_role('menuitem',name='Collapse',exact=True).click()
        expect(card.locator('.fs-widget-body')).to_have_count(0)
        before_collapse=len(requests)
        page.clock.run_for(20000)
        assert len(requests)==before_collapse
        card.get_by_role('button',name=OPTIONS).click()
        card.get_by_role('menuitem',name='Expand',exact=True).click()
        expect(card.get_by_role('meter',name='Load')).to_be_visible()
        with page.expect_response(lambda res: res.url==endpoint and res.status==200):
            page.clock.run_for(15000)
        expect(page.get_by_role('button',name='Done',exact=True)).to_have_count(0)  # adding a widget leaves edit mode off
        if evidence:
            page.screenshot(path=evidence/'ups-dashboard.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=evidence/'ups-mobile.png',full_page=True)
            page.set_viewport_size({'width':1320,'height':920})
        page.get_by_role('button',name='Pause live updates',exact=True).click()
        baseline=len(requests)
        # Advance the real scheduler without spending 10s sleeping.
        page.clock.run_for(15000)
        assert len(requests)==baseline
        observation={'state':'online','summary':'Online','variables':{'ups.load':'0','battery.runtime':'0','battery.charge':'unknown'}}
        page.get_by_role('button',name='Resume live updates',exact=True).click()
        expect(card.get_by_text('0:00:00 (H:M:S)')).to_be_visible()
        expect(card.get_by_role('meter',name='Load')).to_have_attribute('value','0')
        expect(card.get_by_role('meter',name='Battery')).to_have_count(0)
        before_revocation=len(requests)
        account('status:read')
        assert 'pkg-nut' not in layout()['catalogue']
        expect(card).to_have_count(0)
        assert len(requests)==before_revocation
        assert page.request.get(endpoint,headers=auth).status==403
        account('packages.nut:read',grant=False)
        assert 'pkg-nut' not in layout()['catalogue']
        account('packages.nut:read',readonly=True)
        expect(card.get_by_text('Online',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Edit layout')).to_have_count(0)
        path=prefix+'/share/FreeSense/web-packages/nut.json'
        docker('exec',container,'mv',path,'/check/nut.disabled')
        account('packages.nut:read')
        assert 'pkg-nut' not in layout()['catalogue']
        expect(card).to_have_count(0)
        docker('exec',container,'mv','/check/nut.disabled',path)
        account('packages.nut:read')
        expect(card.get_by_text('Online',exact=True)).to_be_visible()
        print('Chromium verified UPS discovery, real disabled state, sample meters/voltage/alarm/runtime, pause, grants, personal persistence and package reinstall.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)

