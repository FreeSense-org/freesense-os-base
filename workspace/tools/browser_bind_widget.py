"""Exercise the BIND widget's scope, active counts and saved dashboard layout."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_bind_widget(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    endpoint = origin + '/api/v1/packages/bind'
    auth = {'Authorization': 'Bearer ' + key}

    def account(with_package_grant):
        doc = ET.fromstring(original)
        user = doc.find('./system/user')
        for tag in user.findall('priv'):
            user.remove(tag)
        grants = ['page-system-restapi-keys', 'page-dashboard-all']
        if with_package_grant:
            grants.append('page-services-binddns')
        for grant in grants:
            ET.SubElement(user, 'priv').text = grant
        doc.find('./restapi/apitoken/scopes').text = 'packages.bind:read'
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))
        page.goto(origin + '/dashboard')
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('heading', name='Dashboard', exact=True)).to_be_visible()

    def layout():
        response = page.request.get(origin + '/api/v1/dashboard/layout', headers=auth)
        assert response.ok
        return response.json()['data']

    def sample(route):
        response = route.fetch()
        data = response.json()
        if response.ok:
            data['data']['values']['status'] = {
                'state': 'running', 'zones': [{'name': 'one.example'}, {'name': 'two.example'}],
                'listeners': [{'address': '192.0.2.53'}, {'address': '2001:db8::53'}],
            }
        route.fulfill(response=response, json=data)

    try:
        docker('exec', container, 'cp', '-R', '/optional/dns/FreeSense-pkg-bind/files/usr/local/.', '/check/prefix/')
        account(False)
        assert 'pkg-bind' not in layout()['catalogue']
        assert page.request.get(endpoint, headers=auth).status == 403
        account(True)
        assert layout()['catalogue']['pkg-bind']['every'] == 30
        package = page.request.get(endpoint, headers=auth)
        assert package.ok and package.json()['data']['values']['status']['state'] == 'unavailable'
        page.get_by_role('button', name='Add widget', exact=True).click()
        page.get_by_role('button', name='BIND', exact=True).click()
        widget = page.get_by_role('article', name='BIND', exact=True)
        expect(widget.get_by_text('BIND status unavailable', exact=True)).to_be_visible()
        page.route(endpoint, sample)
        page.get_by_role('button', name='Pause live updates',exact=True).click()
        page.get_by_role('button', name='Resume live updates',exact=True).click()
        expect(widget.get_by_text('Running', exact=True)).to_be_visible()
        expect(widget.get_by_text('Authoritative zones', exact=True)).to_be_visible()
        expect(widget.get_by_text('Listener addresses', exact=True)).to_be_visible()
        assert widget.locator('dd').all_text_contents() == ['2', '2']
        expect(widget.locator('a[href="/services/bind"]')).to_be_visible()
        expect(page.get_by_role('button', name='Done', exact=True)).to_have_count(0)  # adding a widget leaves edit mode off
        if evidence is not None:
            page.screenshot(path=str(evidence / 'bind-dashboard.png'), full_page=True)
        account(False)
        assert 'pkg-bind' not in layout()['catalogue']
        expect(widget).to_have_count(0)
        assert page.request.get(endpoint, headers=auth).status == 403
        print('Chromium verified BIND widget discovery, granted access, active counts, link and grant withdrawal.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
