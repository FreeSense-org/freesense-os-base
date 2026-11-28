"""Verify administrative credential issuance without persisting or printing the delivery value."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
from xml.etree import ElementTree as ET
import secrets
import time

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_admin_key_issuance(page,origin,principal,container,docker,evidence):
    cli=['php','/check/prefix/sbin/freesense-config']
    original=docker('exec',container,*cli,'backup')
    doc=ET.fromstring(original)
    user=doc.find('./system/user'); ET.SubElement(user,'uid').text='701'
    ET.SubElement(user,'priv').text='page-system-restapi'
    owner=user.findtext('name')
    rec=ET.SubElement(doc.find('system'),'user')
    for k,v in [('name','dns-operator'),('uid','702'),('priv','page-system-restapi-keys'),('priv','page-services-dnsresolver')]:
        ET.SubElement(rec,k).text=v
    protected=ET.SubElement(doc.find('system'),'user')
    for k,v in [('name','service-identity'),('uid','703'),('priv','page-system-restapi-keys'),('managed','fixture')]:
        ET.SubElement(protected,k).text=v
    parent=doc.find('./restapi/apitoken')
    parent.find('scopes').text='users:write services.dns:read'
    ET.SubElement(parent,'expires').text=str(int(time.time())+3*86400)
    def key(name,uid,scope,readonly=False):
        ident=f'{uid:012x}'; suffix=secrets.token_urlsafe(32)
        token=ET.SubElement(doc.find('restapi'),'apitoken')
        for k,v in [('username',name),('id',ident),('scopes',scope),('hash',sha256(suffix.encode()).hexdigest())]:
            ET.SubElement(token,k).text=v
        if readonly: ET.SubElement(token,'readonly')
        return 'fsk_'+ident+'_'+suffix
    other=key('dns-operator',2,'users:write services.dns:read')
    scoped=key(owner,3,'users:read')
    ro=key(owner,4,'users:write',True)
    base=origin+'/api/v1/system/rest-api/keys'
    pick=base+'/recipients'
    def headers(token): return {'Authorization':'Bearer '+token,'Origin':origin,'X-FreeSense-Command':'createSystemApiKey'}
    def get(url,token): return page.request.get(url,headers=headers(token))
    def write(token,body): return page.request.put(base+'/create',headers=headers(token),data=body)
    def install(): docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
    def login(token):
        page.goto(origin+'/system/rest-api/keys')
        fill_api_key(page, token)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
    try:
        install()
        assert all(get(pick,t).status==403 for t in [other,scoped,ro])
        assert get(pick+'?search=dns',principal).json()=={'data':{'owners':[{'uid':702,'name':'dns-operator'}],'more':False}}
        assert get(pick+'?uid=703',principal).status==404
        assert get(pick+'?uid=702&search=dns',principal).status==400
        target=get(pick+'?uid=702',principal).json()['data']
        body={'recipientUid':702,'scopeMode':'limited','scopes':['services.dns:read'],'readonly':True,'description':'DNS audit','revision':target['revision']}
        assert write(ro,body).status==403 and write(scoped,body).status==403 and write(other,body).status==403
        assert write(principal,body|{'scopeMode':'full','scopes':[]}).status==422
        assert write(principal,body|{'scopes':['services.dns:write']}).status==422
        login(principal)
        launch=page.get_by_role('button',name='Create key',exact=True)
        launch.click()
        modal=page.get_by_role('dialog')
        search=modal.get_by_label('Find a local user',exact=True)
        search.fill('service')
        expect(modal.get_by_text('No eligible local users match.')).to_be_visible()
        search.fill('dns')
        expect(modal.get_by_label('Eligible local user',exact=True)).to_have_value('702')
        modal.get_by_role('button',name='Continue').click()
        expect(modal.get_by_text('This key acts as dns-operator.')).to_be_visible()
        modal.get_by_label('Description',exact=True).fill('DNS audit for remote office')
        modal.get_by_role('checkbox',name='read DNS services',exact=True).check()
        modal.get_by_label('Read-only',exact=True).check()
        # Re-create the recipient between approval and submission, without exposing any secret-bearing response.
        rec.find('uid').text='802'; install()
        with page.expect_response(lambda response: response.url==base+'/create') as stale:
            modal.get_by_role('button',name='Create key',exact=True).click()
        assert stale.value.status==409
        modal.get_by_role('button',name='Change user').click()
        search.fill('dns')
        expect(modal.get_by_label('Eligible local user',exact=True)).to_have_value('802')
        modal.get_by_role('button',name='Continue').click()
        modal.get_by_label('Description',exact=True).fill('DNS read access')
        modal.get_by_role('checkbox',name='read DNS services',exact=True).check()
        modal.get_by_label('Read-only',exact=True).check()
        if evidence: page.screenshot(path=evidence/'admin-key-recipient.png')
        with page.expect_response(lambda response: response.url==base+'/create') as created:
            modal.get_by_role('button',name='Create key',exact=True).click()
        assert created.value.status==200 and created.value.headers['cache-control']=='no-store'
        receipt=created.value.json()['data']; token=receipt['token']
        field=modal.get_by_label('New API key',exact=True)
        expect(field).to_have_attribute('type','password')
        assert field.input_value()==token
        modal.get_by_role('button',name='Done, I have copied it').click()
        expect(modal).to_be_hidden()
        assert token not in page.content() and get(origin+'/api/v1/services/dns/forwarders',token).status==200
        assert get(base,token).status==403 and get(pick,token).status==403
        mine=get(origin+'/api/v1/me/api-keys',token).json()['data']
        assert any(row['id']==receipt['id'] and row['current'] and row['readonly'] for row in mine['keys'])
        assert all(row['id']!=receipt['id'] for row in get(origin+'/api/v1/me/api-keys',principal).json()['data']['keys'])
        assert get(base,principal).status==200 and get(origin+'/api/v1/session',other).status==200
        stored=docker('exec',container,*cli,'backup')
        assert token[17:].encode() not in stored
        record=next(t for t in ET.fromstring(stored).findall('./restapi/apitoken') if t.findtext('id')==receipt['id'])
        assert record.findtext('owneruid')=='802' and record.findtext('username')=='dns-operator'
        login(scoped)
        expect(page.get_by_role('table')).to_be_visible()
        expect(page.get_by_role('button',name='Create key',exact=True)).to_have_count(0)
        print('Chromium verified authorized recipient search, changed UID rejection, scoped/readonly boundaries, one-time delivery, recipient-only inventory and an actual DNS read.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
