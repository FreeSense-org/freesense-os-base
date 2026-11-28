from bz2 import compress
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_filter_log_summary(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    route = '/status/logs/firewall/summary'
    endpoint = origin + '/api/v1/logs/firewall/summary'
    auth = {'Authorization':'Bearer ' + key}
    event = ('Nov 21 08:10:05 firewall filterlog[123]: '
             '3,,,12,em0,match,pass,in,4,0x00,,64,123,0,none,17,udp,28,192.0.2.2,192.0.2.1,43100,53,0')
    records = [event, event] + [
        event.replace('match,pass', 'match,block').replace('192.0.2.2,', f'192.0.2.{i},')
        for i in range(3, 8)
    ]

    def configure(*grants):
        config = ET.fromstring(original)
        user = config.find('./system/user')
        for node in user.findall('priv'):
            user.remove(node)
        for grant in ['page-system-restapi-keys', *grants]:
            ET.SubElement(user, 'priv').text = grant
        config.find('./restapi/apitoken/scopes').text = 'logs:read diagnostics:read'
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(config))

    def log(body):
        docker('exec', '-i', container, 'php', '-r',
               'file_put_contents("/var/log/filter.log",stream_get_contents(STDIN)); chmod("/var/log/filter.log",0600);',
               input=body.encode())

    try:
        log('\n'.join(records) + '\n')
        configure('page-diagnostics-logs-firewall')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-diagnostics-logs-firewall-summary', 'page-diagnostics-dns')
        assert page.request.get(origin + '/api/v1/logs/firewall', headers=auth).status == 403
        assert page.request.get(endpoint + '?limit=1', headers=auth).status == 400
        received = page.request.get(endpoint, headers=auth)
        assert received.ok, received.text()
        summary = received.json()['data']
        assert 'rows' not in summary and summary['totals'] == {'entries':7, 'block':5, 'pass':2, 'sources':6}
        assert summary['facets']['source']['other'] == 1
        page.goto(origin + route)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('heading', name='Firewall log summary')).to_be_visible()
        for title in ('Actions', 'Interfaces', 'Protocols', 'Source IPs', 'Destination IPs', 'Source Ports', 'Destination Ports'):
            expect(page.get_by_role('heading', name=title, exact=True)).to_be_visible()
        expect(page.get_by_role('cell', name='Other', exact=True)).to_be_visible()
        source = page.get_by_role('link', name='192.0.2.2', exact=True)
        expect(source).to_have_attribute('href', '/diagnostics/dns-lookup?host=192.0.2.2')
        if evidence:
            page.screenshot(path=evidence/'firewall-log-summary.png', full_page=True)
        log('')
        page.get_by_role('button', name='Refresh summary', exact=True).click()
        expect(page.get_by_text('The firewall log has no entries to summarize yet.')).to_be_visible()
        older = event.replace('192.0.2.2,', '192.0.2.90,')
        archive = '/var/log/filter.log.0.bz2'
        docker('exec', '-i', container, 'php', '-r',
               'file_put_contents($argv[1],stream_get_contents(STDIN)); chmod($argv[1],0600);',
               archive, input=compress((older + '\n').encode()))
        log(event + '\n')
        page.get_by_role('button', name='Refresh summary', exact=True).click()
        expect(page.get_by_role('link', name='192.0.2.90', exact=True)).to_be_visible()
        expect(page.get_by_role('link', name='192.0.2.2', exact=True)).to_be_visible()
        combined = page.request.get(endpoint, headers=auth).json()['data']
        assert combined['totals'] == {'entries':2, 'block':0, 'pass':2, 'sources':2}
        assert combined['coverage']['scope'] == 'current-and-rotated'
        assert combined['coverage']['archives'] == 1 and 'rows' not in combined
        expect(page.get_by_text('1 rotated log inspected.')).to_be_visible()
        if evidence:
            page.screenshot(path=evidence/'firewall-log-summary-archives.png', full_page=True)
        log('')
        page.get_by_role('button', name='Refresh summary', exact=True).click()
        expect(page.get_by_role('link', name='192.0.2.90', exact=True)).to_be_visible()
        expect(page.get_by_role('link', name='192.0.2.2', exact=True)).to_have_count(0)
        rotated_only = page.request.get(endpoint, headers=auth).json()['data']
        assert rotated_only['totals'] == {'entries':1, 'block':0, 'pass':1, 'sources':1}
        log(event + '\n')
        docker('exec', '-i', container, 'php', '-r',
               'file_put_contents($argv[1],stream_get_contents(STDIN));',
               archive, input=b'not a valid archive')
        page.get_by_role('button', name='Refresh summary', exact=True).click()
        expect(page.get_by_role('alert')).to_contain_text('private firewall log is unavailable')
        expect(page.get_by_role('link', name='192.0.2.90', exact=True)).to_have_count(0)
        docker('exec', container, 'php', '-r', 'unlink($argv[1]);', archive)
        page.get_by_role('button', name='Refresh summary', exact=True).click()
        expect(page.get_by_role('link', name='192.0.2.2', exact=True)).to_be_visible()
        plain = '/var/log/filter.log.0'
        docker('exec', '-i', container, 'php', '-r',
               'file_put_contents($argv[1],stream_get_contents(STDIN)); chmod($argv[1],0600);',
               plain, input=(older + '\n').encode())
        page.get_by_role('button', name='Refresh summary', exact=True).click()
        expect(page.get_by_role('link', name='192.0.2.90', exact=True)).to_be_visible()
        plain_only = page.request.get(endpoint, headers=auth).json()['data']
        assert plain_only['totals']['entries'] == 2 and plain_only['coverage']['archives'] == 1
        docker('exec', '-i', container, 'php', '-r',
               'file_put_contents($argv[1],stream_get_contents(STDIN)); chmod($argv[1],0600);',
               archive, input=compress((older + '\n').encode()))
        page.get_by_role('button', name='Refresh summary', exact=True).click()
        expect(page.get_by_role('alert')).to_contain_text('private firewall log is unavailable')
        expect(page.get_by_role('link', name='192.0.2.90', exact=True)).to_have_count(0)
        print('Chromium verified independent summary grants, seven ranked facets, Other, the DNS link and empty refresh.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
