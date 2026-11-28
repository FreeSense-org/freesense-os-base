"""Interact with the shared navigation in either of its responsive layouts."""

import browser_select  # noqa: F401  installs the enhanced-dropdown support for every script



def open_menu(page, group):
    navigation = page.get_by_role('navigation', name='Appliance', exact=True)
    mobile = page.get_by_role('button', name='Menu', exact=True)
    if mobile.is_visible() and mobile.get_attribute('aria-expanded') != 'true':
        mobile.click()
    summary = navigation.locator('summary').get_by_text(group, exact=True)
    disclosure = summary.locator('xpath=ancestor::details')
    if disclosure.get_attribute('open') is None:
        summary.click()
    return disclosure


def close_menu(page):
    """Close an open menu panel the way a keyboard operator does: Escape from inside it. The top layout's panel covers the page."""
    summary = page.get_by_role('navigation', name='Appliance', exact=True).locator('details[open] > summary')
    if summary.count():
        summary.first.focus()
        page.keyboard.press('Escape')


def _legacy_paths():
    """Page title -> path for the pages the shell still reaches through the legacy adapter table."""
    import re
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / 'freesense/src/usr/local/www/assets/shell/legacy-pages.js'
    titles = {}
    if source.is_file():
        for path, title in re.findall(r"\['(/[^']+)', '([^']+)'\]", source.read_text(encoding='utf-8')):
            titles.setdefault(title, path)
    return titles


def menu_link(page, group, label):
    """A menu link by its name.

    The old-menu rebuild gives entries plain labels and folds pages such as 'WAN IPv4' under their parent entry, as the
    1.2 menu did. A legacy 'Group: Label' name falls back to the plain label; a page without an entry of its own is
    reached the way the shell reaches any page, through an in-page navigation link, which the caller clicks.
    """
    menu = open_menu(page, group)
    exact = menu.get_by_role('link', name=label, exact=True)
    if exact.count():
        return exact
    plain = label.split(': ', 1)[-1]
    parts = label.split(': ')
    # 'Status: System Logs: Firewall': the menu now has one 'System Logs' entry (its tabs hold the rest); it opens the first
    # log page the account may read.
    nested = [parts[1]] if len(parts) > 2 else []
    for name in (plain, f'{plain} ({group})', *nested):
        # A package page offered in two sections is labelled 'Title (Section)' to tell the entries apart.
        if name != label and menu.get_by_role('link', name=name, exact=True).count():
            return menu.get_by_role('link', name=name, exact=True)
    path = _legacy_paths().get(plain)
    if path is None:
        return exact
    return nav_link(page, path)


def nav_link(page, path):
    """A link to a page the menu does not offer (for example a package page), reached through the shell's own in-page navigation."""
    page.evaluate(
        """(target) => {
            document.querySelectorAll('a[data-test-nav]').forEach(node => node.remove());
            const link = document.createElement('a');
            link.href = target; link.dataset.page = ''; link.dataset.testNav = ''; link.textContent = 'test navigation';
            link.style.position = 'fixed'; link.style.left = '0'; link.style.top = '0'; link.style.zIndex = '99999';
            document.body.append(link);
        }""", path)
    return page.locator('a[data-test-nav]')


def sign_out(page):
    toggle = page.locator('#account-toggle')
    if toggle.is_visible() and toggle.get_attribute('aria-expanded') != 'true':
        toggle.click()
    page.get_by_role('button', name='Sign out', exact=True).click()


def fill_api_key(page, key):
    """Fill the API key field, switching the sign-in page from the password form first when it is showing."""
    # The sign-in fieldsets stay disabled until the page script has started; on a busy machine that takes a while.
    page.wait_for_function("() => [...document.querySelectorAll('#sign-in fieldset[data-unlock]')].some(f => !f.disabled)", timeout=60000)
    toggle = page.get_by_role('button', name='Use an API key', exact=True)
    field = page.get_by_label('API key', exact=True)
    if not toggle.is_visible() and not field.is_visible() and page.locator('#credentials[data-local-sign-in]').count():
        # The local sign-in page offers an API key only when Admin Access allows it (default off). These harnesses sign in
        # with a key, so serve the page as if that setting were on: mark the shell and reload.
        page.route('**/*', _mark_key_sign_in)
        page.reload()
        page.wait_for_function("() => [...document.querySelectorAll('#sign-in fieldset[data-unlock]')].some(f => !f.disabled)", timeout=60000)
        toggle = page.get_by_role('button', name='Use an API key', exact=True)
    if toggle.is_visible():
        toggle.click()
    field.fill(key, timeout=60000)


def _mark_key_sign_in(route):
    if route.request.resource_type != 'document':
        # fallback(), not continue_(): routes a test registered earlier (mocked API answers) must still apply.
        route.fallback()
        return
    response = route.fetch()
    body = response.text() if 'text/html' in response.headers.get('content-type', '') else None
    if body is None or 'data-local-sign-in' not in body:
        route.fulfill(response=response)
        return
    headers = {name: value for name, value in response.headers.items() if name.lower() != 'content-length'}
    route.fulfill(response=response, headers=headers, body=body.replace('data-local-sign-in', 'data-local-sign-in data-key-sign-in', 1))
