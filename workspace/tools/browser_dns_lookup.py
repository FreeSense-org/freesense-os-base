from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_dns_lookup(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    route = '/diagnostics/dns-lookup'
    endpoint = origin + '/api/v1' + route
    headers = {'Authorization': 'Bearer ' + key, 'Origin': origin, 'X-FreeSense-Command': 'lookupDns'}
    drill = b'''#!/bin/sh
case "$2" in
A) answer=192.0.2.8 ;;
AAAA) answer=2001:db8::8 ;;
CNAME) answer=target.example.test. ;;
PTR) answer=ptr.example.test. ;;
*) exit 1 ;;
esac
printf ';; ->>HEADER<<- opcode: QUERY, rcode: NOERROR, id: 1\n'
printf ';; QUESTION SECTION:\n;; %s IN %s\n' "$1" "$2"
printf ';; ANSWER SECTION:\n%s 60 IN %s %s\n' "$1" "$2" "$answer"
printf ';; Query time: 2 msec\n'
'''

    def configure(allowed):
        doc = ET.fromstring(original)
        system = doc.find('./system')
        user = system.find('user')
        for privilege in user.findall('priv'):
            user.remove(privilege)
        grants = ['page-system-restapi-keys', 'page-diagnostics-ping']
        if allowed:
            grants.extend(['page-diagnostics-dns', 'page-firewall-alias-edit', 'page-firewall-aliases'])
        for privilege in grants:
            ET.SubElement(user, 'priv').text = privilege
        for server in system.findall('dnsserver'):
            system.remove(server)
        ET.SubElement(system, 'dnsserver').text = '192.0.2.53'
        doc.find('./restapi/apitoken/scopes').text = 'diagnostics:read diagnostics.ping:write firewall.aliases:write'
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    try:
        docker('exec', '-i', container, 'sh', '-c', 'cat > /usr/bin/drill && chmod 0555 /usr/bin/drill', input=drill)
        configure(False)
        assert page.request.put(endpoint, headers=headers, data={'hostname':'example.test'}).status == 403
        configure(True)
        invalid = page.request.put(endpoint, headers=headers, data={'hostname':'::1', 'type':'A'})
        assert invalid.status == 422 and 'type' in invalid.json()['error']['fields']
        reverse = page.request.put(endpoint, headers=headers, data={'hostname':'[2001:db8::8]', 'type':'PTR'})
        assert reverse.ok and reverse.json()['data']['answers'] == [{'type':'PTR', 'data':'ptr.example.test'}]
        page.goto(origin + route)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        host = page.get_by_role('textbox', name='Hostname or IP address')
        submit = page.get_by_role('button', name='Lookup', exact=True)
        host.fill('example.test:53')
        submit.click()
        expect(host).to_have_attribute('aria-invalid', 'true')
        host.fill('example.test')
        submit.click()
        for answer in ('192.0.2.8', '2001:db8::8', 'target.example.test'):
            expect(page.get_by_role('cell', name=answer, exact=True)).to_be_visible()
        server = page.get_by_role('row').filter(has=page.get_by_role('cell', name='192.0.2.53', exact=True))
        expect(server.get_by_role('cell', name='2 msec', exact=True)).to_be_visible()
        result = page.locator('main section[aria-live=polite]').filter(has=page.get_by_role('heading', name='Timings'))
        expect(result.get_by_role('link', name='Ping')).to_have_attribute('href', '/diagnostics/ping?host=example.test')
        expect(result.get_by_role('link', name='Traceroute')).to_have_count(0)
        if evidence:
            page.screenshot(path=evidence/'diagnostics-dns-lookup.png', full_page=True)
        before_alias = docker('exec', container, *cli, 'backup')
        page.get_by_role('button', name='Add or update alias', exact=True).click()
        expect(page.get_by_role('textbox', name='Alias name')).to_have_value('example_test')
        assert '192.0.2.8' in page.locator('main input').evaluate_all('(inputs) => inputs.map(input => input.value)')
        assert docker('exec', container, *cli, 'backup') == before_alias
        alias_endpoint = origin + '/api/v1/firewall/aliases'
        with page.expect_response(lambda response: response.url == alias_endpoint and response.request.method == 'PUT') as saved:
            page.get_by_role('button', name='Save alias', exact=True).click()
        assert saved.value.ok, saved.value.text()
        applied = docker('exec', container, *cli, 'backup')
        assert applied != before_alias and b'example_test' in applied and b'2001:db8::8' in applied
        docker('exec', '-i', container, 'sh', '-c', 'cat > /usr/bin/drill', input=drill.replace(b'192.0.2.8', b'192.0.2.9'))
        page.goto(origin + route + '?host=example.test')
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(host).to_have_value('example.test')
        submit.click()
        expect(page.get_by_role('cell', name='192.0.2.9', exact=True)).to_be_visible()
        page.get_by_role('button', name='Add or update alias', exact=True).click()
        expect(page.get_by_role('heading', name='Edit address alias')).to_be_visible()
        expect(page.get_by_role('textbox', name='Alias name')).to_have_value('example_test')
        save = page.get_by_role('button', name='Save alias', exact=True)
        dialogs = []
        def dismiss_replacement(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()
        page.once('dialog', dismiss_replacement)
        save.click()
        assert dialogs == ['Replace the saved members of example_test?']
        assert docker('exec', container, *cli, 'backup') == applied
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_response(lambda response: response.url == alias_endpoint and response.request.method == 'PUT') as replaced:
            save.click()
        assert replaced.value.ok, replaced.value.text()
        final = docker('exec', container, *cli, 'backup')
        assert b'192.0.2.9' in final and b'192.0.2.8' not in final
        print('Chromium verified DNS lookup grants, validation, records, timings and permitted handoff.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
