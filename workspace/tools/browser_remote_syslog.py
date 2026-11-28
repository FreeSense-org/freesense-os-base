from xml.etree import ElementTree as ET

import json
from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_remote_syslog(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    route = '/status/logs/settings/remote'
    endpoint = origin + '/api/v1/logs/settings/remote'
    auth = {'Authorization': 'Bearer ' + key}
    write = auth | {'Origin': origin, 'X-FreeSense-Command': 'saveRemoteSyslog'}

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
        # The backend reads interfaces from the native helper (JSON rows), no longer from ifconfig; supply it for this fixture.
        rows = json.dumps([{'name': 'em0', 'flags': ['UP', 'BROADCAST', 'RUNNING', 'MULTICAST'], 'mtu': 1500,
                            'addresses': [{'family': 'inet', 'address': '192.0.2.1', 'prefix': 24}]},
                           {'name': 'lo0', 'flags': ['UP', 'LOOPBACK', 'RUNNING', 'MULTICAST'], 'mtu': 16384,
                            'addresses': [{'family': 'inet', 'address': '127.0.0.1', 'prefix': 8}, {'family': 'inet6', 'address': '::1', 'prefix': 128}]}])
        docker('exec', '-i', container, 'sh', '-c', 'cat > /check/syslog-nics', input=rows.encode())
        docker('exec', '-i', container, 'sh', '-c', 'mkdir -p /usr/local/sbin && cat > /usr/local/sbin/freesense-sysinfo && chmod 755 /usr/local/sbin/freesense-sysinfo',
               input=b'#!/bin/sh\ncase "$1" in --version) echo 1 ;; interfaces) cat /check/syslog-nics ;; *) exit 1 ;; esac\n')
        configure('page-diagnostics-logs-firewall', 'logs:write')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-diagnostics-logs-settings', 'logs:read')
        lookup = page.request.get(endpoint, headers=auth)
        assert lookup.ok, lookup.text()
        draft = lookup.json()['data']['values']
        assert 'lo0' in lookup.json()['data']['sources'], lookup.json()['data']['sources']
        assert draft['enable'] is False and not draft['servers']
        assert page.request.put(endpoint, headers=write, data=draft).status == 403
        configure('page-diagnostics-logs-settings', 'logs:write')
        enabled = draft | {'enable': True, 'contents': ['filter']}
        assert page.request.put(endpoint, headers=write, data=enabled).status == 422
        bad = enabled | {'servers': ['collector.test:65536']}
        assert page.request.put(endpoint, headers=write, data=bad).status == 422
        wrong_family = enabled | {'sourceip':'127.0.0.1', 'servers':['[2001:db8::53]:1514']}
        refused = page.request.put(endpoint, headers=write, data=wrong_family)
        assert refused.status == 422 and 'servers' in refused.json()['error']['fields']
        assert page.request.get(endpoint, headers=auth).json()['data']['values'] == draft
        page.goto(origin + route)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        form = page.locator('#content form')
        expect(form.get_by_label('Send log messages to remote syslog servers')).to_be_visible()
        form.get_by_label('Send log messages to remote syslog servers').check()
        targets = form.get_by_role('group', name='Remote log servers')
        targets.get_by_role('button', name='Add item').click()
        targets.get_by_role('textbox').fill('collector.test:1514')
        form.get_by_role('combobox', name='Source address').select_option('lo0')
        form.get_by_label('Firewall events', exact=True).check()
        with page.expect_response(lambda response: response.url == endpoint and response.request.method == 'PUT') as response:
            form.get_by_role('button', name='Save settings').click()
        assert response.value.ok, response.value.text()
        after = page.request.get(endpoint, headers=auth).json()['data']['values']
        assert after['servers'] == ['collector.test:1514'] and after['contents'] == ['filter'] and after['sourceip'] == 'lo0'
        assert page.request.put(endpoint, headers=write, data=enabled | {'servers':['192.0.2.53']}).status == 409
        if evidence:
            page.screenshot(path=evidence/'remote-logging.png', full_page=True)
        stopped = page.request.put(endpoint, headers=write, data=after | {'enable':False})
        assert stopped.ok, stopped.text()
        confirmed = page.request.get(endpoint, headers=auth).json()['data']['values']
        assert not confirmed['enable'] and confirmed['servers'] == [] and confirmed['contents'] == ['filter']
        servers = ['192.0.2.53', '[2001:db8::53]:1514', 'collector.test:2514']
        mixed = confirmed | {'enable':True, 'sourceip':'', 'servers':servers, 'contents':['all']}
        result = page.request.put(endpoint, headers=write, data=mixed)
        assert result.ok, result.text()
        confirmed = page.request.get(endpoint, headers=auth).json()['data']['values']
        assert confirmed['sourceip'] == '' and confirmed['servers'] == servers and confirmed['contents'] == ['all']
        repeated = confirmed | {'servers':['192.0.2.53', '192.0.2.53:514']}
        denied = page.request.put(endpoint, headers=write, data=repeated)
        assert denied.status == 422 and 'servers' in denied.json()['error']['fields']
        assert page.request.get(endpoint, headers=auth).json()['data']['values'] == confirmed
        activation = endpoint + '/activation'
        activation_headers = write | {'X-FreeSense-Command':'activateRemoteSyslog'}
        assert page.request.put(activation, headers=activation_headers,
                                data={'action':'apply','revision':'0' * 64}).status == 409
        assert page.request.put(activation, headers=activation_headers,
                                data={'action':'restart','revision':confirmed['revision']}).status == 422
        assert page.request.get(endpoint, headers=auth).json()['data']['values'] == confirmed
        active = '1' * 64
        requests = []
        state = {'state':'active', 'active':active, 'pending':True, 'canRollback':True}

        def observed(request):
            if request.request.method == 'PUT':
                payload = request.request.post_data_json
                requests.append(payload)
                state['active'] = payload['revision']
                state['pending'] = False
            request.fulfill(json={'data':state})

        page.route(activation, observed)
        page.goto(origin + route)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        apply = page.get_by_role('button', name='Apply saved settings', exact=True)
        expect(apply).to_be_visible()
        page.once('dialog', lambda dialog: dialog.dismiss())
        apply.click()
        assert requests == []
        rollback = page.get_by_role('button', name='Restore previous forwarding', exact=True)
        page.once('dialog', lambda dialog: dialog.accept())
        rollback.click()
        expect(page.get_by_text('Previous remote logging settings restored.', exact=True)).to_be_visible()
        assert requests == [{'action':'rollback', 'revision':active}]
        state['pending'] = True
        page.reload()
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        page.once('dialog', lambda dialog: dialog.accept())
        apply.click()
        expect(page.get_by_text('Remote logging settings applied.', exact=True)).to_be_visible()
        assert requests[-1] == {'action':'apply', 'revision':confirmed['revision']}
        configure('page-diagnostics-logs-settings', 'logs:read')
        assert page.request.put(activation, headers=activation_headers, data=requests[-1]).status == 403
        page.reload()
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('heading', name='Active forwarding')).to_be_visible()
        expect(page.get_by_role('button', name='Restore previous forwarding', exact=True)).to_have_count(0)
        print('Chromium verified remote logging grants, client form save, server validation and stale revisions.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
