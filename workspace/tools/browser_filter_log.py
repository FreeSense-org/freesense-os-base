"""Read a private synthetic filterlog through the production local management router and browser."""
from xml.etree import ElementTree

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_filter_log(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    path = '/status/logs/firewall'
    endpoint = origin + '/api/v1/logs/firewall'
    auth = {'Authorization': 'Bearer ' + key}
    one = 'Nov 21 08:10:05 firewall filterlog[123]: 3,,,12,em0,match,pass,in,4,0x00,,64,123,0,none,17,udp,28,192.0.2.2,192.0.2.1,43100,33221,0'
    two = '<134>1 2026-11-21T08:10:06Z firewall filterlog 124 - - 4,,,23,em1,match,block,out,6,0x00,0x00000,64,ipv6-icmp,58,8,2001:db8::2,2001:db8::1,request,22,1'
    marker = 'web-packet-payload-marker'
    records = one + ',' + marker + '\n' + two + '\n'

    def write(body):
        docker('exec', '-i', container, 'php', '-r',
               'file_put_contents("/var/log/filter.log",stream_get_contents(STDIN)); chmod("/var/log/filter.log",0600);',
               input=body.encode())

    def login(grant, scope, target, extra_grants=()):
        doc = ElementTree.fromstring(original)
        # This regression fixture explicitly selects newest first.
        log = doc.find('syslog')
        if log is None: log = ElementTree.SubElement(doc,'syslog')
        if log.find('reverse') is None: ElementTree.SubElement(log,'reverse')
        user = doc.find('./system/user')
        for privilege in user.findall('priv'):
            user.remove(privilege)
        for privilege in ['page-system-restapi-keys', grant, *extra_grants]:
            ElementTree.SubElement(user, 'priv').text = privilege
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ElementTree.tostring(doc))
        page.goto(origin + target)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in').click()
        expect(page.locator('#workspace')).to_be_visible()

    nav = page.get_by_role('link', name='Status: System Logs: Firewall')
    search = page.get_by_label('Search text', exact=True)
    apply = page.get_by_role('button', name='Filter', exact=True)
    try:
        write(records)
        assert page.request.get(endpoint + '?bad[]=1').status == 401
        for grant, scope in [('page-diagnostics-showstates','diagnostics:read'),('page-diagnostics-logs-firewall','diagnostics:read'),
                             ('page-status-interfaces','logs:read')]:
            login(grant, scope, path + '?filter=old')
            expect(nav).to_have_count(0)
            expect(search).to_have_count(0)
            assert page.request.get(endpoint + '?x=1', headers=auth).status == 403
            forms = page.request.get(origin + '/api/v1/forms', headers=auth).json()['data']
            assert 'firewallLogs' not in forms.get('reads', {})
        login('page-diagnostics-logs-firewall', 'logs:read', '/overview')
        menu_link(page,'Status','Status: System Logs: Firewall').click()
        expect(page.get_by_role('heading', name='Firewall log')).to_be_visible()
        expect(page.get_by_role('cell', name='192.0.2.2:43100')).to_be_visible()
        expect(page.get_by_role('cell', name='2001:db8::1')).to_be_visible()
        expect(page.get_by_role('link', name='192.0.2.2:43100', exact=True)).to_have_count(0)
        expect(page.get_by_role('row').nth(1).get_by_role('cell').nth(3)).to_have_text('4Log tracker 23')
        expect(page.get_by_text('Log tracker 12', exact=True)).to_be_visible()
        data = page.request.get(endpoint, headers=auth)
        assert data.status == 200 and marker not in data.text() and data.json()['data']['rows'][0]['action'] == 'block'
        if evidence:
            page.screenshot(path=str(evidence/'firewall-log.png'), full_page=True)
        search.fill('43100')
        apply.click()
        expect(page).to_have_url(origin + path + '?filter=43100')
        expect(page.get_by_role('row')).to_have_count(2)
        page.get_by_label('Action', exact=True).select_option('block')
        apply.click()
        expect(page.get_by_text('No matching firewall events in the inspected log entries.')).to_be_visible()
        page.go_back()
        expect(search).to_have_value('43100')
        expect(page.get_by_role('cell', name='192.0.2.2:43100')).to_be_visible()
        sent = []
        page.on('request', lambda req: sent.append(req.url) if req.url.startswith(endpoint) else None)
        search.fill('x' * 101)
        apply.click()
        expect(search).to_have_attribute('aria-invalid','true')
        assert not sent
        for raw, status in [('action=pass&action=block',400),('filter%5B%5D=x',400),('unknown=1',400),('filter=%04',422),
                            ('limit=501',422),('interface=unknown',422),('action=erase',422)]:
            assert page.request.get(endpoint + '?' + raw, headers=auth).status == status
        assert page.request.delete(endpoint, headers=auth).status == 405
        login('api-logs-read','logs:read',path + '?limit=1')
        expect(page.get_by_text('Partial results', exact=False)).to_be_visible()
        expect(page.get_by_label('Limit', exact=True)).to_have_value('1')
        docker('exec', container, 'chmod', '0644', '/var/log/filter.log')
        search.fill('2001:db8')
        apply.click()
        expect(page.get_by_role('alert')).to_contain_text('unavailable')
        expect(search).to_have_value('2001:db8')
        write(two + '\ninvalid record\npartial')
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.get_by_role('cell', name='2001:db8::2')).to_be_visible()
        expect(page.get_by_text('1 malformed log entries skipped.', exact=False)).to_be_visible()
        expect(page.get_by_text('An incomplete final line was skipped.', exact=False)).to_be_visible()
        write('')
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.get_by_text('No firewall events have been recorded in the current log.')).to_be_visible()
        write(records)
        page.get_by_role('button', name='Refresh', exact=True).click()
        page.get_by_text('Advanced filters', exact=True).click()
        source = page.get_by_label('Source address or network', exact=True)
        source.fill('2001:db8::2/64')
        previous_count = len(sent)
        apply.click()
        expect(source).to_have_attribute('aria-invalid', 'true')
        assert len(sent) == previous_count
        source.fill('2001:0db8::/64')
        page.get_by_label('Rule tracker ID', exact=True).fill('23')
        page.get_by_label('Direction', exact=True).select_option('out')
        apply.click()
        expect(page.get_by_role('cell', name='2001:db8::2')).to_be_visible()
        expect(page.get_by_role('cell', name='192.0.2.2:43100')).to_have_count(0)
        assert page.request.get(endpoint + '?source=2001%3Adb8%3A%3A2%2F64', headers=auth).status == 422
        negative = page.request.get(endpoint + '?direction=out&sourcePort=0', headers=auth)
        assert negative.status == 200 and negative.json()['data']['rows'] == []
        assert page.request.get(endpoint + '?sourcePort=43100&destinationPort=33221', headers=auth).json()['data']['shown'] == 1
        if evidence:
            page.screenshot(path=str(evidence/'firewall-filters.png'), full_page=True)
        tcp = one.replace('17,udp,28', '6,tcp,40').replace('33221,0', '33221,0,SA')
        write(records + tcp + '\n')
        flags = page.request.get(endpoint + '?tcpFlags=sA&protocol=tcp', headers=auth)
        assert flags.status == 200 and flags.json()['data']['shown'] == 1
        login('page-diagnostics-logs-firewall', 'logs:read diagnostics:read', path, ('page-diagnostics-dns',))
        expect(page.get_by_role('link', name='192.0.2.2:43100', exact=True).first).to_have_attribute('href', '/diagnostics/dns-lookup?host=192.0.2.2')
        page.get_by_role('link', name='2001:db8::1', exact=True).click()
        expect(page.get_by_role('textbox', name='Hostname or IP address')).to_have_value('2001:db8::1')

        assert page.request.get(endpoint + '?tcpFlags=xyz', headers=auth).status == 422
        login('page-diagnostics-logs-firewall', 'logs:read status:read', '/account/preferences',
              ('page-system-login-logout',))
        preference = page.get_by_role('combobox', name='Advanced log filters')
        expect(preference).to_have_value('collapsed')
        preference.select_option('expanded')
        with page.expect_response(lambda reply: reply.url.endswith('/api/v1/me/preferences') and reply.request.method == 'PUT') as saved:
            page.get_by_role('button', name='Save', exact=True).click()
        assert saved.value.ok
        login('page-diagnostics-logs-firewall', 'logs:read', path, ('page-system-login-logout',))
        expect(page.get_by_label('Source address or network', exact=True)).to_be_visible()
        live = endpoint + '/live'
        login('page-diagnostics-logs-firewall', 'logs:read', path)
        assert page.request.get(live, headers=auth).status == 403
        login('page-diagnostics-logs-firewall-dynamic', 'diagnostics:read', path + '/live')
        assert page.request.get(live + '?bad[]=1', headers=auth).status == 403
        write(one + '\n')
        page.clock.install()
        login('page-diagnostics-logs-firewall-dynamic', 'logs:read', path + '/live?action=pass')
        expect(page.get_by_role('cell', name='192.0.2.2:43100')).to_be_visible()
        assert page.request.get(endpoint, headers=auth).status == 403
        assert page.request.get(live + '?limit=900', headers=auth).status == 422
        forms = page.request.get(origin + '/api/v1/forms', headers=auth).json()['data']['reads']
        assert 'liveFirewallLogs' in forms and 'firewallLogs' not in forms
        pause = page.get_by_role('button', name='Pause live updates')
        resume = page.get_by_role('button', name='Resume live updates')
        pause.click()
        live_queries = []
        page.on('request', lambda req: live_queries.append(req.url) if req.url.startswith(live) else None)
        write(two + '\n' + one.replace('43100', '43101') + '\n')
        page.clock.fast_forward(51000)
        assert not live_queries
        expect(page.get_by_role('cell', name='192.0.2.2:43100')).to_be_visible()
        resume.click()
        expect(page.get_by_role('cell', name='192.0.2.2:43101')).to_be_visible()
        expect(page.get_by_role('cell', name='2001:db8::2')).to_have_count(0)
        write(one.replace('43100','43102') + '\n')
        page.clock.fast_forward(25600)
        expect(page.get_by_role('cell', name='192.0.2.2:43102')).to_be_visible()
        assert live_queries == [live + '?action=pass&presentation=display'] * 2
        if evidence:
            page.screenshot(path=str(evidence/'firewall-live.png'), full_page=True)
        doc = ElementTree.fromstring(docker('exec', container, *cli, 'backup'))
        doc.find('./restapi/apitoken/scopes').text = 'diagnostics:read'
        docker('exec', '-i', container, *cli, 'restore', input=ElementTree.tostring(doc))
        page.clock.fast_forward(25600)
        expect(page.get_by_role('alert')).to_contain_text('cannot access this resource')
        expect(page.get_by_role('cell', name='192.0.2.2:43102')).to_have_count(0)
        expect(resume).to_be_visible()
        count = len(live_queries)
        page.clock.fast_forward(51000)
        assert len(live_queries) == count
        login('api-logs-read', 'logs:read', path)
        search.fill('43102')
        apply.click()
        expect(page).to_have_url(origin + path + '?filter=43102')
        page.get_by_role('link', name='View live firewall log').click()
        expect(page).to_have_url(origin + path + '/live?filter=43102')
        expect(page.get_by_role('cell', name='192.0.2.2:43102')).to_be_visible()
        page.get_by_role('link', name='View firewall log', exact=True).click()
        expect(page).to_have_url(origin + path + '?filter=43102')
        expect(page.get_by_role('button', name='Pause live updates')).to_have_count(0)
        count = len(live_queries)
        page.clock.fast_forward(51000)
        assert len(live_queries) == count
        print('Live firewall log verified separate authorization, pause, resume, bounded filtered refresh, and revocation.')
        print('Firewall log browser passed scoped authorization, real private-file reads, NAT-free numeric endpoints, history, filters, bounded rows, partial lines and retry.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
