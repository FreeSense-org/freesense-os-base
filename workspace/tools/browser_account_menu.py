"""Exercise the real per-account menu, personal start page and scoped login redirects."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
import secrets
from xml.etree import ElementTree as ET

from browser_shell import sign_out, fill_api_key
from playwright.sync_api import expect


def exercise_account_menu(page, origin, key, container, docker, evidence):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    config = ET.fromstring(original)
    ET.SubElement(config.find('system'), 'hostname').text = 'edge'
    ET.SubElement(config.find('system'), 'domain').text = 'lab.test'
    ET.SubElement(config.find('./system/user'), 'descr').text = 'Network Operator'
    readonly = ET.SubElement(config.find('system'), 'user')
    ET.SubElement(readonly, 'name').text = 'limited-operator'
    for name in ['page-system-restapi-keys', 'user-config-readonly']:
        ET.SubElement(readonly, 'priv').text = name
    secret = secrets.token_urlsafe(32)
    token = ET.SubElement(config.find('restapi'), 'apitoken')
    for name, value in [('id','000000000002'),('username','limited-operator'),('hash',sha256(secret.encode()).hexdigest()),('scopes','status:read')]:
        ET.SubElement(token, name).text = value
    restricted_key = 'fsk_000000000002_' + secret
    menu = page.locator('#account-menu')
    toggle = page.locator('#account-toggle')
    settings = menu.get_by_role('link', name='Account settings', exact=True)
    command = menu.get_by_role('button', name='Command palette')
    logout = menu.get_by_role('button', name='Sign out', exact=True)
    preference = page.get_by_role('combobox', name='Start page')
    endpoint = origin + '/api/v1/me/preferences'
    auth = {'Authorization': 'Bearer '+key, 'Origin': origin, 'X-FreeSense-Command': 'saveAccountAppearance'}
    limited = auth | {'Authorization': 'Bearer '+restricted_key}

    def login(path, credential):
        page.goto(origin+path)
        expect(toggle).to_be_hidden()
        fill_api_key(page, credential)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(toggle).to_be_visible()

    def save():
        with page.expect_response(lambda response: response.url == endpoint and response.request.method == 'PUT') as result:
            page.get_by_role('button', name='Save', exact=True).click()
        assert result.value.status == 200
        expect(page.locator('#feedback')).to_contain_text('saved.')

    try:
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(config))
        login('/account/appearance', key)
        expect(page.get_by_role('heading', name='Appearance', exact=True)).to_be_visible()
        assert page.request.get(origin+'/api/v1/session', headers=auth).json()['data'].get('account') is None
        expect(menu.locator('.account-avatar')).to_have_text('NO')
        toggle.focus()
        page.keyboard.press('ArrowDown')
        expect(settings).to_be_focused()
        expect(menu.get_by_text('Signed in as test-operator@edge', exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence/'account-menu-top.png')
        page.keyboard.press('End')
        expect(logout).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(settings).to_be_focused()
        page.keyboard.press('ArrowDown')
        expect(command).to_be_focused()
        page.keyboard.press('Enter')
        search = page.get_by_role('dialog', name='Search menus')
        expect(search.get_by_role('combobox')).to_be_focused()
        expect(settings).to_be_hidden()
        search.get_by_role('combobox').press('Escape')
        expect(toggle).to_be_focused()
        toggle.press('ArrowDown')
        settings.press('Control+k')
        expect(search.get_by_role('combobox')).to_be_focused()
        search.get_by_role('combobox').press('Escape')
        expect(toggle).to_be_focused()
        toggle.click()
        page.locator('#page-title').click()
        expect(settings).to_be_hidden()
        toggle.click()
        settings.click()
        page.get_by_role('link',name='Appearance',exact=True).click()
        expect(page.get_by_role('heading', name='Appearance', exact=True)).to_be_visible()
        page.get_by_role('combobox', name='Menu', exact=True).select_option('side')
        save()
        expect(page.locator('html')).to_have_attribute('data-nav','side')
        page.get_by_role('link',name='Preferences',exact=True).click()
        expect(page).to_have_url(origin+'/account/preferences')
        expect(preference).to_have_value('/')
        choices = preference.locator('option').evaluate_all("items=>items.map(item=>item.value)")
        assert '/firewall' in choices and '/status/gateways' not in choices
        descriptor = page.request.get(origin+'/api/v1/forms', headers=auth).json()['data']['operations']['saveAccountAppearance']
        assert descriptor['fields']['start_page']['choices'] == choices
        start = page.request.get(endpoint,headers=auth).json()['data']
        for invalid in ['/status/gateways','//example.invalid','/firewall?return=1']:
            response = page.request.put(endpoint,headers=auth,data={'revision': start['revision'], 'start_page':invalid})
            assert response.status == 422 and 'start_page' in response.json()['error']['fields']
        preference.select_option('/firewall')
        save()
        assert page.request.put(endpoint,headers=auth,data={'revision':start['revision'],'start_page':'/'}).status == 409
        assert page.request.get(endpoint,headers=auth).json()['data']['nav'] == 'side'
        toggle.click()
        expect(menu.locator('.account-caption')).to_be_visible()
        if evidence:
            page.screenshot(path=evidence/'account-menu-sidebar.png')
        toggle.press('Escape')
        expect(toggle).to_be_focused()
        page.set_viewport_size({'width':320,'height':680})
        toggle.click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if evidence:
            page.screenshot(path=evidence/'account-menu-mobile.png')
        logout.click()
        expect(page.get_by_role('heading',name='Appliance sign-in')).to_be_visible()
        assert 'test-operator' not in page.locator('body').text_content()
        expect(page).to_have_url(origin+'/')
        login('/',key)
        expect(page).to_have_url(origin+'/firewall')
        sign_out(page)
        login('/services/dns',key)
        expect(page).to_have_url(origin+'/services/dns')
        sign_out(page)
        login('/account/preferences',restricted_key)
        expect(preference).to_be_disabled()
        assert preference.locator('option').all_text_contents() == ['Overview']
        assert page.request.put(endpoint,headers=limited,data={'revision':start['revision'],'start_page':'/'}).status == 403
        toggle.click()
        expect(menu.get_by_text('Signed in as limited-operator@edge',exact=True)).to_be_visible()
        assert 'test-operator' not in menu.text_content()
        logout.click()
        login('/',restricted_key)
        expect(page.get_by_role('heading',name='Overview',exact=True)).to_be_visible()
        print('Chromium verified Account menu focus, dismissal, palette, sign-out, personal start pages, direct links, '
              'two accounts, scoped choices and responsive layouts.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
