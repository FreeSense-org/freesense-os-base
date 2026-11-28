"""Exercise the authenticated local state-table workflow using synthetic numeric FreeBSD records."""
import json
from pathlib import Path
from xml.etree import ElementTree

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_states(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    index = '/check/prefix/FreeSense/web/index.php'
    docker('exec', container, 'cp', index, '/check/original-state-index')
    router = Path(__file__).resolve().parents[1] / 'freesense/tests/fixtures/state-browser.php'
    docker('exec', '-i', container, 'php', '-r', 'file_put_contents($argv[1],stream_get_contents(STDIN));', index, input=router.read_bytes())
    auth = {'Authorization': 'Bearer ' + key}
    endpoint = origin + '/api/v1/diagnostics/states'
    def state_key(address1, port1, address0, port0):
        return {'address1': address1, 'port1': port1, 'address0': address0, 'port0': port0}

    # The kernel rows the helper returns: an inbound redirect (floating, rule 7) and an outbound IPv6 UDP state (em0, rule 2).
    # Each carries the wire and stack keys, the raw state levels and the counters.
    records = [
        {'id': 1, 'creator': 1, 'interface': 'all', 'family': 'inet', 'protocol': 6, 'direction': 'in',
         'wire': state_key('198.51.100.1', 20400, '198.51.100.2', 40400), 'stack': state_key('192.0.2.2', 30400, '198.51.100.2', 40400),
         'source_state': 4, 'destination_state': 4, 'rule': 7, 'age': 2, 'expires': 28, 'packets': [4, 2], 'bytes': [238, 147]},
        {'id': 2, 'creator': 1, 'interface': 'em0', 'family': 'inet6', 'protocol': 17, 'direction': 'out',
         'wire': state_key('2001:db8:1::2', 40402, '2001:db8:2::2', 30401), 'stack': state_key('2001:db8:1::2', 40402, '2001:db8:2::2', 30401),
         'source_state': 2, 'destination_state': 1, 'rule': 2, 'age': 2, 'expires': 28, 'packets': [4, 2], 'bytes': [238, 147]},
    ]
    text = json.dumps(records)

    def store_records(body):
        docker('exec', '-i', container, 'php', '-r', 'file_put_contents("/check/state-records",stream_get_contents(STDIN));',
               input=body.encode())

    def login(grant, scope, path='/overview'):
        doc = ElementTree.fromstring(original)
        user = doc.find('./system/user')
        for privilege in user.findall('priv'):
            user.remove(privilege)
        for privilege in ['page-system-restapi-keys', grant]:
            ElementTree.SubElement(user, 'priv').text = privilege
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ElementTree.tostring(doc))
        page.goto(origin + path)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in').click()
        expect(page.locator('#workspace')).to_be_visible()

    def read_count():
        return docker('exec', container, 'cat', '/check/state-reads').count(b'\n')

    nav = page.get_by_role('link', name='Diagnostics: States')
    apply = page.get_by_role('button', name='Filter', exact=True)
    search = page.get_by_label('Filter expression', exact=True)
    rule = page.get_by_label('Rule IDs', exact=True)
    family = page.get_by_label('Interface', exact=True)
    try:
        store_records(text)
        docker('exec', container, 'touch', '/check/state-reads')
        assert page.request.get(endpoint + '?x[]=1').status == 401
        for grant, scope in [('page-status-interfaces', 'status:read'), ('page-diagnostics-showstates', 'status:read'),
                             ('page-status-interfaces', 'diagnostics:read')]:
            login(grant, scope, '/diagnostics/states?filter=private')
            expect(nav).to_have_count(0)
            expect(search).to_have_count(0)
            assert page.request.get(endpoint + '?interface=wan&interface=lan', headers=auth).status == 403
            assert 'reads' not in page.request.get(origin + '/api/v1/forms', headers=auth).json()['data']
        assert read_count() == 0
        login('page-diagnostics-showstates', 'diagnostics:read')
        menu_link(page,'Diagnostics','Diagnostics: States').click()
        expect(page.get_by_text('Original: 198.51.100.2:40400 → 198.51.100.1:20400')).to_be_visible()
        expect(page.get_by_text('[2001:db8:1::2]:40402 → [2001:db8:2::2]:30401')).to_be_visible()
        expect(page.get_by_role('cell', name='238 / 147')).to_have_count(2)
        if evidence:
            page.screenshot(path=str(evidence / 'diagnostic-states.png'), full_page=True)
        for value in ['198.51.100.1:20400', '192.0.2.2:30400', 'TCP']:
            search.fill(value)
            apply.click()
            expect(page.get_by_role('cell', name='TCP · IPv4')).to_have_count(1)
            expect(page.get_by_role('cell', name='UDP · IPv6')).to_have_count(0)
            expect(page).to_have_url(origin + '/diagnostics/states?filter=' + value.replace(':', '%3A'))
        search.fill('no-matching-peer')
        apply.click()
        expect(page.get_by_text('No matching firewall states.')).to_be_visible()
        page.go_back()
        expect(search).to_have_value('TCP')
        expect(page.get_by_role('cell', name='TCP · IPv4')).to_be_visible()
        search.fill('')
        rule.fill('2')
        apply.click()
        expect(page.get_by_role('cell', name='UDP · IPv6')).to_be_visible()
        family.select_option('lan')
        reads = read_count()
        apply.click()
        expect(rule).to_have_attribute('aria-invalid', 'true')
        rule.fill('1,1')
        family.select_option('all')
        apply.click()
        expect(rule).to_have_attribute('aria-invalid', 'true')
        assert read_count() == reads
        for raw, status in [('ruleid=1&ruleid=2', 400), ('ruleid%5B%5D=1', 400), ('x=1', 400), ('filter=%04', 422),
                            ('ruleid=4294967296', 422), ('interface=lan&ruleid=2', 422), ('limit=501', 422)]:
            assert page.request.get(endpoint + '?' + raw, headers=auth).status == status
        assert page.request.delete(endpoint, headers=auth).status == 405 and read_count() == reads
        login('api-diagnostics-read', 'diagnostics:read', '/diagnostics/states?limit=1')
        expect(page.get_by_text('Partial results', exact=False)).to_be_visible()
        expect(page.get_by_label('Limit', exact=True)).to_have_value('1')
        search.fill('198.51.100.1')
        store_records('unreadable native output\n')
        apply.click()
        expect(page.get_by_role('alert')).to_contain_text('unavailable')
        expect(search).to_have_value('198.51.100.1')
        expect(page.get_by_text('No matching firewall states.')).to_have_count(0)
        store_records(text)
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.get_by_text('Original: 198.51.100.2:40400 → 198.51.100.1:20400')).to_be_visible()
        print('States browser verified owner grants and scopes, closed methods, NAT/IPv6, native IDs, filters, bounds, history, failure and refresh.')
    finally:
        docker('exec', container, 'cp', '/check/original-state-index', index)
        docker('exec', '-i', container, *cli, 'restore', input=original)
