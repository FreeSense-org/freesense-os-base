"""Exercise menu search with the real router, package registry and two differently scoped operators."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from hashlib import sha256
import secrets
from xml.etree import ElementTree as ET

from browser_shell import sign_out, fill_api_key
from playwright.sync_api import expect


def exercise_menu_search(page, origin, key, container, docker, evidence):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    config = ET.fromstring(original)
    reader = ET.SubElement(config.find('system'), 'user')
    ET.SubElement(reader, 'name').text = 'menu-reader'
    for grant in ['page-system-restapi-keys', 'user-config-readonly']:
        ET.SubElement(reader, 'priv').text = grant
    secret = secrets.token_urlsafe(32)
    token = ET.SubElement(config.find('restapi'), 'apitoken')
    for field, value in [('username', 'menu-reader'), ('id', '000000000002'), ('scopes', 'status:read'),
                         ('hash', sha256(secret.encode()).hexdigest())]:
        ET.SubElement(token, field).text = value

    trigger = page.get_by_role('button', name='Search menus', exact=True)
    dialog = page.get_by_role('dialog', name='Search menus')
    search = dialog.get_by_role('combobox')
    options = dialog.get_by_role('option')
    empty = dialog.get_by_text('No matching pages.', exact=True)
    signed_out = page.get_by_role('heading', name='Appliance sign-in')
    history = []
    page.on('request', lambda request: history.append(request.url) if request.url.startswith(origin) else None)

    def sign_in(value):
        fill_api_key(page, value)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('heading', name='Appearance', exact=True)).to_be_visible()
        expect(trigger).to_be_visible()

    def labels():
        return options.evaluate_all('nodes => nodes.map(n => n.firstChild.textContent)')

    def capture(name):
        if evidence:
            page.screenshot(path=evidence/name)

    try:
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(config))
        page.goto(origin+'/account/appearance')
        expect(signed_out).to_be_visible()
        expect(trigger).to_be_hidden()
        page.keyboard.press('Control+k')
        expect(dialog).to_be_hidden()
        sign_in(key)

        trigger.focus()
        history.clear()
        page.keyboard.press('Control+k')
        expect(search).to_be_focused()
        expect(search).to_have_value('')
        # The palette lists the pages the operator may open plus the greyed entries that are not ported yet (shown, not
        # activatable), in menu order. Links that carry a query or fragment (one page per interface) are reached from their
        # page, not from the palette.
        allowed = page.locator('#appliance-menu a[data-page], #appliance-menu a[data-unported]').evaluate_all(
            'links => links.filter(link => !link.closest("[hidden]") && (link.hasAttribute("data-unported") || (!link.search && !link.hash)))'
            '.map(link => link.textContent.trim())')
        allowed.append(page.locator('#account-appearance-link').text_content().strip())
        assert labels() == allowed[:50], (labels(), allowed[:50])
        assert 'Rules' in allowed and 'Admin Access' not in allowed
        search.press('ArrowUp')
        expect(options.last).to_have_attribute('aria-selected', 'true')
        search.press('ArrowDown')
        expect(options.first).to_have_attribute('aria-selected', 'true')
        assert search.get_attribute('aria-activedescendant') == options.first.get_attribute('id')
        search.fill('FiReWaLl')
        assert 'Aliases' in labels() and 'Rules' in labels()
        search.fill('Firewall')
        capture('menu-search-top.png')
        search.fill('services  TfTp')
        assert labels() and all('tftp' in item.lower() for item in labels())
        search.fill('no page matches this query')
        expect(empty).to_be_visible()
        expect(options).to_have_count(0)
        assert search.get_attribute('aria-activedescendant') is None
        search.press('Enter')
        expect(dialog).to_be_visible()
        search.press('Escape')
        expect(dialog).to_be_hidden()
        expect(trigger).to_be_focused()
        assert not history, 'Typing, moving selection or cancelling issued a request'

        page.keyboard.press('Meta+k')
        expect(search).to_have_value('')
        search.fill('aliases')
        search.press('Enter')
        expect(page).to_have_url(origin+'/firewall/aliases')
        expect(dialog).to_be_hidden()
        expect(page.locator('#page-title')).to_be_focused()
        page.go_back()
        expect(page.get_by_role('heading', name='Appearance', exact=True)).to_be_visible()
        page.get_by_role('combobox', name='Menu', exact=True).select_option('side')
        page.get_by_role('button', name='Save', exact=True).click()
        expect(page.locator('html')).to_have_attribute('data-nav', 'side')
        expect(page.get_by_text('Appearance saved.', exact=True)).to_be_visible()
        trigger.click()
        expect(search).to_be_focused()
        assert labels() == allowed[:50]
        search.fill('TFTP')
        package = page.locator('#appliance-menu a[href="/packages/tftp-settings"]')
        package_title = package.text_content().strip()
        assert package_title in labels()
        capture('menu-search-sidebar.png')
        # If a live menu changes before activation, an old search result cannot navigate.
        package.evaluate('(link) => {link.hidden = true;}')
        options.filter(has_text=package_title).click()
        expect(dialog).to_be_visible()
        assert package_title not in labels()
        package.evaluate('(link) => {link.hidden = false;}')
        search.fill('TFTP Server')
        options.filter(has_text=package_title).click()
        expect(page).to_have_url(origin+'/packages/tftp-settings')
        expect(page.get_by_role('heading', name='TFTP listener', exact=True)).to_be_visible()

        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.get_by_role('navigation', name='Appliance', exact=True)).to_be_hidden()
        trigger.click()
        search.fill('services')
        assert package_title in labels()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        capture('menu-search-mobile.png')
        page.set_viewport_size({'width': 320, 'height': 660})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        dialog.get_by_role('button', name='Close search').click()
        expect(trigger).to_be_focused()
        sign_out(page)
        expect(signed_out).to_be_visible()
        expect(trigger).to_be_hidden()
        assert page.locator('#menu-search-results').inner_text() == ''
        page.goto(origin+'/account/appearance')
        sign_in('fsk_000000000002_' + secret)
        trigger.click()
        # Greyed entries (not ported yet) are listed for every operator; the pages this reader may open are only these.
        active = options.evaluate_all('nodes => nodes.filter(n => n.getAttribute("aria-disabled") !== "true").map(n => n.firstChild.textContent)')
        assert set(active) == {'Overview','REST API','Account settings'}, active
        assert page.locator('#menu-system-rest-api').get_attribute('href') == '/system/rest-api/my-keys'
        # Nothing this reader may not open can be activated: at most greyed (not ported) entries match.
        def nothing_active(query):
            search.fill(query)
            page.wait_for_timeout(100)
            assert options.evaluate_all('nodes => nodes.filter(n => n.getAttribute("aria-disabled") !== "true").length') == 0, query
        nothing_active('TFTP')
        nothing_active('firewall')
        search.fill('/system/advanced/admin')
        expect(empty).to_be_visible()
        search.press('Enter')
        expect(page).to_have_url(origin+'/account/appearance')
        print('Chromium verified authorized System and Optional menu search, stable ranking, both shortcuts, '
              'keyboard and pointer navigation, mobile, both layouts and isolation across sign-in.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
