"""Verify the same authorized menu in both Account Appearance layouts."""
from hashlib import sha256
import secrets
from xml.etree import ElementTree as ET

from playwright.sync_api import expect
from browser_shell import sign_out, fill_api_key


def exercise_appearance(page, origin, key, container, docker, evidence):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    doc = ET.fromstring(original)
    system = doc.find('system')
    hostname = system.find('hostname')
    if hostname is None: hostname = ET.SubElement(system,'hostname')
    hostname.text = 'east-edge'
    domain = system.find('domain')
    if domain is None: domain = ET.SubElement(system,'domain')
    domain.text = 'ops.example.test'
    fqdn = hostname.text+'.'+domain.text
    for grant in ('page-dashboard-all','page-status-interfaces','page-diagnostics-configurationhistory',
                  'page-services-tftpd','page-system-advanced-admin','page-system-gateways'):
        ET.SubElement(system.find('user'),'priv').text = grant
    doc.find('./restapi/apitoken/scopes').text = 'status:read,config:read,packages.tftp:read,system.settings:read,routing:read'
    other = ET.SubElement(system,'user')
    ET.SubElement(other,'name').text = 'appearance-reader'
    for grant in ('page-system-restapi-keys','user-config-readonly'):
        ET.SubElement(other,'priv').text = grant
    secret = secrets.token_urlsafe(32)
    limited = ET.SubElement(doc.find('restapi'),'apitoken')
    for field,value in [('username','appearance-reader'),('id','000000000002'),('scopes','status:read'),
                        ('hash',sha256(secret.encode()).hexdigest())]:
        ET.SubElement(limited,field).text = value
    read_key = 'fsk_000000000002_' + secret
    endpoint = origin+'/api/v1/me/preferences'
    auth = {'Authorization':'Bearer '+key,'Origin':origin,'X-FreeSense-Command':'saveAccountAppearance'}
    nav = page.get_by_role('navigation',name='Appliance',exact=True)
    shell = page.locator('html')
    help_title = nav.locator('details:has(#context-help) > summary')
    context_help = page.locator('#context-help')

    def sign_in(token):
        page.goto(origin+'/account/appearance')
        fill_api_key(page, token)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.get_by_role('heading',name='Appearance',exact=True)).to_be_visible()

    def save():
        save_button = page.get_by_role('button',name='Save',exact=True)
        with page.expect_response(lambda response:response.url==endpoint and response.request.method=='PUT') as request:
            save_button.click()
        assert request.value.status == 200
        expect(page.get_by_text('Appearance saved.',exact=True)).to_be_visible()
        assert page.url == origin+'/account/appearance'
        expect(save_button).to_be_focused()

    def select(label,value):
        page.get_by_role('combobox',name=label,exact=True).select_option(value)

    def available():
        return nav.locator('a:not([hidden])').evaluate_all('links=>links.map(link=>link.pathname).sort()')

    def screenshot(name):
        if evidence:
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=evidence/name,full_page=True)

    try:
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        saved_xml=docker('exec',container,*cli,'backup')
        assert page.request.get(endpoint).status==401
        page.emulate_media(color_scheme='light')
        sign_in(key)
        expect(shell).to_have_attribute('data-nav','top')
        expect(help_title).to_have_text('Help')
        expect(context_help).to_have_attribute('href','https://docs.freesense.org/2.0/reference/menu-guide/')
        help_title.click()
        expect(nav.get_by_role('link',name='About this Page')).to_be_visible()
        links = help_title.locator('..').locator('a')
        assert links.count()==6 and all(link.get_attribute('rel')=='noopener noreferrer' and
            link.get_attribute('target')=='_blank' and link.get_attribute('data-page') is None for link in links.all())
        external = []
        def mock_guide(route):
            external.append(route.request.all_headers())
            route.fulfill(content_type='text/html',body='<title>Documentation guide</title>')
        page.context.route('https://docs.freesense.org/**',mock_guide)
        with page.context.expect_page() as popup:
            context_help.click()
        guide = popup.value
        guide.wait_for_load_state()
        assert guide.title()=='Documentation guide' and guide.evaluate('window.opener === null')
        assert len(external)==1 and not {'referer','authorization','cookie'} & external[0].keys()
        guide.close()
        page.context.unroute('https://docs.freesense.org/**',mock_guide)
        help_title.click()
        # The account mode (default Dark) decides the colours, not the browser preference: Bootstrap 6 sets color-scheme from
        # data-bs-theme, so the light-dark() tokens follow the account. (Under Bootstrap 5.3.8 a light browser leaked through.)
        background = shell.evaluate('item=>getComputedStyle(item).backgroundColor')
        assert background=='rgb(20, 24, 31)', background
        page.emulate_media(color_scheme='dark')
        background = shell.evaluate('item=>getComputedStyle(item).backgroundColor')
        assert background=='rgb(20, 24, 31)', background
        baseline=page.request.get(endpoint,headers=auth).json()['data']
        fetch_saved=lambda:page.request.get(endpoint,headers=auth).json()['data']
        authorized_paths=available()
        assert '/packages/tftp-settings' in authorized_paths and '/system/advanced/admin' in authorized_paths
        for label,value in [('Mode','dark'),('Accent colour','amber'),('Density','compact'),
                            ('Top menu bar','fixed'),('Top bar menu','auto'),('Hostname in menu','hostonly')]:
            select(label,value)
        expect(shell).to_have_attribute('data-accent','amber')
        expect(shell).to_have_attribute('data-density','compact')
        assert fetch_saved()['accent']==baseline['accent']
        save()
        expect(shell).to_have_attribute('data-accent','amber')
        select('Accent colour','green')
        expect(shell).to_have_attribute('data-accent','green')
        page.get_by_role('button',name='Cancel',exact=True).click()
        expect(shell).to_have_attribute('data-accent','amber')
        expect(help_title).to_have_text('east-edge')
        expect(help_title).to_have_attribute('title','east-edge')
        assert page.locator('.mast').evaluate('item=>getComputedStyle(item).position')=='sticky'
        screenshot('top-bar.png')
        help_title.click()
        screenshot('hostname-help.png')
        help_title.click()
        select('Menu','side')
        select('Page width','wide')
        select('Hostname in menu','fqdn')
        expect(page.get_by_role('combobox',name='Top menu bar',exact=True)).to_be_hidden()
        save()
        expect(shell).to_have_attribute('data-nav','side')
        expect(help_title).to_have_text(fqdn)
        assert available()==authorized_paths
        assert nav.evaluate('item=>getComputedStyle(item).position')=='fixed'
        assert page.request.put(endpoint,headers=auth,data={'revision':baseline['revision'],'nav':'top'}).status==409
        screenshot('sidebar.png')
        system_group=nav.locator('summary',has_text='System')
        system_group.focus()
        page.keyboard.press('Enter')
        # The 1.2 menu: System > Advanced opens Admin Access, its first tab (the entry is named Advanced, not Admin Access).
        admin=nav.get_by_role('link',name='Advanced',exact=True)
        expect(admin).to_be_visible()
        admin.focus()
        page.keyboard.press('Escape')
        expect(system_group).to_be_focused()
        expect(admin).to_be_hidden()
        system_group.click()
        admin.click()
        expect(page.get_by_role('heading',name='Admin Access',exact=True)).to_be_visible()
        expect(admin).to_have_attribute('aria-current','page')
        expect(context_help).to_have_attribute('href','https://docs.freesense.org/2.0/base-os/system-administration/')
        page.locator('#account-toggle').click()
        page.get_by_role('link',name='Account settings',exact=True).click()
        page.get_by_role('link',name='Appearance',exact=True).click()
        expect(page.get_by_role('heading',name='Appearance',exact=True)).to_be_visible()
        page.set_viewport_size({'width':390,'height':844})
        mobile=page.get_by_role('button',name='Menu',exact=True)
        expect(nav).to_be_hidden()
        mobile.click()
        expect(nav).to_be_visible()
        expect(mobile).to_have_attribute('aria-expanded','true')
        expect(help_title).to_be_visible()
        expect(help_title.locator('span')).to_have_text(fqdn)
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        screenshot('mobile.png')
        page.keyboard.press('Escape')
        expect(mobile).to_be_focused()
        expect(nav).to_be_hidden()
        page.set_viewport_size({'width':1320,'height':920})
        sign_in(key)
        expect(shell).to_have_attribute('data-nav','side')
        expect(page.get_by_role('combobox',name='Menu',exact=True)).to_have_value('side')
        expect(page.get_by_role('combobox',name='Hostname in menu',exact=True)).to_have_value('fqdn')
        expect(help_title).to_have_text(fqdn)
        select('Menu','top')
        expect(page.get_by_role('combobox',name='Top menu bar',exact=True)).to_have_value('fixed')
        expect(page.get_by_role('combobox',name='Top bar menu',exact=True)).to_have_value('auto')
        save()
        assert docker('exec',container,*cli,'backup')==saved_xml
        sign_in(read_key)
        expect(help_title).to_have_text('Help')
        expect(page.get_by_role('combobox',name='Menu',exact=True)).to_have_value('top')
        expect(page.get_by_role('combobox',name='Menu',exact=True)).to_be_disabled()
        assert '/packages/tftp-settings' not in available()
        assert page.get_by_role('button',name='Save',exact=True).count()==0
        restricted=auth|{'Authorization':'Bearer '+read_key}
        current=page.request.get(endpoint,headers=restricted).json()['data']
        assert page.request.put(endpoint,headers=restricted,data={'revision':current['revision'],'nav':'side'}).status==403
        hostname.text = 'x'*63
        domain.text = '.'.join(['y'*60]*3)
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        sign_in(key)
        expect(help_title).to_have_attribute('title',hostname.text+'.'+domain.text)
        page.set_viewport_size({'width':390,'height':844})
        page.get_by_role('button',name='Menu',exact=True).click()
        expect(help_title).to_be_visible()
        assert help_title.locator('span').evaluate('item=>item.scrollWidth>item.clientWidth')
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        hostname.text = 'invalid/name'
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        sign_in(key)
        page.get_by_role('button',name='Menu',exact=True).click()
        expect(help_title).to_have_text('Help')
        expect(page.get_by_role('combobox',name='Hostname in menu',exact=True)).to_have_value('fqdn')
        sign_out(page)
        expect(page.locator('#context-help')).to_be_hidden()
        # Signing out clears the menu, so no host name stays behind in a Help caption.
        expect(page.locator('details:has(#context-help) > summary')).to_have_count(0)
        assert 'east-edge' not in page.locator('#appliance-menu').inner_html()
        print('Chromium verified both menu layouts, local Help hostname/FQDN and fallback, private external navigation, '
              'keyboard, mobile, appearance isolation, stale edits and package permissions.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
