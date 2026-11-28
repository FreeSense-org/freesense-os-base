from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_lldp(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    endpoint = origin + '/api/v1/packages/lldp'
    auth = {'Authorization':'Bearer ' + key}

    def configure(grant, scope):
        doc = ET.fromstring(original)
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for privilege in ['page-system-restapi-keys', grant]:
            ET.SubElement(user, 'priv').text = privilege
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    try:
        docker('exec', container, 'cp', '-R', '/optional/net-mgmt/FreeSense-pkg-lldpd/files/usr/local/.', '/check/prefix/')
        docker('exec', container, 'mkdir', '-p', '/usr/local/sbin')
        docker('exec', '-i', container, 'sh', '-c', 'cat > /usr/local/sbin/lldpcli && chmod 0555 /usr/local/sbin/lldpcli',
               input=b'#!/bin/sh\ntouch /tmp/queried-unowned-lldp\nexit 1\n')
        configure('page-status-services', 'packages.lldp:read')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-services-lldpd', 'status:read')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-services-lldpd', 'packages.lldp:read')
        result = page.request.get(endpoint, headers=auth)
        assert result.ok, result.text()
        data = result.json()['data']
        assert data['values']['state'] in ('disabled', 'stopped', 'unavailable')
        assert data['values']['chassis'] == data['values']['neighbors'] == ''
        absent = docker('exec', container, 'sh', '-c', 'test ! -e /tmp/queried-unowned-lldp && printf yes')
        assert absent == b'yes'
        rendered = data | {'values':{'state':'running', 'chassis':'Local chassis: FreeSense appliance',
                                     'neighbors':'Neighbor: <b>Switch A</b>\nPort: em0'}}
        page.route(endpoint, lambda request: request.fulfill(json={'data':rendered}))
        page.goto(origin + '/services/lldp')
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('heading', name='LLDP Status')).to_be_visible()
        expect(page.get_by_text('Neighbor: <b>Switch A</b>', exact=False)).to_be_visible()
        expect(page.locator('#content pre b')).to_have_count(0)
        if evidence:
            page.screenshot(path=evidence/'lldp-status.png', full_page=True)
        page.unroute(endpoint)
        settings = origin + '/api/v1/packages/lldp-settings'
        saved_before = page.request.get(settings, headers=auth)
        assert saved_before.ok, saved_before.text()
        draft = saved_before.json()['data']['values']
        assert draft['enable'] is False and draft['lldp_proto'] == 'active'
        page.get_by_role('link', name='LLDP Settings', exact=True).click()
        expect(page.get_by_role('heading', name='LLDP Settings', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Save LLDP settings')).to_have_count(0)
        configure('page-services-lldpd', 'packages.lldp:write')
        page.reload()
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        page.get_by_label('Receive Only Mode', exact=True).check()
        page.locator('select[name="cdp_proto"]').select_option('passive_v2_only')
        with page.expect_response(lambda reply: reply.url == settings + '/settings' and reply.request.method == 'PUT') as save:
            page.get_by_role('button', name='Save LLDP settings', exact=True).click()
        assert save.value.ok, save.value.text()
        result = page.request.get(settings, headers=auth).json()['data']['values']
        assert result['receiveonly'] is True and result['cdp_proto'] == 'passive_v2_only' and not result['enable']
        request = save.value.request.post_data_json
        command = auth | {'Origin':origin, 'X-FreeSense-Command':'package.lldp-settings.save'}
        assert page.request.put(settings + '/settings', headers=command, data=request).status == 409
        apply = settings + '/apply'
        apply_headers = command | {'X-FreeSense-Command':'package.lldp-settings.apply'}
        assert page.request.put(apply, headers=apply_headers, data={'revision':'0' * 64}).status == 409
        expect(page.get_by_text('The saved settings are pending application.', exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence/'lldp-settings.png', full_page=True)
        with page.expect_response(lambda reply: reply.url == apply and reply.request.method == 'PUT') as applied:
            page.get_by_role('button', name='Apply saved settings', exact=True).click()
        assert applied.value.ok, applied.value.text()
        active = applied.value.json()['data']
        assert active['revision'] == result['revision'] and active['state'] == 'disabled' and not active['running']
        expect(page.get_by_role('button', name='Apply saved settings', exact=True)).to_have_count(0)
        print('Chromium verified LLDP grants, safe neighbor text, revisioned Save and applied disabled settings.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
