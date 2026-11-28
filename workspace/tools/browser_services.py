"""Exercise the separately authorized Status > Services page in Chromium."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_services(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    endpoint = origin + '/api/v1/status/services'
    auth = {'Authorization': 'Bearer ' + key}
    doc = ET.fromstring(original)
    user = doc.find('./system/user')
    for node in user.findall('priv'):
        user.remove(node)
    for grant in ('page-system-restapi-keys', 'page-status-services'):
        ET.SubElement(user, 'priv').text = grant
    scopes = doc.find('./restapi/apitoken/scopes')
    try:
        for origin_name in ('FreeSense-pkg-ANDwatch', 'FreeSense-pkg-softflowd', 'FreeSense-pkg-net-snmp'):
            docker('exec', container, 'cp', '-R', f'/optional/net-mgmt/{origin_name}/files/usr/local/.', '/check/prefix/')
        scopes.text = 'interfaces:read'
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))
        assert page.request.get(endpoint, headers=auth).status == 403
        scopes.text = 'status.services:read'
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))
        api = page.request.get(endpoint, headers=auth)
        assert api.ok
        rows = api.json()['data']
        assert {row['id'] for row in rows} == {'dhcp4', 'kea6', 'dhcrelay', 'resolver', 'filterlog', 'package-andwatch', 'package-softflowd', 'package-net-snmp'}, sorted(row['id'] for row in rows)
        assert all(set(row) == {'id', 'name', 'description', 'state'} for row in rows)
        page.goto(origin + '/packages')
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        menu_link(page, 'Status', 'Status: Services').click()
        expect(page.get_by_role('heading', name='Services status', exact=True)).to_be_visible()
        expect(page.get_by_role('cell', name='DNS Resolver', exact=True)).to_be_visible()
        expect(page.get_by_role('cell', name='SNMP (NET-SNMP)', exact=True)).to_be_visible()
        search = page.get_by_label('Search services')
        search.fill('kea')
        expect(page.get_by_role('cell', name='DHCPv4', exact=True)).to_be_visible()
        expect(page.get_by_role('cell', name='DNS Resolver', exact=True)).to_have_count(0)
        search.fill('')
        state = page.get_by_role('combobox', name='Status')
        state.select_option('unavailable')
        expect(page.get_by_role('cell', name='Firewall Logs', exact=True)).to_be_visible()
        state.select_option('running')
        expect(page.get_by_text('No matching services.')).to_be_visible()
        state.select_option('')
        with page.expect_response(lambda response: response.url == endpoint) as refreshed:
            page.get_by_role('button', name='Refresh services').click()
        assert refreshed.value.ok
        expect(page.get_by_role('heading', name='Firewall log collector controls')).to_have_count(0)
        if evidence is not None:
            page.screenshot(path=str(evidence / 'services.png'), full_page=True)
        scopes.text = 'status.services:write'
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))
        page.reload()
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('heading', name='Firewall log collector controls')).to_be_visible()
        page.get_by_role('combobox', name='Action').select_option('restart')
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_response(lambda response: response.url == endpoint + '/action') as action:
            page.get_by_role('button', name='Perform action').click()
        assert action.value.status == 503
        expect(page.get_by_text('The firewall log collector could not complete the service action.')).to_be_visible()
        print('Chromium verified the Services menu, independent scopes, search, filtering, refresh and confirmed controls.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
