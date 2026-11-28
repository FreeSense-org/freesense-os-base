import json
from pathlib import Path
from xml.etree import ElementTree

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_dhcp_leases(page, origin, key, container, docker, evidence=None):
    route = '/status/dhcp-leases'
    endpoint = origin + '/api/v1' + route
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    auth = {'Authorization': 'Bearer ' + key}

    def login(grant, scope, extra=(), reservation='02:00:00:00:22:11'):
        configuration = ElementTree.fromstring(original)
        if reservation:
            lan = ElementTree.SubElement(ElementTree.SubElement(configuration, 'dhcpd'), 'lan')
            mapping = ElementTree.SubElement(lan, 'staticmap')
            for field, value in {'id': '1' * 32, 'mac': reservation, 'address': '192.0.2.53', 'hostname': 'test-printer'}.items():
                ElementTree.SubElement(mapping, field).text = value
        user = configuration.find('./system/user')
        for privilege in user.findall('priv'):
            user.remove(privilege)
        for name in ['page-system-restapi-keys', grant, *extra]:
            ElementTree.SubElement(user, 'priv').text = name
        configuration.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ElementTree.tostring(configuration))
        page.goto(origin + route)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in').click()
        expect(page.locator('#workspace')).to_be_visible()

    index = '/check/prefix/FreeSense/web/index.php'
    fixture = Path(__file__).resolve().parents[1] / 'freesense/tests/fixtures/dhcp-lease-browser.php'

    def upload(path, data):
        docker('exec','-i',container,'php','-r','file_put_contents($argv[1],stream_get_contents(STDIN));',path,input=data)

    upload(index,fixture.read_bytes())

    try:
        login('page-services-dhcpserver', 'services.dhcp:write')
        assert page.request.get(endpoint + '?limit[]=1', headers=auth).status == 403
        login('page-status-dhcpleases', 'status:read')
        expect(page.get_by_text('The DHCPv4 server is stopped.')).to_be_visible()
        assert page.request.get(endpoint + '?limit=101', headers=auth).status == 422
        assert page.request.get(endpoint + '?after=::1', headers=auth).status == 422
        assert page.request.put(endpoint, headers=auth).status == 405
        count = []
        lease = {'address': '192.0.2.53', 'mac': '02:00:00:00:22:11', 'hostname':'test-printer',
                 'reserved':True, 'state':'active', 'starts':1795314000, 'expires':1795350000}

        def paginate(request):
            count.append(request.request.url)
            second = 'after=192.0.2.53' in request.request.url
            row = (lease | {'address':'192.0.2.65', 'hostname':'lab-client', 'reserved':False}) if second else lease
            request.fulfill(json={'data':{'state':'running','leases':[row],'next':None if second else lease['address']}})

        page.route(endpoint + '*', paginate)
        limit = page.get_by_label('Rows per page')
        limit.fill('101')
        page.get_by_role('button', name='Filter', exact=True).click()
        expect(limit).to_have_attribute('aria-invalid', 'true')
        assert not count
        limit.fill('1')
        page.get_by_role('button', name='Filter', exact=True).click()
        expect(page.get_by_role('cell',name='test-printer')).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / 'dhcp-leases.png', full_page=True)
        page.get_by_role('link', name='Next page').click()
        expect(page.get_by_role('cell',name='lab-client')).to_be_visible()
        assert count[-2:] == [endpoint + '?limit=1',endpoint + '?limit=1&after=192.0.2.53']
        page.get_by_role('link', name='First page').click()
        expect(page.get_by_role('cell',name='test-printer')).to_be_visible()
        page.unroute(endpoint + '*',paginate)
        dynamic = lease | {'address':'192.0.2.65','hostname':'lab-client','reserved':False,'mac':'02:00:00:00:22:12'}
        upload('/check/leases.json',json.dumps([lease | {'reservation': {'mac': lease['mac']}},dynamic]).encode())
        editor_grant = 'page-services-dhcpserver-editstaticmapping'
        mapping = origin + '/api/v1/services/dhcp/lan/mappings'
        seed_url = mapping + '/edit?lease=192.0.2.65'
        login('page-status-dhcpleases','status:read')
        actual = page.request.get(endpoint, headers=auth)
        assert 'lab-client' in actual.text(), (actual.status, actual.text(), docker('exec', container, 'ls', '/check').decode())
        expect(page.get_by_role('cell',name='lab-client')).to_be_visible()
        leases = page.get_by_role('table')
        header = leases.get_by_role('columnheader', name='IP address')
        expect(header).to_have_attribute('aria-sort', 'ascending')
        expect(leases.get_by_role('row').nth(1)).to_contain_text('192.0.2.53')
        header.get_by_role('button').click()
        expect(header).to_have_attribute('aria-sort', 'descending')
        expect(leases.get_by_role('row').nth(1)).to_contain_text('192.0.2.65')
        host_column = leases.get_by_role('columnheader', name='Hostname')
        host_column.get_by_role('button').click()
        expect(host_column).to_have_attribute('aria-sort', 'ascending')
        expect(leases.get_by_role('row').nth(1)).to_contain_text('lab-client')
        assert header.get_attribute('aria-sort') is None
        if evidence:
            page.screenshot(path=evidence / 'sortable-leases.png', full_page=True)
        search = page.get_by_role('searchbox', name='Search this page by IP, MAC or hostname')
        search.fill('LAB-client')
        expect(page.get_by_role('cell', name='lab-client')).to_be_visible()
        expect(page.get_by_role('cell', name='test-printer')).to_be_hidden()
        search.fill('02:00:00:00:22:11')
        expect(page.get_by_role('cell', name='test-printer')).to_be_visible()
        expect(page.get_by_role('cell', name='lab-client')).to_be_hidden()
        state = page.get_by_role('combobox', name='Lease state')
        state.select_option('expired')
        expect(page.get_by_text('No matching leases on this page.')).to_be_visible()
        state.select_option('active')
        search.fill('192.0.2.65')
        expect(page.get_by_role('cell', name='lab-client')).to_be_visible()
        expect(page.get_by_text('1 of 2 leases on this page')).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / 'lease-search.png', full_page=True)
        expect(page.get_by_role('link',name='Add static mapping for 192.0.2.65')).to_have_count(0)
        assert 'reservation' not in actual.json()['data']['leases'][0]
        reserved_url = mapping + '/edit?reservation=192.0.2.53'
        assert page.request.get(reserved_url,headers=auth).status == 403
        before = docker('exec', container, 'cat', '/check/lease-reads')
        assert page.request.get(seed_url,headers=auth).status == 403
        assert docker('exec', container, 'cat', '/check/lease-reads') == before
        login(editor_grant,'services.dhcp:write')
        before = docker('exec', container, 'cat', '/check/lease-reads')
        assert page.request.get(seed_url,headers=auth).status == 403
        assert docker('exec', container, 'cat', '/check/lease-reads') == before
        login('page-status-dhcpleases','status:read services.dhcp:write',extra=(editor_grant,))
        expect(page.get_by_role('link',name='Add static mapping for 192.0.2.53')).to_have_count(0)
        page.get_by_role('link', name='Edit static mapping for 192.0.2.53').click()
        expect(page.get_by_role('heading', name='Edit static mapping', exact=True)).to_be_visible()
        expect(page.get_by_label('Hostname',exact=True)).to_have_value('test-printer')
        expect(page.get_by_label('IP address',exact=True)).to_have_value('192.0.2.53')
        if evidence:
            page.screenshot(path=evidence / 'existing-reservation.png', full_page=True)
        page.get_by_label('Hostname', exact=True).fill('revised-printer')
        with page.expect_response(lambda response: response.url == mapping + '/edit' and response.request.method == 'PUT') as edit:
            page.get_by_role('button',name='Save mapping',exact=True).click()
        assert edit.value.status == 200, edit.value.text()
        login('page-status-dhcpleases','status:read services.dhcp:write',extra=(editor_grant,),reservation='02:00:00:00:22:19')
        assert page.request.get(reserved_url,headers=auth).status == 404
        login('page-status-dhcpleases','status:read services.dhcp:write',extra=(editor_grant,))
        page.get_by_role('link',name='Add static mapping for 192.0.2.65').click()
        expect(page.get_by_role('heading', name='Add static mapping', exact=True)).to_be_visible()
        expect(page.get_by_label('MAC address',exact=True)).to_have_value(dynamic['mac'])
        expect(page.get_by_label('Hostname',exact=True)).to_have_value('lab-client')
        expect(page.get_by_label('IP address',exact=True)).to_have_value('')
        assert dynamic['mac'] not in page.url and 'lab-client' not in page.url
        assert page.request.get(mapping+'/edit?lease=192.0.2.53',headers=auth).status == 404
        assert page.request.get(mapping+'/edit?lease=192.0.2.66',headers=auth).status == 404
        assert page.request.get(mapping+'/edit?lease=999.1.1.1',headers=auth).status == 422
        assert page.request.get(seed_url+'&lease=192.0.2.53',headers=auth).status == 400
        if evidence:
            page.screenshot(path=evidence / 'lease-to-mapping.png', full_page=True)
        with page.expect_response(lambda response:response.url==mapping and response.request.method=='PUT') as persisted:
            page.get_by_role('button',name='Save mapping',exact=True).click()
        assert persisted.value.status == 200, persisted.value.text()
        config = ElementTree.fromstring(docker('exec',container,*cli,'backup'))
        assert config.find('.//staticmap[hostname="lab-client"]/mac').text == dynamic['mac']
        print('Chromium DHCP lease cursor, validation, stopped state and authorized lease-to-mapping workflow passed.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
