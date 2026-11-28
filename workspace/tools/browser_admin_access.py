"""Exercise the two local Admin Access settings against the actual PHP and syslog paths."""
import hashlib
import re
import secrets
import time
from xml.etree import ElementTree as ET
from browser_shell import menu_link, fill_api_key, nav_link
from playwright.sync_api import expect


def exercise_admin_access(page, origin, key, container, docker, evidence):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    doc = ET.fromstring(original)
    system = doc.find('system')
    ET.SubElement(system, 'hostname').text = 'edge'
    ET.SubElement(system, 'domain').text = 'office.test'
    ET.SubElement(system.find('user'), 'priv').text = 'page-system-advanced-admin'
    token = doc.find('./restapi/apitoken')
    token.find('scopes').text += ' system.settings:write'
    guest = ET.SubElement(system, 'user')
    ET.SubElement(guest, 'name').text = 'read-operator'
    for grant in ['page-system-advanced-admin', 'page-system-restapi-keys']:
        ET.SubElement(guest, 'priv').text = grant
    ET.SubElement(guest, 'customsettings')
    secret = secrets.token_urlsafe(32)
    limited = ET.SubElement(doc.find('restapi'), 'apitoken')
    for name, value in [('username','read-operator'), ('id','000000000002'), ('scopes','system.settings:read'),
                        ('hash',hashlib.sha256(secret.encode()).hexdigest())]:
        ET.SubElement(limited, name).text = value
    readonly = 'fsk_000000000002_' + secret
    url = origin + '/api/v1/system/advanced/admin'
    signin = origin + '/api/v1/session/sign-in'
    page_url = origin + '/system/advanced/admin'
    def headers(token, command=None):
        result = {'Authorization':'Bearer ' + token, 'Origin':origin}
        if command: result['X-FreeSense-Command'] = command
        return result
    def full(state, **over):
        # Admin Access saves the whole form (every field is required); a stale or partial edit starts from a read.
        fields = page.request.get(origin + '/api/v1/forms', headers=headers(key)).json()['data']['operations']['saveAdminAccess']['fields']
        values = state.get('values', state)
        return {name: values[name] for name in fields if name in values} | {'revision': state['revision']} | over
    def write(token, body):
        return page.request.put(url, headers=headers(token, 'saveAdminAccess'), data=body)
    def login(token):
        page.goto(page_url)
        expect(page.get_by_label('API key', exact=True)).to_have_attribute('autocomplete', 'off')
        fill_api_key(page, token)
        page.get_by_role('button', name='Sign in').click()
        expect(page.get_by_text('webConfigurator', exact=True).first).to_be_visible()  # panel title (legend or summary), or the read-only heading
    def screenshot(name):
        if evidence: page.screenshot(path=evidence/name, full_page=True)
    # Capture the real libc syslog datagram in the network-isolated disposable test container.
    collector = """
if (file_exists('/dev/log')) exit(1);
$s=stream_socket_server('udg:///dev/log',$n,$e,STREAM_SERVER_BIND);
if (!$s) exit(1);
touch('/check/log-ready');
while (true) {
    $msg=stream_socket_recvfrom($s,16384);
    if (!is_string($msg)) exit(1);
    if (str_contains($msg,'Successful login')) file_put_contents('/check/login-events',$msg."\\n",FILE_APPEND);
}
"""
    try:
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))
        docker('exec','-d',container,'php','-r',collector)
        docker('exec',container,'sh','-ec','i=0; while [ ! -f /check/log-ready ] && [ "$i" -lt 50 ]; do sleep .1; i=$((i+1)); done; test -S /dev/log')
        public = page.request.get(page_url).text()
        assert 'edge.office.test' not in public and 'data-local-sign-in' in public
        page.set_extra_http_headers({'X-Forwarded-For': '198.51.100.17'})
        login(key)
        expect(page).to_have_title('edge.office.test - Admin Access')
        original_state = page.request.get(url, headers=headers(key)).json()['data']
        level = page.get_by_label('Lower syslog level for successful GUI login events')
        title = page.get_by_label('Display page name first in browser tab')
        expect(level).not_to_be_checked()
        expect(title).not_to_be_checked()
        level.check(); title.check()
        page.get_by_role('button', name='Save',exact=True).click()
        expect(page.get_by_text('Admin Access settings saved.', exact=True)).to_be_visible()
        expect(page).to_have_title('Admin Access - edge.office.test')
        screenshot('webconfigurator.png')
        stale = write(key, full(original_state, **{'webgui-login-messages': False}))
        assert stale.status == 409, (stale.status, stale.text())
        assert write(key, full(original_state, enablesshd=True)).status in (409, 422)
        nav_link(page, '/packages/node-exporter').click()
        expect(page).to_have_title('Prometheus node exporter - edge.office.test')
        page.go_back()
        expect(page).to_have_title('Admin Access - edge.office.test')
        expect(menu_link(page,'System','Advanced')).to_be_visible()  # Admin Access is a tab of System > Advanced
        login(readonly)
        expect(page).to_have_title('edge.office.test - Admin Access')
        expect(page.get_by_text('Your key does not allow changing the admin access settings.',exact=True)).to_be_visible()
        expect(page.get_by_role('button',name='Save',exact=True)).to_have_count(0)
        screenshot('read-only.png')
        current = page.request.get(url,headers=headers(readonly)).json()['data']
        assert write(readonly,{'revision':current['revision'],'webgui-login-messages':False}).status == 403
        assert page.request.post(signin,headers=headers(key),data={}).status == 400
        assert page.request.post(signin,headers={**headers(key,'signIn'),'Origin':'http://localhost'},data={}).status == 403
        assert page.request.post(signin,headers=headers(key,'signIn'),data={'username':'different-owner'}).status == 422
        assert page.request.post(signin,headers=headers('invalid','signIn'),data={}).status == 401
        assert page.request.get(signin,headers=headers(key)).status == 405
        assert page.request.get(origin+'/api/v1/session',headers=headers(key)).status == 200
        reset = write(key, full(current, **{'webgui-login-messages': False, 'pagenamefirst': False}))
        assert reset.status == 200 and reset.json()['data']['revision'] == original_state['revision']
        login(key)
        expect(page).to_have_title('edge.office.test - Admin Access')
        expect(level).not_to_be_checked()
        for _ in range(20):
            events = docker('exec',container,'cat','/check/login-events').decode().splitlines()
            if len(events) >= 3: break
            time.sleep(.1)
        for secret in [key,readonly]:
            assert all(secret not in line and secret.split('_',2)[-1] not in line for line in events)
        expected = [(32,'test-operator'),(37,'read-operator'),(32,'test-operator')]
        assert len(events) == len(expected)
        for line, (priority,owner) in zip(events,expected):
            assert re.match(r'^<' + str(priority) + r'>',line)
            assert "Successful login for user '" + owner + "' from: 127.0.0.1" in line
            assert '198.51.100.17' not in line
        if evidence:
            (evidence/'audit.txt').write_text('Observed local auth syslog priorities in order: 32, 37, 32. Two confirmed settings changes. Exactly three successful explicit browser sign-ins logged.\n', encoding='utf-8')
        print('Chromium verified login logging at auth.emerg / auth.notice, actual tab order on System and Optional pages, individual override, rollback, scope and registered AJAX validation.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
