"""Exercise passive Zabbix Agent settings through FreeSense's management shell."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import re
from xml.etree import ElementTree as ET

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_zabbix_agent(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    endpoint = origin + '/api/v1/packages/zabbix-agent'
    auth = {'Authorization': 'Bearer ' + key}
    command = auth | {'Origin': origin, 'X-FreeSense-Command': 'package.zabbix-agent.save'}

    def configure(grant, scope):
        doc = ET.fromstring(docker('exec', container, *cli, 'backup'))
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for permission in ('page-system-restapi-keys', grant):
            ET.SubElement(user, 'priv').text = permission
        lan = doc.find('./interfaces/lan')
        for name, value in {'ipaddrv6': '2001:db8:1::1', 'subnetv6': '64'}.items():
            item = lan.find(name)
            if item is None:
                item = ET.SubElement(lan, name)
            item.text = value
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    def detail():
        response = page.request.get(endpoint, headers=auth)
        assert response.ok, response.status
        return response.json()['data']

    try:
        docker('exec', container, 'cp', '-R', '/optional/net-mgmt/FreeSense-pkg-zabbix-agent/files/usr/local/.', '/check/prefix/')
        configure('page-status-services', 'packages.zabbix-agent:write')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-services-zabbix-agent', 'packages.zabbix-agent:write')
        before = detail()['values']
        assert not before['enabled'] and before['listenPort'] == 10050
        page.goto(origin + '/packages')
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        menu_link(page, 'Services', 'Services: Zabbix Agent 7.0 LTS').click()
        page.get_by_role('checkbox', name='Enable', exact=True).check()
        page.get_by_role('button', name='Save', exact=True).click()
        expect(page.locator('[data-error-for="servers"]')).not_to_be_empty()
        page.get_by_role('group', name='Server', exact=True).get_by_role('button', name='Add item').click()
        page.get_by_role('textbox', name='Server 1').fill('192.0.2.20')
        page.get_by_label('Hostname', exact=True).fill('Lab-Monitor')
        page.get_by_role('checkbox', name=re.compile(r'LAN.*192\.0\.2\.1')).check()
        page.get_by_role('checkbox', name=re.compile(r'LAN.*2001:db8:1::1')).check()
        page.get_by_label('Listen Port', exact=True).fill('11050')
        with page.expect_response(lambda reply: reply.url == endpoint + '/settings' and reply.request.method == 'PUT') as saved:
            page.get_by_role('button', name='Save', exact=True).click()
        assert saved.value.ok, saved.value.status
        expect(page.get_by_text('Zabbix Agent draft saved.', exact=True)).to_be_visible()
        current = detail()['values']
        assert current['hostname'] == 'Lab-Monitor' and current['servers'] == ['192.0.2.20']
        assert {row['address'] for row in current['bindings']} == {'192.0.2.1', '2001:db8:1::1'}
        fields = ('enabled', 'servers', 'hostname', 'bindings', 'listenPort', 'timeout', 'startAgents')
        payload = {name: current[name] for name in fields}
        assert page.request.put(endpoint + '/settings', headers=command, data={
            **payload, 'revision': before['revision'],
        }).status == 409
        assert page.request.put(endpoint + '/settings', headers=command, data={
            **payload, 'revision': current['revision'], 'bindings': [{'role': 'lan', 'address': '192.0.2.99'}],
        }).status == 422
        assert detail()['values']['revision'] == current['revision']
        if evidence is not None:
            page.screenshot(path=str(evidence / 'zabbix-agent-settings.png'), full_page=True)
        configure('page-services-zabbix-agent', 'packages.zabbix-agent:read')
        assert detail()['operations'] == []
        assert page.request.put(endpoint + '/settings', headers=command, data={}).status == 403
        print('Chromium verified Zabbix Agent assigned IPv4 and IPv6 listeners, passive sources, stale writes and grants.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
