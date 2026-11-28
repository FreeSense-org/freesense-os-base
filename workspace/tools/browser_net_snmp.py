"""Verify polling-only Net-SNMP management forms through the routed WebUI."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from xml.etree import ElementTree as ET

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_net_snmp(page, origin, credential, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    endpoint = origin + '/api/v1/packages/net-snmp'
    auth = {'Authorization': 'Bearer ' + credential}
    original = docker('exec', container, *cli, 'backup')
    auth_secret = 'test-only-auth-A52'
    privacy_secret = 'test-only-privacy-C97'
    community = 'test-only-community-D44'
    rotated_auth = 'test-only-rotated-auth-E31'
    rotated_privacy = 'test-only-rotated-privacy-F46'

    def configure(page_grant, scope):
        doc = ET.fromstring(docker('exec', container, *cli, 'backup'))
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for grant in ['page-system-restapi-keys', page_grant]:
            ET.SubElement(user, 'priv').text = grant
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    def detail():
        response = page.request.get(endpoint, headers=auth)
        assert response.ok, response.status
        data = response.json()['data']
        serialized = json.dumps(data)
        assert not any(secret in serialized for secret in (auth_secret, privacy_secret, community, rotated_auth, rotated_privacy))
        return data

    def save(path, action, fields):
        return page.request.put(endpoint + '/' + path, headers={
            **auth, 'Origin': origin, 'X-FreeSense-Command': 'package.net-snmp.' + action,
        }, data=fields)

    def section(title):
        return page.locator('section').filter(has=page.get_by_role('heading', name=title, exact=True)).last

    try:
        docker('exec', container, 'cp', '-R', '/optional/net-mgmt/FreeSense-pkg-net-snmp/files/usr/local/.', '/check/prefix/')
        configure('page-services-net-snmp', 'packages.net-snmp:write')
        before = detail()['values']
        page.goto(origin + '/packages')
        fill_api_key(page, credential)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        menu_link(page, 'Services', 'Services: SNMP (NET-SNMP)').click()
        general = section('General Settings')
        general.get_by_role('checkbox', name='Enable snmpd').check()
        general.get_by_role('button', name='Save', exact=True).click()
        expect(general.locator('[data-error-for="bindings"]')).not_to_be_empty()
        available = next(row for row in before['availableBindings'] if row['transport'] == 'udp')
        general.get_by_role('button', name='Add listener').click()
        general.get_by_label('Assigned address').select_option(
            '/'.join(available[field] for field in ('role', 'transport', 'address'))
        )
        general.get_by_label('UDP port').fill('1161')
        general.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_text('Agent draft updated.', exact=True)).to_be_visible()
        general_value = detail()['values']['general']
        assert general_value['bindings'] == [{
            **{field: available[field] for field in ('role', 'transport', 'address')}, 'port': 1161,
        }]
        assert save('host', 'saveHost', {**before['host'], 'revision': before['revision']}).status == 409

        page.get_by_role('link', name='Host Information', exact=True).click()
        host = section('Host Information')
        host_fields = {
            'Location': 'Lab rack', 'Contact': 'Monitoring team',
            'Name': 'lab-firewall', 'Description': 'Synthetic polling fixture',
        }
        for label, value in host_fields.items():
            host.get_by_label(label, exact=True).fill(value)
        with page.expect_response(lambda response: response.url == endpoint + '/host' and response.request.method == 'PUT') as result:
            host.get_by_role('button', name='Save', exact=True).click()
        assert result.value.ok, result.value.status
        expect(host.get_by_label('Location', exact=True)).to_have_value('Lab rack')
        assert detail()['values']['host'] == {label.lower(): value for label, value in host_fields.items()}

        page.get_by_role('link', name='Users', exact=True).click()
        page.get_by_role('button', name='Add user').click()
        user = section('Add user')
        user.get_by_label('User name').fill('test-monitor')
        user.get_by_label('Description').fill('Synthetic monitoring user')
        user.get_by_label('Base OID').fill('.1.3.6.1.2.1.1')
        user.get_by_label('Password', exact=True).fill(auth_secret)
        user.get_by_label('Passphrase', exact=True).fill(privacy_secret)
        user.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_role('cell', name='test-monitor', exact=True)).to_be_visible()
        page.get_by_role('row').filter(has_text='test-monitor').get_by_role('button', name='Edit').click()
        edit = section('Edit user')
        expect(edit.get_by_label('Password', exact=True)).to_have_value('')
        expect(edit.get_by_label('Passphrase', exact=True)).to_have_value('')
        edit.get_by_label('Include Password', exact=True).check()
        edit.get_by_label('Password', exact=True).fill(rotated_auth)
        edit.get_by_role('button', name='Save', exact=True).click()
        expect(edit.locator('[data-error-for="privSecret"]')).not_to_be_empty()
        edit.get_by_label('Include Passphrase', exact=True).check()
        edit.get_by_label('Passphrase', exact=True).fill(rotated_privacy)
        edit.get_by_label('Description', exact=True).fill('Rotated synthetic user')
        edit.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_role('cell', name='Rotated synthetic user', exact=True)).to_be_visible()
        after_user = detail()['values']
        assert [row['name'] for row in after_user['users']] == ['test-monitor']

        page.get_by_role('link', name='Communities', exact=True).click()
        page.get_by_role('button', name='Add community', exact=True).click()
        editor = section('Add community')
        editor.get_by_label('Community name').fill(community)
        editor.get_by_label('Description', exact=True).fill('Synthetic v2c')
        editor.get_by_label('Source', exact=True).fill('192.0.2.0/24')
        editor.get_by_label('Base OID', exact=True).fill('.1.3.6.1.2.1.1')
        editor.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_role('cell', name='Synthetic v2c', exact=True)).to_be_visible()
        assert len(detail()['values']['communities']) == 1
        if evidence is not None:
            page.screenshot(path=str(evidence / 'net-snmp-agent.png'), full_page=True)

        def present_running_agent(route):
            response = route.fetch()
            data = response.json()
            data['data']['values']['status'] = {
                'state': 'running', 'running': True, 'revision': '0' * 64,
                'listeners': [{
                    'role': 'lan', 'transport': 'udp6', 'device': 'em0',
                    'address': '2001:db8::1', 'port': 1161,
                }],
            }
            route.fulfill(response=response, json=data)

        page.route(endpoint, present_running_agent, times=1)
        page.reload()
        fill_api_key(page, credential)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('heading', name='Active polling listeners')).to_be_visible()
        expect(page.get_by_text('[2001:db8::1]:1161', exact=True)).to_be_visible()
        expect(page.get_by_text('Saved settings are awaiting approval.', exact=True)).to_be_visible()

        configure('page-services-net-snmp', 'packages.net-snmp:read')
        readonly = detail()
        assert readonly['operations'] == []
        assert save('users/remove', 'removeUser', {
            'revision': readonly['values']['revision'], 'id': readonly['values']['users'][0]['id'],
        }).status == 403
        configure('page-services-net-snmp', 'packages.net-snmp:write')
        page.get_by_role('link', name='Users', exact=True).click()
        page.get_by_role('row').filter(has_text='test-monitor').get_by_role('button', name='Remove', exact=True).click()
        page.once('dialog', lambda dialog: dialog.dismiss())
        section('Remove user').get_by_role('button', name='Remove', exact=True).click()
        assert len(detail()['values']['users']) == 1
        page.once('dialog', lambda dialog: dialog.accept())
        section('Remove user').get_by_role('button', name='Remove', exact=True).click()
        expect(page.get_by_role('cell', name='test-monitor', exact=True)).to_have_count(0)
        assert not detail()['values']['users']
        configure('page-status-andwatch', 'packages.net-snmp:read')
        assert page.request.get(endpoint, headers=auth).status == 403
        print('Chromium verified Net-SNMP listeners, write-only credential rotation, confirmed removal, stale writes and scoped grants.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
