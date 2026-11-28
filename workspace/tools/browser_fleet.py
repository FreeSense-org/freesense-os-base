"""Verify optional node membership and its independent System menu grant in Chromium."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree
import base64
import hashlib
import json
import secrets
import time
from browser_shell import sign_out, menu_link, fill_api_key
from playwright.sync_api import expect


def exercise_fleet(page, origin, credential, container, docker, evidence=None):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    docker('exec',container,'cp','-R','/optional/sysutils/FreeSense-pkg-Fleet/files/usr/local/.','/check/prefix/')
    auth = {'Authorization':'Bearer '+credential}
    endpoint = origin+'/api/v1/packages/fleet'
    link = page.get_by_role('link',name='System: Central Management',exact=True)
    def login(grant,scope):
        doc = ElementTree.fromstring(original)
        user=doc.find('./system/user')
        for elem in user.findall('priv'):
            user.remove(elem)
        for item in ['page-system-restapi-keys',grant]:
            ElementTree.SubElement(user,'priv').text=item
        doc.find('./restapi/apitoken/scopes').text=scope
        docker('exec','-i',container,*cli,'restore',input=ElementTree.tostring(doc))
        page.goto(origin+'/packages')
        fill_api_key(page, credential)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()
    try:
        for grant,scope in [('page-system-fleet-controller','packages.fleet:read'),('page-system-fleet','status:read')]:
            login(grant,scope)
            expect(link).to_have_count(0)
            assert page.request.get(endpoint,headers=auth).status == 403
            sign_out(page)
        login('page-system-fleet','packages.fleet:read')
        menu_link(page,'System','System: Central Management').click()
        expect(page).to_have_url(origin+'/system/central-management')
        expect(page.locator('[data-fleet-membership]')).to_be_visible()
        expect(page.get_by_text('Not enrolled',exact=True)).to_be_visible()
        assert page.request.get(endpoint,headers=auth).json()['data']['values']=={'state':'not enrolled'}
        docker('exec',container,'mkdir','-m','700','/check/fleet')
        key=lambda:base64.b64encode(secrets.token_bytes(32)).decode()
        pending={'version':1,'stage':'awaiting approval','code':'012345','id':secrets.token_hex(6),'eid':'e-'+secrets.token_hex(10),
                 'digest':hashlib.sha256(b'fixture').hexdigest(),'fingerprint':secrets.token_hex(32),'started':int(time.time()),
                 'key':key(),'controller_key':key(),'material':key(),'lan':'192.0.2.1'}
        docker('exec','-i',container,'sh','-c','umask 077; cat > /check/fleet/enrollment.json',input=json.dumps(pending).encode())
        with page.expect_response(lambda r:r.url==endpoint and r.request.method=='GET'):
            page.get_by_role('button',name='Refresh',exact=True).click()
        expect(page.get_by_text('Awaiting controller approval',exact=True)).to_be_visible()
        assert page.request.get(endpoint,headers=auth).json()['data']['values']=={'state':'awaiting approval','code':pending['code']}
        assert all(pending[k] not in page.content() for k in ['key','controller_key','material','eid'])
        assert page.locator('input[type=password]:visible').count()==0
        if evidence:
            page.screenshot(path=str(evidence/'fleet-node.png'))
        assert page.request.post(endpoint+'/approve',headers=auth,data={}).status in [404,405]
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
