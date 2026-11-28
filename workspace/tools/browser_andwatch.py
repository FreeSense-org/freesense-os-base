"""Exercise the ANDwatch package routes through the shared management shell."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree as ET

from browser_shell import menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_andwatch(page, origin, credential, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    endpoint = origin + '/api/v1/packages/andwatch'
    auth = {'Authorization': 'Bearer ' + credential}
    cmd = auth | {'Origin': origin, 'X-FreeSense-Command': 'package.andwatch.save'}
    writes = []

    def observe(request):
        if request.method == 'PUT' and request.url == endpoint + '/settings':
            writes.append(request.post_data_json)

    def configure(grant, scope):
        doc = ET.fromstring(docker('exec', container, *cli, 'backup'))
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for name in ['page-system-restapi-keys', grant]:
            ET.SubElement(user, 'priv').text = name
        doc.find('./restapi/apitoken/scopes').text = scope
        links = doc.find('./interfaces')
        if links.find('opt1') is None:
            links.append(ET.fromstring('<opt1><enable/><if>em4</if><descr>Office</descr></opt1>'))
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    def login():
        page.goto(origin + '/packages')
        fill_api_key(page, credential)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()

    def status_view(interfaces):
        status = origin + '/api/v1/packages/andwatch-status'
        configure('page-status-andwatch', 'packages.andwatch.status:read')
        assert page.request.get(endpoint, headers=auth).status == 403
        lan_device = next(i['device'] for i in interfaces if i['role'] == 'lan')
        opt_device = next(i['device'] for i in interfaces if i['role'] == 'opt1')
        assert lan_device.isalnum() and opt_device.isalnum()
        script = ('#!/bin/sh\n'
                  'test "$1" = -L && test "$2" = /var/db/freesense-andwatch || exit 1\nshift 2\n'
                  'test ! -f /tmp/andwatch-db-unavailable || exit 1\n'
                  'history=no\nif [ "$1" = -a ]; then history=yes; shift; fi\n'
                  'test "$#" = 1 || exit 1\ncase "$1" in\n'
                  + lan_device + ''') echo '2026-11-20 10:00:00 00:01:00 printer 192.0.2.11 02:00:00:00:00:11 Office Devices'
                                     echo '2026-11-20 10:05:00 00:02:00 router 2001:db8::22 02:00:00:00:00:22 Network Lab'
                                     if [ "$history" = yes ]; then echo '2026-11-18 10:00:00 2d old-printer 192.0.2.99 02:00:00:00:00:99 Retired Devices'; fi ;;
'''               + opt_device + ''') echo '2026-11-20 10:20:00 00:06:00 test-node 192.0.2.70 02:00:00:00:00:70 Office Fixture' ;;
*) exit 1 ;;
esac
''')
        docker('exec','-i',container,'sh','-ec','cat > /usr/local/bin/andwatch-query; chmod 0555 /usr/local/bin/andwatch-query',input=script.encode())
        # The real private receipt below models the approval normally produced by the FreeBSD service.
        # Linux provides only a deterministic query executable; this check performs no capture.
        docker('exec',container,'php','-r',r'''
require '/check/prefix/FreeSense/autoload.php';
$store = new FreeSense\Config\ConfigurationStore('/check/config');
$doc = $store->document();
$approval = new FreeSense\Packages\Andwatch\MonitorApproval(FreeSense\Packages\Andwatch\Settings::snapshot($doc)['revision'],
    FreeSense\Packages\Andwatch\Settings::policy($doc));
FreeSense\Config\PrivateFile::replace($store->directory.'/andwatch-approved.json',$approval->bytes());''')
        details = page.request.get(status, headers=auth)
        assert details.ok, details.text()
        assert [row['role'] for row in details.json()['data']['values']['availableInterfaces']] == ['lan','opt1']
        login()
        menu_link(page,'Status','Status: ANDwatch Database').click()
        expect(page.get_by_role('heading',name='Current Address Records')).to_be_visible()
        expect(page.get_by_role('cell',name='printer',exact=True)).to_be_visible()
        expect(page.get_by_role('cell',name='old-printer',exact=True)).to_have_count(0)
        page.get_by_role('button',name='Show Historical Records',exact=True).click()
        expect(page.get_by_role('cell',name='old-printer',exact=True)).to_be_visible()
        term = page.get_by_label('Search Term',exact=True)
        term.fill('^old-printer$')
        page.get_by_role('combobox',name='Search in').select_option('hostname')
        page.get_by_role('combobox',name='Match').select_option('regex')
        term.press('Enter')
        expect(page.get_by_role('cell',name='printer',exact=True)).to_have_count(0)
        expect(page.get_by_role('cell',name='old-printer',exact=True)).to_be_visible()
        term.fill('['); page.get_by_role('button',name='Search',exact=True).click()
        expect(page.locator('[data-error-for="search"]')).not_to_be_empty()
        page.get_by_role('button',name='Clear',exact=True).click()
        expect(page.get_by_role('cell',name='router',exact=True)).to_be_visible()
        page.get_by_role('button',name='IP Address',exact=True).click()
        expect(page.get_by_role('columnheader',name='IP Address')).to_have_attribute('aria-sort','ascending')
        if evidence:
            page.screenshot(path=evidence/'andwatch-database.png',full_page=True)
        page.get_by_role('button',name='Office',exact=True).click()
        expect(page.get_by_role('cell',name='test-node',exact=True)).to_be_visible()
        expect(page.get_by_role('cell',name='printer',exact=True)).to_have_count(0)
        page.get_by_role('button',name='Show Current Records Only',exact=True).click()
        expect(page.get_by_role('heading',name='Current Address Records')).to_be_visible()
        assert page.request.get(status+'?field=bad',headers=auth).status == 400
        assert page.request.get(status+'?role=wan',headers=auth).status in (403,422)
        assert page.request.get(status+'?search=one&search=two',headers=auth).status in (400,422)
        assert page.request.put(endpoint+'/settings',headers=cmd,data={'revision':'a'*64}).status == 403
        docker('exec',container,'touch','/tmp/andwatch-db-unavailable')
        assert page.request.get(status,headers=auth).status == 503
        page.get_by_role('button',name='Search',exact=True).click()
        search_form = page.locator('form').filter(has=page.get_by_role('button',name='Search',exact=True))
        expect(search_form.locator('[data-form-error]')).to_contain_text('unavailable')
        configure('page-services-andwatch','packages.andwatch:read')
        assert page.request.get(status,headers=auth).status == 403

    page.on('request', observe)
    try:
        docker('exec', container, 'cp', '-R', '/optional/net-mgmt/FreeSense-pkg-ANDwatch/files/usr/local/.', '/check/prefix/')
        configure('page-status-andwatch', 'packages.andwatch:write')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-services-andwatch', 'packages.andwatch:write')
        login()
        menu_link(page, 'Services', 'Services: ANDwatch').click()
        first = page.request.get(endpoint, headers=auth).json()['data']['values']
        assert 'wan' not in [item['role'] for item in first['availableInterfaces']]
        form = page.locator('form').filter(has=page.get_by_role('button', name='Save', exact=True))
        selected = form.get_by_role('listbox', name='Interfaces', exact=True)
        office = form.get_by_role('group', name='Office Settings', exact=True)
        form.get_by_label('Enable the ANDwatch daemon(s)', exact=True).check()
        submit = form.get_by_role('button', name='Save', exact=True)
        submit.click()
        expect(form.locator('[data-error-for="interfaces"]')).not_to_be_empty()
        selected.select_option(['lan','opt1'])
        expect(office.get_by_role('combobox', name='Record Expiration')).to_have_value('30')
        expect(office.get_by_role('combobox', name='PCAP Filter')).to_have_value('link-local-unique')
        office.get_by_role('combobox', name='PCAP Filter').select_option('custom')
        custom = office.get_by_label('Custom Filter', exact=True)
        expect(custom).to_be_visible()
        submit.click()
        expect(form.locator('[data-error-for="interfaces"]')).not_to_be_empty()
        assert not writes
        custom.fill('not host 192.0.2.40')
        office.get_by_role('combobox', name='Record Expiration').select_option('60')
        selected.select_option(['lan'])
        expect(office).to_be_hidden()
        selected.select_option(['lan','opt1'])
        expect(custom).to_have_value('not host 192.0.2.40')
        submit.click()
        expect(page.get_by_text('ANDwatch settings saved.', exact=True)).to_be_visible()
        assert len(writes) == 1
        payload = writes[0]
        assert all('device' not in row and 'current' not in row for row in payload['interfaces'])
        current = page.request.get(endpoint, headers=auth).json()['data']['values']
        assert current['enabled'] and current['interfaces'][1]['customFilter'] == 'not host 192.0.2.40'
        stale = page.request.put(endpoint + '/settings', headers=cmd, data=payload)
        assert stale.status == 409
        updated = payload | {'revision': current['revision']}
        for delta in [{'customFilter':'x'*101}, {'retentionDays': 30}, {'role':'wan'}, {'device':'em0'}]:
            bad = updated | {'interfaces': [updated['interfaces'][1] | delta]}
            assert page.request.put(endpoint + '/settings', headers=cmd, data=bad).status == 422
        selected.select_option(['lan'])
        with page.expect_response(lambda res: res.url == endpoint + '/settings' and res.status == 200):
            submit.click()
        expect(form.get_by_role('button', name='Save')).to_be_enabled()
        hidden = page.request.get(endpoint, headers=auth).json()['data']['values']
        assert hidden['inactiveInterfaces'][0]['customFilter'] == 'not host 192.0.2.40'
        login()
        menu_link(page, 'Services', 'Services: ANDwatch').click()
        selected.select_option(['lan','opt1'])
        expect(custom).to_have_value('not host 192.0.2.40')
        with page.expect_response(lambda res: res.url == endpoint + '/settings' and res.status == 200):
            submit.click()
        expect(page.get_by_text('ANDwatch settings saved.', exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence/'andwatch-settings.png', full_page=True)
        configure('page-services-andwatch', 'packages.andwatch:read')
        login()
        menu_link(page, 'Services', 'Services: ANDwatch').click()
        expect(page.get_by_role('button', name='Save', exact=True)).to_have_count(0)
        expect(page.get_by_text('not host 192.0.2.40', exact=True)).to_be_visible()
        assert page.request.put(endpoint + '/settings', headers=cmd, data=updated).status == 403
        status_view(first['availableInterfaces'])
        print('Chromium verified ANDwatch settings, separate grants, current/historical records, field and regex filters, interface tabs and sorting.')
    finally:
        page.remove_listener('request', observe)
        docker('exec', '-i', container, *cli, 'restore', input=original)
