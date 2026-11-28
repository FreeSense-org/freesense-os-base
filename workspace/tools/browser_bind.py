"""Exercise BIND's authoritative DNS draft forms through the local router."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_bind(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    endpoint = origin + '/api/v1/packages/bind'
    auth = {'Authorization': 'Bearer ' + key}
    doc = ET.fromstring(original)
    user = doc.find('./system/user')
    for item in user.findall('priv'):
        user.remove(item)
    ET.SubElement(user, 'priv').text = 'page-system-restapi-keys'
    grant = ET.SubElement(user, 'priv')
    scopes = doc.find('./restapi/apitoken/scopes')

    def configure(page_grant, scope):
        grant.text = page_grant
        scopes.text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    def snapshot():
        response = page.request.get(endpoint, headers=auth)
        assert response.ok, response.status
        return response.json()['data']['values']

    def command(path, action, fields):
        return page.request.put(endpoint + '/' + path, data=fields, headers={
            **auth, 'Origin': origin, 'X-FreeSense-Command': 'package.bind.' + action,
        })

    def section(title):
        return page.locator('section').filter(has=page.get_by_role('heading', name=title, exact=True)).last

    def save(form, path):
        with page.expect_response(lambda reply: reply.url == endpoint + '/' + path and reply.request.method == 'PUT') as saved:
            form.get_by_role('button', name='Save', exact=True).click()
        assert saved.value.ok, saved.value.status

    try:
        docker('exec', container, 'cp', '-R', '/optional/dns/FreeSense-pkg-bind/files/usr/local/.', '/check/prefix/')
        configure('page-status-services', 'packages.bind:write')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-services-binddns', 'packages.bind:write')
        before = snapshot()
        page.goto(origin + '/packages')
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        menu_link(page, 'Services', 'Services: BIND DNS Server').click()
        settings = section('General Settings')
        settings.get_by_role('checkbox', name='Enable BIND').check()
        settings.get_by_role('combobox', name='IP Version').select_option('ipv4')
        settings.get_by_role('button', name='Add item', exact=True).click()
        settings.get_by_role('combobox', name='Listen on 1').select_option('lan')
        settings.get_by_label('Listen port', exact=True).fill('5353')
        save(settings, 'settings')
        configured = snapshot()
        assert configured['general']['roles'] == ['lan'] and configured['general']['port'] == 5353
        assert command('settings', 'saveGeneral', {**configured['general'], 'revision': before['revision']}).status == 409

        page.get_by_role('link', name='ACLs', exact=True).click()
        page.get_by_role('button', name='Add ACL', exact=True).click()
        acl_form = section('Add ACL')
        acl_form.get_by_label('ACL Name', exact=True).fill('office')
        acl_form.get_by_role('textbox', name='Description', exact=True).fill('Office network')
        acl_form.get_by_role('button', name='Add item', exact=True).click()
        acl_form.get_by_label('IP or network', exact=True).fill('192.0.2.0/24')
        acl_form.get_by_role('group', name='IP or network range 1', exact=True).get_by_label('Description', exact=True).fill('Lab clients')
        save(acl_form, 'acls/save')
        expect(page.get_by_role('cell', name='office', exact=True)).to_be_visible()
        acl = snapshot()['acls'][0]

        page.get_by_role('link', name='Views', exact=True).click()
        page.get_by_role('button', name='Add view', exact=True).click()
        view_form = section('Add view')
        view_form.get_by_label('View Name', exact=True).fill('internal')
        view_form.get_by_label('Description', exact=True).fill('Authoritative office DNS')
        view_form.get_by_role('button', name='Add item', exact=True).click()
        view_form.get_by_role('combobox', name='match-clients 1').select_option(acl['id'])
        save(view_form, 'views/save')
        expect(page.get_by_role('cell', name='internal', exact=True)).to_be_visible()
        view = snapshot()['views'][0]

        page.get_by_role('link', name='Zones', exact=True).click()
        page.get_by_role('button', name='Add zone', exact=True).click()
        zone_form = section('Add zone')
        for field, value in {'Zone Name': 'lab.example', 'Description': 'Lab zone', 'Name Server': 'ns.lab.example',
                             'Mail Admin Zone': 'hostmaster@lab.example', 'Serial': '2026112201'}.items():
            zone_form.get_by_label(field, exact=True).fill(value)
        zone_form.get_by_role('group', name='View', exact=True).get_by_role('button', name='Add item').click()
        zone_form.get_by_role('combobox', name='View 1').select_option(view['id'])
        zone_form.get_by_role('group', name='Domain Records', exact=True).get_by_role('button', name='Add item').click()
        record = zone_form.get_by_role('group', name='Domain Records 1', exact=True)
        record.get_by_label('Record', exact=True).fill('ns')
        record.get_by_role('combobox', name='Type', exact=True).select_option('AAAA')
        record.get_by_label('Alias or IP address', exact=True).fill('192.0.2.53')
        zone_form.get_by_role('button', name='Save', exact=True).click()
        expect(zone_form.locator('[data-error-for="records"]')).not_to_be_empty()
        record.get_by_role('combobox', name='Type', exact=True).select_option('A')
        save(zone_form, 'zones/save')
        expect(page.get_by_role('cell', name='lab.example', exact=True)).to_be_visible()
        after = snapshot()
        zone = after['zones'][0]
        assert zone['records'][0]['owner'] == 'ns'
        assert command('acls/remove', 'removeAcl', {'id': acl['id'], 'revision': after['revision']}).status == 422
        zone_fields = ('id', 'enabled', 'name', 'description', 'views', 'ttl', 'nameserver', 'baseAddress',
                       'mailbox', 'serial', 'refresh', 'retry', 'expire', 'minimum', 'records')
        amended = {field: zone[field] for field in zone_fields if zone.get(field) is not None}
        amended['ttl'] += 60
        assert command('zones/save', 'saveZone', {**amended, 'revision': after['revision']}).ok
        after = snapshot()
        assert after['zones'][0]['serial'] == zone['serial'] + 1

        assert command('acls/save', 'saveAcl', {'name': 'branch', 'description': '', 'revision': after['revision'],
                      'entries': [{'source': '198.51.100.0/24', 'description': 'Branch clients'}]}).ok
        with_branch = snapshot()
        assert command('views/save', 'saveView', {'name': 'branch-view', 'description': '',
                      'revision': with_branch['revision'], 'matchAcls': [with_branch['acls'][1]['id']]}).ok
        page.reload()
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        page.get_by_role('link', name='Views', exact=True).click()
        order = section('View order')
        order.get_by_role('button', name='Move up', exact=True).last.click()
        with page.expect_response(lambda reply: reply.url == endpoint + '/views/order' and reply.request.method == 'PUT') as ordered:
            order.get_by_role('button', name='Save order', exact=True).click()
        assert ordered.value.ok
        after = snapshot()
        assert [row['name'] for row in after['views']] == ['branch-view', 'internal']

        page.get_by_role('link', name='Zones', exact=True).click()
        configure('page-services-binddns', 'packages.bind:read')
        assert command('zones/remove', 'removeZone', {'id': after['zones'][0]['id'], 'revision': after['revision']}).status == 403
        if evidence is not None:
            page.screenshot(path=str(evidence / 'bind-zones.png'), full_page=True)
        print('Chromium verified BIND listener, ACL, view and zone drafts, record validation, stale writes and grants.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
