"""Exercise Telegraf's output-specific forms through the routed WebUI."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import json
from xml.etree import ElementTree as ET

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_telegraf(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    endpoint = origin + '/api/v1/packages/telegraf'
    auth = {'Authorization': 'Bearer ' + key}
    secret = 'test-only-output-R79'
    replacement = 'test-only-rotated-Q48'
    credentials = auth | {'Origin': origin, 'X-FreeSense-Command': 'package.telegraf.save'}

    def configure(grant, scope):
        doc = ET.fromstring(docker('exec', container, *cli, 'backup'))
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for permission in ('page-system-restapi-keys', grant):
            ET.SubElement(user, 'priv').text = permission
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    def detail():
        response = page.request.get(endpoint, headers=auth)
        assert response.ok, response.status
        data = response.json()['data']
        assert all(value not in json.dumps(data) for value in (secret, replacement)) and 'password' not in data['values']
        return data

    def save():
        with page.expect_response(lambda result: result.url == endpoint + '/settings' and result.request.method == 'PUT') as received:
            page.get_by_role('button', name='Save', exact=True).click()
        assert received.value.ok, received.value.status
        assert all(value not in received.value.text() for value in (secret, replacement))
        expect(page.get_by_text('Telegraf draft saved.', exact=True)).to_be_visible()

    try:
        docker('exec', container, 'cp', '-R', '/optional/net-mgmt/FreeSense-pkg-Telegraf/files/usr/local/.', '/check/prefix/')
        configure('page-status-services', 'packages.telegraf:write')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-services-telegraf', 'packages.telegraf:write')
        before = detail()['values']
        assert not before['enabled'] and before['output'] == 'influxdb'
        page.goto(origin + '/packages')
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        menu_link(page, 'Services', 'Services: Telegraf').click()
        page.get_by_role('checkbox', name='Enable', exact=True).check()
        page.get_by_role('button', name='Save', exact=True).click()
        expect(page.locator('[data-error-for="host"]')).not_to_be_empty()
        page.get_by_role('combobox', name='Telegraf Output').select_option('graphite')
        expect(page.get_by_label('Server port')).to_have_value('2003')
        expect(page.get_by_label('InfluxDB Password')).to_be_hidden()
        page.get_by_label('Server hostname or address').fill('192.0.2.50')
        page.get_by_label('Update Interval').fill('15')
        page.get_by_label('Graphite Prefix').fill('firewall')
        page.get_by_label('Graphite Timeout').fill('3')
        page.get_by_role('checkbox', name='Enable Netstat Monitor').check()
        save()
        expect(page.get_by_role('button', name='Apply', exact=True)).to_be_visible()
        graph = detail()['values']
        assert graph['output'] == 'graphite' and graph['prefix'] == 'firewall' and graph['port'] == 2003
        graph_fields = ('enabled', 'interval', 'shortHostname', 'netstat', 'output', 'host', 'port', 'prefix', 'timeout')
        assert page.request.put(endpoint + '/settings', headers=credentials, data={
            **{name: graph[name] for name in graph_fields}, 'revision': before['revision'],
        }).status == 409

        output = page.get_by_role('combobox', name='Telegraf Output')
        expect(output).to_have_value('graphite')
        output.select_option('influxdb')
        expect(page.get_by_label('Server port')).to_have_value('8086')
        expect(page.get_by_label('InfluxDB Password')).to_be_hidden()
        page.get_by_role('combobox', name='InfluxDB transport').select_option('https')
        page.get_by_label('Server hostname or address').fill('collector.example')
        page.get_by_label('InfluxDB Database').fill('monitoring')
        username = page.get_by_label('InfluxDB Username')
        username.fill('team:metrics')
        page.get_by_role('button', name='Save', exact=True).click()
        expect(page.locator('#content [data-error-for="username"]')).not_to_be_empty()
        username.fill('metrics-test@example.test')
        page.get_by_label('InfluxDB Password').fill(secret)
        save()
        expect(page.get_by_role('button', name='Apply', exact=True)).to_be_visible()
        approved_draft = detail()['values']
        assert approved_draft['output'] == 'influxdb' and approved_draft['hasPassword']
        expect(page.get_by_label('InfluxDB Password')).to_have_value('')
        if evidence is not None:
            page.screenshot(path=str(evidence / 'telegraf-settings.png'), full_page=True)

        page.get_by_label('Server hostname or address').fill('replacement.example')
        with page.expect_response(lambda result: result.url == endpoint + '/settings' and result.request.method == 'PUT') as rejected:
            page.get_by_role('button', name='Save', exact=True).click()
        assert rejected.value.status == 422
        expect(page.locator('#content [data-error-for="password"]')).to_contain_text('Re-enter the password')
        assert detail()['values']['revision'] == approved_draft['revision']
        page.get_by_label('InfluxDB Password').fill(replacement)
        save()
        rotated = detail()['values']
        assert rotated['host'] == 'replacement.example' and rotated['hasPassword']
        expect(page.get_by_label('InfluxDB Password')).to_have_value('')
        page.get_by_role('combobox', name='InfluxDB transport').select_option('http')
        expect(page.get_by_label('InfluxDB Password')).to_be_hidden()
        save()
        assert not detail()['values']['hasPassword']
        page.get_by_role('checkbox', name='Enable', exact=True).uncheck()
        save()
        expect(page.get_by_role('button', name='Apply', exact=True)).to_be_visible()
        configure('page-services-telegraf', 'packages.telegraf:read')
        assert detail()['operations'] == []
        assert page.request.put(endpoint + '/settings', headers=credentials, data={}).status == 403
        print('Chromium verified Telegraf output fields, stale saves, destination-bound rotation, credential clearing and grants.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
