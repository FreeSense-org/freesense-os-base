"""Exercise one-time API key delivery using temporary local users. Never print credential values."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
import secrets
import time
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_personal_key_issuance(page, origin, principal, container, docker, evidence):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    doc = ET.fromstring(original)
    user = doc.find('./system/user')
    ET.SubElement(user,'uid').text = '701'
    ET.SubElement(user,'priv').text = 'page-status-interfaces'
    parent = doc.find('./restapi/apitoken')
    parent.find('scopes').text = 'users:write status:read services.dns:read'
    expiry = int(time.time())+30*3600
    ET.SubElement(parent,'expires').text = str(expiry)
    second = ET.SubElement(doc.find('system'),'user')
    for tag,value in [('name','two'),('uid','702'),('priv','page-system-restapi-keys'),('priv','user-config-readonly')]:
        ET.SubElement(second,tag).text = value
    secret = secrets.token_urlsafe(32)
    other = 'fsk_000000000002_'+secret
    otherkey = ET.SubElement(doc.find('restapi'),'apitoken')
    for tag,value in [('username','two'),('id','000000000002'),('hash',sha256(secret.encode()).hexdigest())]:
        ET.SubElement(otherkey,tag).text = value
    base = origin+'/api/v1/system/rest-api/my-keys'
    operation = base+'/create'
    def headers(token):
        return {'Authorization':'Bearer '+token,'Origin':origin,'X-FreeSense-Command':'createMyApiKey'}
    def get(url,token):
        return page.request.get(url,headers=headers(token))
    def write(body,token):
        return page.request.put(operation,headers=headers(token),data=body)
    def login(token):
        page.goto(origin+'/system/rest-api/my-keys')
        fill_api_key(page, token)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
    # Isolate explicit Copy from the workstation clipboard; retain only a synthetic digest.
    page.add_init_script('''Object.defineProperty(navigator,"clipboard",{value:{async writeText(s) {
      window.copyDigest = [...new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(s)))]
        .map(b=>b.toString(16).padStart(2,"0")).join("");
    }}})''')
    requests = []
    page.on('request',lambda request:requests.append(request.url))
    try:
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        login(principal)
        launch = page.get_by_role('button',name='Create key',exact=True)
        expect(launch).to_be_enabled()
        revision = get(base,principal).json()['data']['create']['revision']
        refused = {'revision':revision,'description':'Lab readonly','readonly':True,'scopeMode':'limited','scopes':['status:read']}
        assert write(refused,other).status==403 and write(refused|{'username':'two'},principal).status==422
        assert write(refused|{'scopeMode':'full','scopes':[]},principal).status==422
        launch.click()
        modal = page.get_by_role('dialog')
        expect(modal.get_by_role('heading',name='Create API Key')).to_be_visible()
        expect(modal.get_by_role('radio')).to_have_count(1)
        expect(modal.get_by_role('radio',name='Only the scopes selected below')).to_be_checked()
        expect(modal.get_by_label('Expires after (days)')).to_have_attribute('max','1')
        expect(modal.get_by_label('Description',exact=True)).to_be_focused()
        modal.get_by_role('button',name='Cancel').click()
        assert get(base,principal).json()['data']['total']==1
        launch.click()
        modal.get_by_label('Description',exact=True).fill('LAN metrics integration')
        submits = []
        page.on('request',lambda request:submits.append(request.method) if request.url==operation else None)
        modal.get_by_role('button',name='Create key',exact=True).click()
        expect(modal.locator('[data-error-for="scopes"]')).to_contain_text('Select between')
        assert not submits
        modal.get_by_role('checkbox',name='read DNS services',exact=True).check()
        modal.get_by_label('Read-only',exact=True).check()
        if evidence: page.screenshot(path=evidence/'personal-key-create.png')
        with page.expect_response(lambda response: response.url==operation and response.request.method=='PUT') as response:
            modal.get_by_role('button',name='Create key',exact=True).click()
        assert response.value.status==200 and response.value.headers['cache-control']=='no-store'
        created = response.value.json()['data']
        token = created['token']
        field = modal.get_by_label('New API key',exact=True)
        expect(field).to_have_attribute('type','password')
        assert field.input_value()==token
        modal.get_by_role('button',name='Show',exact=True).click()
        assert field.get_attribute('type')=='text'
        modal.get_by_role('button',name='Hide',exact=True).click()
        assert field.get_attribute('type')=='password'
        modal.get_by_role('button',name='Copy',exact=True).click()
        expect(modal.get_by_role('status')).to_have_text('Copied.')
        assert page.evaluate('window.copyDigest')==sha256(token.encode()).hexdigest()
        # No screenshot or trace captures the delivery view.
        modal.get_by_role('button',name='Done, I have copied it').click()
        expect(modal).to_be_hidden()
        assert page.locator('input').evaluate_all('(fields,t)=>fields.every(f=>f.value!==t)',token)
        assert token not in page.content() and all(token not in url for url in requests)
        storage = page.evaluate('JSON.stringify([localStorage,sessionStorage,history.state])')
        assert token not in storage and len(submits)==1
        assert write(refused,principal).status==409
        inventory = get(base,principal).json()['data']
        issued = next(row for row in inventory['keys'] if row['id']==created['id'])
        assert issued['state']=='active' and issued['readonly'] and issued['restricted'] and issued['expires']==expiry
        assert token not in get(base,principal).text() and get(operation,principal).status==405
        suffix = token[17:]
        stored = docker('exec',container,*cli,'backup')
        assert suffix.encode() not in stored
        record = next(k for k in ET.fromstring(stored).findall('./restapi/apitoken') if k.findtext('id')==created['id'])
        assert record.findtext('hash')==sha256(suffix.encode()).hexdigest() and record.findtext('username')==user.findtext('name')
        assert get(origin+'/api/v1/session',token).status==200
        assert get(origin+'/api/v1/services/dns/forwarders',token).status==200
        assert get(origin+'/api/v1/session','fsk_'+created['id']+'_'+principal[17:]).status==401
        assert get(origin+'/api/v1/system/rest-api/keys',token).status==403 and write(refused,token).status==403
        assert get(base,principal).status==200 and get(base,other).json()['data']['total']==1
        # Reopen a fresh grant, leave the second receipt undismissed, and reload. The browser starts signed out.
        launch.click()
        modal.get_by_label('Description',exact=True).fill('Second serving of metrics')
        modal.get_by_role('checkbox',name='read DNS services',exact=True).check()
        modal.get_by_role('button',name='Create key',exact=True).click()
        expect(modal.get_by_label('New API key',exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_role('heading',name='Appliance sign-in')).to_be_visible()
        expect(page.get_by_label('New API key',exact=True)).to_have_count(0)
        assert get(base,principal).json()['data']['total']==3
        login(other)
        expect(page.get_by_role('table')).to_be_visible()
        expect(page.get_by_role('button',name='Create key',exact=True)).to_have_count(0)
        print('Chromium verified account-bound creation, cancel and valid client/server input, one-time confidential delivery, independent read-only use, expiry and replay protection.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
