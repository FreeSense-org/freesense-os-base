"""Verify the Security tab using disposable credentials for two independent owners."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
import secrets
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_account_security(page, origin, key, container, docker, evidence):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    doc = ET.fromstring(original)
    first = doc.find('./system/user')
    second = ET.SubElement(doc.find('system'),'user')
    ET.SubElement(second,'name').text = 'other-operator'
    ET.SubElement(second,'priv').text = 'page-system-restapi-keys'
    own = first.findtext('name')
    def credential(owner, identifier, readonly=False, managed=False):
        secret = secrets.token_urlsafe(32)
        token = ET.SubElement(doc.find('restapi'),'apitoken')
        for field,value in [('id',identifier),('username',owner),('hash',sha256(secret.encode()).hexdigest()),
                            ('descr','fsk_'+identifier+'_'+secret),('scopes','status:read')]:
            ET.SubElement(token,field).text = value
        if readonly: ET.SubElement(token,'readonly')
        if managed: ET.SubElement(token,'managed').text='fixture'
        return 'fsk_'+identifier+'_'+secret
    readonly = credential(own,'000000000002',readonly=True)
    other = credential('other-operator','000000000003')
    managed = credential(own,'000000000004',managed=True)
    endpoint = origin+'/api/v1/me/api-keys'
    def auth(token):
        return {'Authorization':'Bearer '+token,'Origin':origin,'X-FreeSense-Command':'revokeOwnApiKeys'}
    def read(token):
        return page.request.get(endpoint,headers=auth(token))
    def revoke(token,body):
        return page.request.put(endpoint+'/revoke',headers=auth(token),data=body)
    def login(token, path='/account/security'):
        page.goto(origin+path)
        fill_api_key(page, token)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
    def install():
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
    def confirm(status):
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_response(lambda response: response.url==endpoint+'/revoke' and response.request.method=='PUT') as result:
            button.click()
        assert result.value.status==status
        return result.value.json()
    button = page.get_by_role('button',name='Revoke all my API keys',exact=True)
    try:
        install()
        assert page.request.get(endpoint).status==401
        login(key,'/account/profile')
        page.get_by_role('link',name='Security',exact=True).click()
        expect(page.get_by_role('heading',name='API keys',exact=True)).to_be_visible()
        expect(button).to_be_enabled()
        expect(page.get_by_text('This key',exact=False)).to_be_visible()
        expect(page.get_by_role('cell',name='Read-only',exact=True)).to_be_visible()
        inventory = read(key).json()['data']
        assert inventory['total']==2
        assert all(set(row)=={'id','current','created','expires','readonly','session','state'} for row in inventory['keys']), inventory['keys']
        assert {row['id'] for row in inventory['keys']}=={key.split('_',2)[1],'000000000002'}
        for token in [key,readonly,managed,other]:
            assert token not in read(key).text() and token not in page.locator('#content').inner_text()
        other_before = read(other).json()['data']
        assert other_before['total']==1
        assert read(managed).status==403
        pref = origin+'/api/v1/me/preferences'
        language_headers = auth(key)|{'X-FreeSense-Command':'saveAccountAppearance'}
        def language(code):
            current = page.request.get(pref,headers=language_headers).json()['data']['revision']
            assert page.request.put(pref,headers=language_headers,data={'revision':current,'language':code}).status==200
        language('de_DE')
        login(readonly)
        expect(page.get_by_role('heading',name='API-Schlüssel',exact=True)).to_be_visible()
        expect(page.get_by_role('cell',name='Schreibgeschützt',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Alle meine API-Schlüssel widerrufen',exact=True)).to_have_count(0)
        language('en_US')
        approval = {'revision':inventory['revision'],'confirmation':'revoke-all'}
        assert revoke(readonly,approval).status==403 and revoke(other,approval).status==409
        assert revoke(key,approval|{'username':'other-operator'}).status==422
        assert revoke(key,{'revision':inventory['revision']}).status==422
        assert revoke(key,{'revision':inventory['revision'],'confirmation':'no'}).status==422
        login(key)
        expect(button).to_be_enabled()
        sent = []
        page.on('request',lambda request:sent.append(request.method) if request.url==endpoint+'/revoke' else None)
        page.once('dialog',lambda dialog:dialog.dismiss())
        button.click()
        assert not sent and read(key).json()['data']==inventory
        additional = credential(own,'000000000005')
        install()
        confirm(409)
        expect(page.get_by_text('Your API keys changed. Refresh before revoking them.')).to_be_visible()
        assert read(additional).status==200 and read(readonly).status==200
        page.get_by_role('button',name='Refresh',exact=True).click()
        expect(page.get_by_role('cell',name='000000000005',exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence/'account-security.png',full_page=True)
        assert confirm(200)=={'data':{'revoked':3}}
        expect(page.get_by_role('heading',name='Appliance sign-in')).to_be_visible()
        expect(page.locator('#sign-in-notice')).to_have_text('Your personal API keys have been revoked. Sign in with a new key.')
        for token in [key,readonly,additional]:
            assert read(token).status==401
        assert read(other).json()['data']==other_before
        assert page.request.get(origin+'/api/v1/session',headers=auth(managed)).status==200
        login(other)
        expect(page.get_by_role('table').get_by_text('000000000003',exact=True)).to_be_visible()
        print('Chromium verified personal API-key inventory, explicit and canceled confirmation, readonly and managed scope, '
              'concurrent key changes, immediate sign-out and rejection, and continued access for the independent operator.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
