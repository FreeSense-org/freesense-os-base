"""Use the real router and browser to edit one authenticated local profile."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
import secrets
from xml.etree import ElementTree as ET

from browser_shell import sign_out, fill_api_key
from playwright.sync_api import expect


def exercise_account_profile(page, origin, credential, container, docker, evidence):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    config = ET.fromstring(original)
    first = config.find('./system/user')
    ET.SubElement(first,'uid').text = '89'
    ET.SubElement(first,'descr').text = 'Local Operator'
    ET.SubElement(first,'email').text = 'initial@example.test'
    def account(username, identifier, uid=None, readonly=False):
        owner = ET.SubElement(config.find('system'),'user')
        ET.SubElement(owner,'name').text = username
        ET.SubElement(owner,'priv').text = 'page-system-restapi-keys'
        if uid:
            ET.SubElement(owner,'uid').text = uid
        token = ET.SubElement(config.find('restapi'),'apitoken')
        secret = secrets.token_urlsafe(32)
        for name, value in [('id',identifier),('username',username),('hash',sha256(secret.encode()).hexdigest()),('scopes','status:read')]:
            ET.SubElement(token,name).text = value
        if readonly:
            ET.SubElement(token,'readonly')
        return 'fsk_'+identifier+'_'+secret
    remote = account('directory-user','000000000002')
    limited = account('read-only','000000000003','90',True)
    url = origin+'/api/v1/me/profile'
    form = page.locator('.account-form')
    name = form.get_by_role('textbox',name='Display name',exact=True)
    email = form.get_by_role('textbox',name='Email',exact=True)
    save = form.get_by_role('button',name='Save',exact=True)
    def auth(key):
        return {'Authorization':'Bearer '+key, 'Origin':origin, 'X-FreeSense-Command':'saveAccountProfile'}
    def login(key):
        page.goto(origin+'/account/profile')
        fill_api_key(page, key)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(form.get_by_role('heading',name='Profile',exact=True)).to_be_visible()
    def submit(status):
        with page.expect_response(lambda r: r.url == url and r.request.method == 'PUT') as response:
            save.click()
        assert response.value.status == status
        return response.value
    try:
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(config))
        assert page.request.get(url).status == 401
        login(credential)
        expect(name).to_have_value('Local Operator')
        expect(email).to_have_value('initial@example.test')
        expect(form.get_by_text('test-operator',exact=True)).to_be_visible()
        seen = []
        page.on('request',lambda r:seen.append(r.method) if r.url==url and r.method=='PUT' else None)
        name.fill('é'*65)
        save.click()
        expect(name).to_have_attribute('aria-invalid','true')
        name.fill('  Núria Rao  ')
        for bad in ['a..b@example.test','a@single','a@host.7','a@[IPv6:bad]']:
            email.fill(bad)
            save.click()
            expect(email).to_have_attribute('aria-invalid','true')
        assert seen == [], 'Invalid local input reached the profile writer'
        email.fill('"on.call"@example.test')
        initial = page.request.get(url,headers=auth(credential)).json()['data']
        assert page.request.put(url,headers=auth(credential),data={'revision':initial['revision'],'email':'parallel@example.test'}).status == 200
        submit(409)
        expect(name).to_have_value('  Núria Rao  ')
        page.get_by_role('button',name='Refresh',exact=True).click()
        expect(email).to_have_value('parallel@example.test')
        name.fill('  Núria Rao  ')
        email.fill('operator+notify@example.test')
        data = submit(200).json()['data']
        expect(page.get_by_text('Profile saved.',exact=True)).to_be_visible()
        expect(name).to_have_value('Núria Rao')
        expect(page.locator('.account-avatar:not(.account-card-avatar)')).to_have_text('NR')
        assert data['email'] == 'operator+notify@example.test' and data['name'] == 'Núria Rao'
        assert page.request.put(url,headers=auth(credential),data={'revision':data['revision'],'uid':'90'}).status == 422
        if evidence:
            page.screenshot(path=evidence/'account-profile.png')
        email.fill('')
        submit(200)
        stored = ET.fromstring(docker('exec',container,*cli,'backup'))
        mine = stored.find('./system/user')
        assert mine.findtext('descr') == 'Núria Rao' and mine.find('email') is None and mine.findtext('uid') == '89'
        assert len(stored.findall('./system/user')) == 3
        sign_out(page)
        login(limited)
        expect(name).to_be_disabled()
        expect(email).to_have_value('')
        expect(save).to_have_count(0)
        assert 'Núria Rao' not in form.text_content()
        assert page.request.put(url,headers=auth(limited),data={'revision':data['revision'],'name':'No'}).status == 403
        sign_out(page)
        login(remote)
        expect(form.get_by_text('Managed on the authentication server.',exact=True)).to_be_visible()
        expect(name).to_have_count(0)
        expect(email).to_have_count(0)
        expect(save).to_have_count(0)
        assert 'saveAccountProfile' not in page.request.get(origin+'/api/v1/forms',headers=auth(remote)).json()['data']['operations']
        their = page.request.get(url,headers=auth(remote)).json()['data']
        assert their['username'] == 'directory-user' and not their['local']
        response = page.request.put(url,headers=auth(remote),data={'revision':their['revision'],'name':'No'})
        assert response.status==409 and response.json()['error']['code']=='remote_user'
        page.set_viewport_size({'width':390,'height':844})
        if evidence:
            page.screenshot(path=evidence/'managed-profile-mobile.png')
        print('Chromium verified the routed local Profile editor, immediate avatar, central client/server checks, '
              'concurrent edit conflicts, clearing email, independent owners and managed-account presentation.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
