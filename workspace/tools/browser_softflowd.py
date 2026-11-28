"""Verify the routed softflowd settings, scope checks and ordered export indices."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_softflowd(page, origin, credential, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    base = origin + '/api/v1/packages/softflowd'
    auth = {'Authorization': 'Bearer ' + credential}
    command = auth | {'Origin': origin, 'X-FreeSense-Command': 'package.softflowd.save'}
    writes = []

    def request_seen(request):
        if request.method == 'PUT' and request.url.startswith(base):
            writes.append(request.url)

    def set_auth(grant, scope):
        doc = ET.fromstring(docker('exec', container, *cli, 'backup'))
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for item in ['page-system-restapi-keys', grant]:
            ET.SubElement(user, 'priv').text = item
        doc.find('./restapi/apitoken/scopes').text = scope
        interfaces = doc.find('./interfaces')
        if interfaces.find('opt1') is None:
            interfaces.append(ET.fromstring('<opt1><enable/><if>em4</if><descr>Lab</descr></opt1>'))
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    def login():
        page.goto(origin + '/packages')
        fill_api_key(page, credential)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
        menu_link(page, 'Services', 'Services: softflowd').click()

    docker('exec', container, 'cp', '-R', '/optional/net-mgmt/FreeSense-pkg-softflowd/files/usr/local/.', '/check/prefix/')
    page.on('request', request_seen)
    try:
        set_auth('page-status-services', 'packages.softflowd:write')
        assert page.request.get(base, headers=auth).status == 403
        set_auth('page-services-softflowd', 'packages.softflowd:write')
        login()
        form = page.locator('form').filter(has=page.get_by_role('button', name='Save', exact=True))
        expect(form.get_by_label('Enable softflowd', exact=True)).to_be_checked()
        initial = page.request.get(base, headers=auth).json()['data']['values']
        save = form.get_by_role('button', name='Save', exact=True)
        group = form.get_by_role('group', name='Interface', exact=True)
        for i, role in enumerate(['lan', 'opt1'], 1):
            group.get_by_role('button', name='Add item').click()
            group.get_by_role('combobox', name='Interface ' + str(i), exact=True).select_option(role)
        form.get_by_label('Host', exact=True).fill('collector.example')
        form.get_by_label('Port', exact=True).fill('2055')
        save.click()
        expect(form.locator('[data-error-for="host"]')).not_to_be_empty()
        assert not writes
        form.get_by_label('Host', exact=True).fill('2001:db8::50')
        form.get_by_role('combobox', name='NetFlow version', exact=True).select_option('5')
        expect(form.get_by_label('Bidirectional Flow', exact=True)).to_be_hidden()
        form.get_by_role('combobox', name='NetFlow version', exact=True).select_option('10')
        form.get_by_label('Bidirectional Flow', exact=True).check()
        form.get_by_label('Max Flows', exact=True).fill('4096')
        form.get_by_label('Expire Interval', exact=True).fill('0')
        group.get_by_role('button', name='Move up').last.click()
        save.click()
        expect(page.get_by_text('softflowd settings saved.', exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name='Configured interface indices')).to_be_visible()
        current = page.request.get(base, headers=auth).json()['data']['values']
        assert current['status']['state'] == 'unavailable' and current['status']['workers'] == []
        assert [(row['index'], row['role']) for row in current['indices']] == [(1, 'opt1'), (2, 'lan')]
        assert current['host'] == '2001:db8::50' and current['maxflows'] == 4096 and current['timeout_expint'] == 0
        assert writes == [base + '/settings']
        fields = {k: current[k] for k in ['revision', 'enabled', 'roles', 'host', 'port', 'sample', 'maxflows', 'version',
                                           'biflows', 'flowtracking', 'flowtimestamp', 'timeout_expint']}
        for delta in [{'roles': ['opt99']}, {'roles':['lan','lan']}, {'version':'9'}, {'port': 65536}, {'argv': ['-d']}]:
            assert page.request.put(base + '/settings', headers=command, data=fields | delta).status == 422
        assert page.request.put(base + '/settings', headers=command, data=fields | {'revision': initial['revision']}).status == 409
        if evidence:
            page.screenshot(path=evidence/'softflowd-settings.png', full_page=True)
        set_auth('page-services-softflowd', 'packages.softflowd:read')
        login()
        expect(page.get_by_role('button', name='Save', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='Apply', exact=True)).to_have_count(0)
        expect(page.get_by_text('2001:db8::50', exact=True)).to_be_visible()
        assert page.request.put(base + '/settings', headers=command, data=fields).status == 403
        apply_command = auth | {'Origin': origin, 'X-FreeSense-Command': 'package.softflowd.apply'}
        assert page.request.put(base + '/apply', headers=apply_command, data={'revision': current['revision']}).status == 403
        print('Chromium verified scoped softflowd settings, ordered indices, central validation and stale writes.')
    finally:
        page.remove_listener('request', request_seen)
        docker('exec', '-i', container, *cli, 'restore', input=original)
