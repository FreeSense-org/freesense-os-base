"""Run Account language through the real shell, production validation and per-account store."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
import json
import re
import secrets
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_account_language(page, origin, key, container, docker, evidence):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    doc = ET.fromstring(original)
    system = doc.find('system')
    first = system.find('user')
    ET.SubElement(first,'uid').text = '84'
    ET.SubElement(first,'descr').text = 'Save'
    for grant in ('page-system-login-logout','page-status-interfaces'):
        ET.SubElement(first,'priv').text = grant
    doc.find('./restapi/apitoken/scopes').text = 'status:read'
    second = ET.SubElement(system,'user')
    for field,value in [('name','second-operator'),('uid','85'),('priv','page-system-restapi-keys'),('priv','page-system-login-logout')]:
        ET.SubElement(second,field).text = value
    def token(identifier, owner, readonly=False):
        secret = secrets.token_urlsafe(32)
        item = ET.SubElement(doc.find('restapi'),'apitoken')
        for field,value in [('id',identifier),('username',owner.findtext('name')),('scopes','status:read'),('hash',sha256(secret.encode()).hexdigest())]:
            ET.SubElement(item,field).text = value
        if readonly: ET.SubElement(item,'readonly')
        return 'fsk_'+identifier+'_'+secret
    other = token('000000000002',second)
    limited = token('000000000003',first,True)
    prefs = origin+'/api/v1/me/preferences'
    profile = origin+'/api/v1/me/profile'
    root = page.locator('html')
    def login(credential):
        page.goto(origin+'/account/appearance')
        fill_api_key(page, credential)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('.account-form h2')).to_be_visible()
    def auth(credential,command):
        return {'Authorization':'Bearer '+credential,'Origin':origin,'X-FreeSense-Command':command}
    def form_submit(label, url, status):
        with page.expect_response(lambda r:r.url==url and r.request.method=='PUT') as response:
            page.get_by_role('button',name=label,exact=True).click()
        assert response.value.status==status
    def set_language(label, value, button):
        page.get_by_role('combobox',name=label,exact=True).select_option(value)
        form_submit(button,prefs,200)
    def config():
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))

    try:
        config()
        loads = []
        page.on('request',lambda req:loads.append('de_DE') if req.url==origin+'/assets/locales/de.js' else None)
        login(key)
        language = page.get_by_role('combobox',name='Language',exact=True)
        expect(language).to_have_value('')
        expect(language.locator('option')).to_have_text(['System default','English','German (Germany)'])
        assert not loads
        set_language('Language','de_DE','Save')
        expect(root).to_have_attribute('lang','de-DE')
        expect(page.get_by_text('Darstellung gespeichert.',exact=True)).to_be_visible()
        expect(page.get_by_role('heading',name='Darstellung',exact=True)).to_be_visible()
        expect(page.get_by_role('combobox',name='Sprache',exact=True)).to_have_value('de_DE')
        expect(page.get_by_role('combobox',name='Modus',exact=True)).to_be_visible()
        expect(page.locator('#appliance-menu details:has(#context-help) summary')).to_have_text('Hilfe')
        if evidence:
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=evidence/'german-appearance.png',full_page=True)
        page.get_by_role('link',name='Profil',exact=True).click()
        name = page.get_by_role('textbox',name='Anzeigename',exact=True)
        email = page.get_by_role('textbox',name='E-Mail-Adresse',exact=True)
        expect(name).to_have_value('Save')
        expect(page).to_have_title(re.compile(r'Konto'))
        submissions = []
        page.on('request',lambda request:submissions.append(request.method) if request.url==profile and request.method=='PUT' else None)
        email.fill('bad address')
        page.get_by_role('button',name='Speichern',exact=True).click()
        expect(page.get_by_text('Geben Sie eine gültige E-Mail-Adresse ein oder lassen Sie das Feld leer.')).to_be_visible()
        assert submissions==[]
        email.fill('first@example.test')
        live = page.request.get(profile,headers=auth(key,'saveAccountProfile')).json()['data']
        assert page.request.put(profile,headers=auth(key,'saveAccountProfile'),data={'revision':live['revision'],'name':'Help'}).status==200
        form_submit('Speichern',profile,409)
        expect(page.get_by_text('Ihr Profil wurde geändert. Aktualisieren Sie die Seite vor dem Speichern.')).to_be_visible()
        page.get_by_role('button',name='Aktualisieren',exact=True).click()
        expect(name).to_have_value('Help')
        email.fill('first@example.test')
        form_submit('Speichern',profile,200)
        expect(page.get_by_text('Profil gespeichert.',exact=True)).to_be_visible()
        page.get_by_role('link',name='Einstellungen',exact=True).click()
        expect(page.get_by_role('heading',name='Bei der Anmeldung',exact=True)).to_be_visible()
        expect(page.get_by_role('combobox',name='Dashboard-Aktualisierung',exact=True)).to_have_value('on')
        reset = page.get_by_role('button',name='Mein Dashboard zurücksetzen')
        def confirm(dialog):
            assert dialog.message=='Dashboard auf die Standardanordnung zurücksetzen?'
            dialog.accept()
        page.once('dialog',confirm)
        reset.click()
        expect(page.get_by_text('Die Standardanordnung Ihres Dashboards wurde wiederhergestellt.')).to_be_visible()
        page.get_by_role('button',name='Menüs durchsuchen',exact=True).click()
        page.get_by_role('combobox',name='Menüs durchsuchen',exact=True).fill('Schnittstellen')
        expect(page.get_by_role('option').first).to_contain_text('Schnittstellen')
        page.keyboard.press('Escape')
        # The home page is the widget dashboard (the old live statistics table under the page is no longer shown): its widget
        # titles and labels follow the German catalogue too.
        page.locator('nav [data-home]').click()
        expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Widget hinzufügen',exact=True)).to_be_visible()
        assert loads==['de_DE'] and page.locator('#content').get_attribute('lang')=='en'
        page.locator('#account-toggle').click()
        page.get_by_role('button',name='Abmelden',exact=True).click()
        expect(page.get_by_role('button',name='Sign in',exact=True)).to_be_visible()
        login(other)
        expect(page.get_by_role('combobox',name='Language',exact=True)).to_have_value('')
        expect(root).to_have_attribute('lang','en-US')
        ET.SubElement(system,'language').text = 'de_DE'
        config()
        login(other)
        expect(page.get_by_role('combobox',name='Sprache',exact=True)).to_have_value('')
        set_language('Sprache','en_US','Speichern')
        expect(page.get_by_text('Appearance saved.',exact=True)).to_be_visible()
        expect(root).to_have_attribute('lang','en-US')
        login(limited)
        expect(page.get_by_role('combobox',name='Sprache',exact=True)).to_be_disabled()
        expect(page.get_by_role('button',name='Speichern',exact=True)).to_have_count(0)
        current = page.request.get(prefs,headers=auth(limited,'saveAccountAppearance')).json()['data']
        assert current['language']=='de_DE' and current['systemLanguage']=='de_DE'
        assert page.request.put(prefs,headers=auth(limited,'saveAccountAppearance'),data={'revision':current['revision'],'language':'en_US'}).status==403
        assert page.request.put(prefs,headers=auth(key,'saveAccountAppearance'),data={'revision':current['revision'],'language':'../../elsewhere'}).status==422
        # A page route: fill_api_key may already route '**/*' on the page, and page routes win over context routes
        # (the newest page route runs first).
        page.route(origin+'/assets/locales/de.js',lambda route:route.abort())
        login(key)
        expect(root).to_have_attribute('lang','en-US')
        expect(page.get_by_role('combobox',name='Language',exact=True)).to_have_value('de_DE')
        print('Chromium verified real German Account tabs, shell navigation, search, validation and revision failures, '
              'localized confirmation, account isolation, System inheritance and explicit English, read-only permission, and missing-catalogue fallback.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
