"""Check the System REST API inventory and individual revocation on an isolated router."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
import secrets
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_system_api_keys(page, origin, principal, container, docker, evidence):
    cli = ['php','/check/prefix/sbin/freesense-config']
    saved = docker('exec',container,*cli,'backup')
    doc = ET.fromstring(saved)
    first = doc.find('./system/user')
    lead = first.findtext('name')
    ET.SubElement(first,'priv').text = 'page-system-restapi'
    doc.find('./restapi/apitoken/scopes').text = 'users:write'
    second = ET.SubElement(doc.find('system'),'user')
    ET.SubElement(second,'name').text = 'operations'
    ET.SubElement(second,'priv').text = 'page-system-restapi-keys'
    ET.SubElement(second,'priv').text = 'api-users-write'

    def credential(owner, number, scope='users:write', managed=False):
        suffix = secrets.token_urlsafe(32)
        identifier = f'{number:012x}'
        token = 'fsk_'+identifier+'_'+suffix
        key = ET.SubElement(doc.find('restapi'),'apitoken')
        for field,value in [('username',owner),('id',identifier),('hash',sha256(suffix.encode()).hexdigest()),('descr',token),('scopes',scope)]:
            ET.SubElement(key,field).text = value
        if managed: ET.SubElement(key,'managed').text = 'local-fixture'
        return token

    other = credential('operations',2)
    item = credential('operations',3)
    readonly = credential(lead,4,'users:read')
    managed = credential(lead,5,managed=True)
    limited = credential(lead,6,'status:read')
    for i in range(7,24): credential('operations',i)
    all_url = origin+'/api/v1/system/rest-api/keys'
    own_url = origin+'/api/v1/system/rest-api/my-keys'
    def headers(token):
        return {'Authorization':'Bearer '+token,'Origin':origin}
    def read(url,token):
        return page.request.get(url,headers=headers(token))
    def revoke(url,token,body):
        command = 'revokeSystemApiKey' if url==all_url else 'revokeMyApiKey'
        return page.request.put(url+'/revoke',headers=headers(token)|{'X-FreeSense-Command':command},data=body)
    def login(token,url):
        page.goto(origin+url)
        fill_api_key(page, token)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
    def install():
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
    try:
        install()
        assert page.request.get(all_url).status==401
        assert all(read(all_url,t).status==403 for t in [other,managed,limited])
        assert read(own_url,managed).status==403
        assert read(all_url+'?page=0',principal).status==400 and read(all_url+'?user=operations',other).status==400
        login(principal,'/system/rest-api/keys')
        expect(page.get_by_role('heading',name='All Keys')).to_be_visible()
        expect(page.get_by_role('link',name='My API Keys',exact=True)).to_be_visible()
        row = page.get_by_role('row').filter(has=page.get_by_text('fsk_000000000003',exact=True))
        button = row.get_by_role('button',name='Revoke fsk_000000000003')
        expect(button).to_be_enabled()
        expect(row.get_by_role('cell',name='operations',exact=True)).to_be_visible()
        view = read(all_url,principal).json()['data']
        assert view['total']==22 and len(view['keys'])==20
        assert 'revision' not in read(all_url,readonly).json()['data']['keys'][0]
        assert all(t not in read(all_url,principal).text() for t in [principal,other,item,readonly,managed,limited])
        page.get_by_role('button',name='Next page').click()
        expect(page.get_by_text('Page 2 of 2',exact=True)).to_be_visible()
        page.get_by_role('button',name='Previous page').click()
        expect(button).to_be_enabled()
        page.get_by_label('Search key, user or description').fill('000000000003')
        expect(row).to_be_visible()
        expect(page.get_by_role('table').locator('tbody tr:visible')).to_have_count(1)
        page.get_by_label('Search key, user or description').fill('')
        approval = {'id':'000000000003','revision':view['keys'][2]['revision'],'confirmation':'revoke'}
        assert revoke(own_url,other,approval).status==409 and revoke(all_url,other,approval).status==403
        assert revoke(all_url,readonly,approval).status==403 and revoke(all_url,principal,approval|{'username':'operations'}).status==422
        page.once('dialog',lambda dialog:dialog.dismiss())
        button.click()
        assert read(own_url,item).status==200
        node = next(k for k in doc.findall('./restapi/apitoken') if k.findtext('id')=='000000000003')
        node.find('descr').text = 'Rotated approval'
        install()
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_response(lambda response:response.url==all_url+'/revoke') as stale:
            button.click()
        assert stale.value.status==409 and read(own_url,item).status==200
        page.get_by_role('button',name='Refresh',exact=True).click()
        expect(button).to_be_enabled()
        if evidence: page.screenshot(path=evidence/'system-api-keys.png',full_page=True)
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_response(lambda response:response.url==all_url+'/revoke') as revoked:
            button.click()
        assert revoked.value.json()=={'data':{'revoked':'000000000003','current':False}}
        expect(row.get_by_role('cell',name='Revoked',exact=True)).to_be_visible()
        expect(row.get_by_role('button',name='Revoke fsk_000000000003')).to_have_count(0)
        expect(row.get_by_role('button',name='Delete fsk_000000000003')).to_be_enabled()
        assert read(own_url,item).status==401 and read(own_url,other).status==200
        assert page.request.get(origin+'/api/v1/session',headers=headers(managed)).status==200
        login(readonly,'/system/rest-api/keys')
        expect(page.get_by_role('table')).to_be_visible()
        expect(page.get_by_role('button',name='Revoke fsk_',exact=False)).to_have_count(0)
        login(other,'/system/rest-api/my-keys')
        expect(page.get_by_role('link',name='All Keys',exact=True)).to_have_count(0)
        expect(page.get_by_role('columnheader',name='User')).to_have_count(0)
        expect(page.get_by_role('cell',name='Revoked',exact=True)).to_be_visible()
        mine = page.get_by_role('button',name='Revoke fsk_000000000002')
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_response(lambda response:response.url==own_url+'/revoke') as own:
            mine.click()
        assert own.value.json()=={'data':{'revoked':'000000000002','current':True}}
        expect(page.get_by_role('heading',name='Appliance sign-in')).to_be_visible()
        assert read(own_url,other).status==401 and read(all_url,principal).status==200
        print('Chromium verified global and personal inventories, pagination, search, exact grants, read-only and managed boundaries, stale/canceled confirmations and individual retained revocation.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=saved)
