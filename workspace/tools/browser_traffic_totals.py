"""Exercise the registered traffic reports, database presentation and independent write grant."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree
from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_traffic_totals(page, origin, credential, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    docker('exec', container, 'cp', '-R', '/optional/net/FreeSense-pkg-Status_Traffic_Totals/files/usr/local/.', '/check/prefix/')
    endpoint = origin + '/api/v1/packages/traffic-totals'
    ticket = origin + '/api/v1/status/live-ticket'
    live_requests = []
    page.on('request', lambda req: live_requests.append(req.method) if req.url == ticket else None)
    auth = {'Authorization':'Bearer ' + credential}
    command = auth | {'Origin':origin, 'X-FreeSense-Command':'package.traffic-totals.save'}

    def login(grant, scope):
        doc = ElementTree.fromstring(docker('exec',container,*cli,'backup'))
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for item in ['page-system-restapi-keys',*grant.split()]:
            ElementTree.SubElement(user,'priv').text = item
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec','-i',container,*cli,'restore',input=ElementTree.tostring(doc))
        page.goto(origin + '/packages')
        fill_api_key(page, credential)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        return page.get_by_role('link',name='Status: Traffic Totals',exact=True)

    def sample(route):
        # The real router supplies authentication, privileges and settings. Inject only the native history dataset.
        actual = route.fetch()
        if actual.status != 200:
            route.fulfill(response=actual)
            return
        body = actual.json()
        periods = {}
        for period in ['hour','day','month','top']:
            periods[period] = {role:[{'at':1795305600,'date':'2026-11-22','hour':0,'tx':'9007199254740995','rx':'2048'}]
                               for role in body['data']['values']['availableRoles']}
        windows = {'day':[['2026-11-22','2026-11-22']], 'hour':[[1795305600,'2026-11-22 00:00']], 'month':[['2026-11','2026-11']]}
        body['data']['values']['history'] = {'state':'available','periods':periods,'windows':windows}
        route.fulfill(response=actual,json=body)

    try:
        expect(login('page-services-avahi','packages.traffic-totals:write')).to_have_count(0)
        assert page.request.get(endpoint,headers=auth).status == 403
        link = login('page-status-monitoring','packages.traffic-totals:read')
        assert page.request.get(endpoint,headers=auth).json()['data']['values']['history']['state'] == 'empty'
        page.route(endpoint,sample)
        menu_link(page,'Status','Status: Traffic Totals').click()
        expect(page).to_have_url(origin+'/status/traffic-totals')
        expect(page.get_by_role('heading',name='Daily',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Save as Defaults')).to_have_count(0)
        expect(page.get_by_role('img',name='Interface traffic graph.',exact=False)).to_be_visible()
        page.get_by_role('button',name='Top 10 Days').click()
        expect(page.get_by_text('Select one interface for Top 10 Days.')).to_be_visible()
        page.get_by_label('Interface(s)',exact=True).select_option('lan')
        page.get_by_label('Type',exact=True).select_option('stacked')
        page.get_by_role('button',name='Hourly').click()
        expect(page.get_by_role('cell',name='2026-11-22 00:00')).to_be_visible()
        page.get_by_role('img',name='Interface traffic graph.',exact=False).press('Home')
        expect(page.locator('output').filter(has_text='2026-11-22 00:00')).to_contain_text('RX 2.00 KiB')
        page.locator('summary').filter(has_text='Display Advanced').click()
        with page.expect_download() as download:
            page.get_by_role('button',name='Export As CSV').click()
        raw = download.value.path().read_text(encoding='utf-8')
        assert '9007199254740995' in raw and '9007199254743043' in raw
        assert page.request.put(endpoint+'/settings',headers=command,data={}).status == 403
        assert live_requests == []
        if evidence:
            page.screenshot(path=str(evidence/'traffic-totals.png'),full_page=True)
        page.unroute(endpoint, sample)
        login('page-status-monitoring','packages.traffic-totals:write'); menu_link(page,'Status','Status: Traffic Totals').click()
        page.get_by_role('button',name='Monthly').click()
        page.get_by_label('Interface(s)',exact=True).select_option('wan')
        page.locator('summary').filter(has_text='Display Advanced').click()
        page.get_by_label('Enable Graphing',exact=True).check()
        start = page.get_by_label('Start Day',exact=True)
        start.fill('29')
        page.get_by_role('button',name='Save as Defaults').click()
        expect(page.locator('[data-error-for="startDay"]')).to_contain_text('integer')
        start.fill('7')
        with page.expect_response(lambda r:r.url == endpoint+'/settings' and r.request.method=='PUT') as answer:
            page.get_by_role('button',name='Save as Defaults').click()
        assert answer.value.status == 200
        receipt = answer.value.json()['data']
        assert receipt['roles']==['wan'] and receipt['enabled'] and receipt['startDay']==7 and receipt['period']=='month'
        fields={k:receipt[k] for k in ['revision','enabled','roles','graph','period','startDay','cumulative','inverse']}
        assert page.request.put(endpoint+'/settings',headers=command,data=fields|{'startDay':29}).status == 422
        assert page.request.put(endpoint+'/settings',headers=command,data=fields|{'startDay':3}).status == 200
        assert page.request.put(endpoint+'/settings',headers=command,data=fields).status == 409
        login('page-status-monitoring page-status-interfaces','packages.traffic-totals:write status:read'); menu_link(page,'Status','Status: Traffic Totals').click()
        page.locator('summary').filter(has_text='Reset Graphing Data').click()
        reset = page.get_by_label('Type RESET to start a new history')
        reset.fill('wrong')
        page.get_by_role('button',name='Reset Graphing Data',exact=True).click()
        expect(page.locator('[data-error-for="confirmation"]')).not_to_be_empty()
        reset.fill('RESET')
        with page.expect_response(lambda r:r.url == endpoint+'/reset' and r.request.method=='PUT') as result:
            page.get_by_role('button',name='Reset Graphing Data',exact=True).click()
        assert result.value.status == 200 and result.value.json()['data']['generation'] != receipt['generation']
        assert 'POST' in live_requests
        print('Traffic Totals Chromium verified scoped navigation, four history views, chart and CSV precision, local filtering, central settings submission and independent grants.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
