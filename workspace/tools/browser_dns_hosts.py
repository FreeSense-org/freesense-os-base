"""Exercise the host list, DNS record editor, central validation and independent grants."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree
import json

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_dns_hosts(page, origin, credential, container, docker, evidence=None):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    endpoint = origin + '/api/v1/services/dns/host-overrides'
    path = '/services/dns/host-overrides'
    auth = {'Authorization':'Bearer '+credential}
    general = 'page-services-dnsresolver'
    editor = 'page-services-dnsresolver-edithost'

    def login(grants, scope, destination='/services/dns'):
        doc = ElementTree.fromstring(docker('exec',container,*cli,'backup'))
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for grant in ['page-system-restapi-keys',*grants]:
            ElementTree.SubElement(user,'priv').text = grant
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec','-i',container,*cli,'restore',input=ElementTree.tostring(doc))
        page.goto(origin+destination)
        fill_api_key(page, credential)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()

    def request(target, command, fields):
        return page.request.put(target,headers=auth|{'Origin':origin,'X-FreeSense-Command':command},data=fields)

    try:
        login([general],'services.dns:read')
        page.get_by_role('link',name='Host Overrides',exact=True).click()
        expect(page.get_by_role('cell',name='printer',exact=True)).to_be_visible()
        expect(page.get_by_role('link',name='Add host override')).to_have_count(0)
        expect(page.get_by_role('button',name='Delete printer.office.test')).to_have_count(0)
        baseline = page.request.get(endpoint,headers=auth).json()['data']
        assert page.request.get(endpoint+'/'+baseline['rows'][0]['id'],headers=auth).status == 200
        assert request(endpoint,'createDnsHostOverride',{}).status == 403
        assert page.request.get(endpoint+'/edit',headers=auth).status == 403
        login([editor],'services.dns:write',path+'/edit')
        assert page.request.get(endpoint,headers=auth).status == 403
        expect(page.get_by_role('heading',name='Host Override Options')).to_be_visible()
        page.get_by_label('Host',exact=True).fill('gateway')
        page.get_by_label('Domain',exact=True).fill('lab.test')
        page.get_by_label('Description',exact=True).fill('VPN & lab')
        addresses = page.locator('[data-computed="addresses"]').first
        addresses.locator('input').fill('192.0.2.30')
        addresses.get_by_role('button',name='Add item').click()
        addresses.locator('input').last.fill('2001:db8::30')
        aliases = page.locator('[data-computed="aliases"]').first
        aliases.get_by_role('button',name='Add item').click()
        aliases.get_by_label('Host',exact=True).fill('vpn')
        aliases.get_by_label('Domain',exact=True).fill('bad.')
        aliases.get_by_label('Description',exact=True).fill('Remote users')
        submitted = []
        page.on('request',lambda req: submitted.append(req.method) if req.url==endpoint and req.method=='PUT' else None)
        page.get_by_role('button',name='Save',exact=True).click()
        expect(page.locator('[data-error-for="aliases"]')).to_contain_text('Domain')
        assert submitted == []
        aliases.get_by_label('Domain',exact=True).fill('lab.test')
        with page.expect_response(lambda r:r.url==endpoint and r.request.method=='PUT') as answer:
            page.get_by_role('button',name='Save',exact=True).click()
        assert answer.value.status == 200
        receipt = answer.value.json()['data']
        if evidence:
            submitted_record = answer.value.request.post_data_json.copy()
            submitted_record.pop('revision')
            (evidence/'dns-host-submitted.json').write_text(json.dumps(submitted_record,indent=2)+'\n',encoding='utf-8')
        assert set(receipt) == {'revision','id'}
        expect(page).to_have_url(origin+path+'/edit/'+receipt['id'])
        expect(aliases.get_by_label('Description',exact=True)).to_have_value('Remote users')
        assert page.request.get(endpoint+'/'+receipt['id'],headers=auth).status == 403
        assert request(endpoint+'/remove','removeDnsHostOverride',{'revision':receipt['revision'],'id':receipt['id']}).status == 403
        if evidence:
            page.screenshot(path=str(evidence/'dns-host-editor.png'),full_page=True)
        login([general,editor],'services.dns:write',path)
        page.get_by_label('Search host overrides').fill('vpn.lab')
        expect(page.get_by_role('link',name='Edit gateway.lab.test')).to_be_visible()
        expect(page.get_by_role('link',name='Edit printer.office.test')).to_have_count(0)
        if evidence:
            page.screenshot(path=str(evidence/'dns-host-list.png'),full_page=True)
        page.get_by_role('link',name='Edit gateway.lab.test').click()
        page.get_by_label('Host',exact=True).first.fill('')
        with page.expect_response(lambda r:r.url==endpoint+'/edit' and r.request.method=='PUT') as updated:
            page.get_by_role('button',name='Save',exact=True).click()
        assert updated.value.status == 200
        renamed = updated.value.json()['data']
        assert renamed['id'] != receipt['id']
        assert request(endpoint+'/remove','removeDnsHostOverride',{'id':renamed['id'],'revision':receipt['revision']}).status == 409
        login([general],'services.dns:write',path)
        expect(page.get_by_role('link',name='Add host override')).to_have_count(0)
        assert request(endpoint+'/edit','editDnsHostOverride',{}).status == 403
        page.once('dialog',lambda dialog: dialog.dismiss())
        page.get_by_role('button',name='Delete lab.test',exact=True).click()
        expect(page.get_by_role('button',name='Delete lab.test',exact=True)).to_be_visible()
        page.once('dialog',lambda dialog: dialog.accept())
        with page.expect_response(lambda r:r.url==endpoint+'/remove' and r.request.method=='PUT') as removed:
            page.get_by_role('button',name='Delete lab.test',exact=True).click()
        assert removed.value.status==200
        expect(page.get_by_role('button',name='Delete lab.test',exact=True)).to_have_count(0)
        assert page.request.get(endpoint,headers=auth).json()['data']==baseline
        print('Chromium verified resolver tabs, bounds, mixed IPv4/IPv6 records, aliases, search, rename, confirmed removal, revision conflict and separate grants.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
