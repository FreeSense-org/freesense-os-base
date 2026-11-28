from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_open_vm_tools(page, origin, key, container, docker, evidence=None):
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    endpoint = origin + '/api/v1/packages/open-vm-tools'
    auth = {'Authorization':'Bearer ' + key}
    route = '/status/open-vm-tools'

    def install(path, body):
        docker('exec', '-i', container, 'sh', '-c', 'cat > "$1" && chmod 0555 "$1"', 'sh', path, input=body)

    def configure(grant, scope):
        doc = ET.fromstring(original)
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for privilege in ['page-system-restapi-keys', grant]:
            ET.SubElement(user, 'priv').text = privilege
        doc.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ET.tostring(doc))

    try:
        docker('exec', container, 'cp', '-R', '/optional/emulators/FreeSense-pkg-Open-VM-Tools/files/usr/local/.', '/check/prefix/')
        docker('exec', container, 'mkdir', '-p', '/usr/local/etc/rc.d')
        install('/usr/local/bin/vmware-checkvm', b'#!/bin/sh\nexit 0\n')
        install('/usr/local/etc/rc.d/vmware-guestd', b'#!/bin/sh\ncase "$1" in onestatus|enabled) exit 0;; *) exit 99;; esac\n')
        configure('page-diagnostics-dns', 'packages.open-vm-tools:read')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-status-services', 'status:read')
        assert page.request.get(endpoint, headers=auth).status == 403
        configure('page-status-services', 'packages.open-vm-tools:read')
        response = page.request.get(endpoint, headers=auth)
        assert response.ok, response.text()
        assert response.json()['data']['values']['state'] == 'running'
        assert response.json()['data']['operations'] == []
        assert page.request.put(endpoint, headers=auth, data={}).status == 405
        page.goto(origin + route)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_role('heading', name='VMware Guest Tools')).to_be_visible()
        expect(page.get_by_text('Running', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Start', exact=True)).to_have_count(0)
        if evidence:
            page.screenshot(path=evidence/'open-vm-tools-status.png', full_page=True)
        install('/usr/local/etc/rc.d/vmware-guestd', b'#!/bin/sh\ncase "$1" in onestatus) exit 1;; enabled) exit 0;; *) exit 99;; esac\n')
        assert page.request.get(endpoint, headers=auth).json()['data']['values']['state'] == 'stopped'
        install('/usr/local/etc/rc.d/vmware-guestd', b'#!/bin/sh\nexit 1\n')
        assert page.request.get(endpoint, headers=auth).json()['data']['values']['state'] == 'disabled'
        install('/usr/local/bin/vmware-checkvm', b'#!/bin/sh\nexit 1\n')
        assert page.request.get(endpoint, headers=auth).json()['data']['values']['state'] == 'not_vmware'
        install('/usr/local/bin/vmware-checkvm', b'#!/bin/sh\nexit 127\n')
        assert page.request.get(endpoint, headers=auth).json()['data']['values']['state'] == 'unavailable'
        # In this Linux-only disposable browser container, translate the stock FreeBSD timeout flag.
        docker('exec', container, 'mv', '/usr/bin/timeout', '/usr/bin/timeout-fixture-real')
        install('/usr/bin/timeout', b'#!/bin/sh\nif [ "$1" = -f ]; then shift; set -- --foreground "$@"; fi\nexec /usr/bin/timeout-fixture-real "$@"\n')
        install('/usr/local/bin/vmware-checkvm', b'#!/bin/sh\nexit 0\n')
        install('/usr/local/etc/rc.d/vmware-guestd', b'''#!/bin/sh
case "$1" in
enabled) exit 0 ;;
onestatus) test -f /tmp/open-vm-tools-running ;;
start|restart) touch /tmp/open-vm-tools-running ;;
stop) rm -f /tmp/open-vm-tools-running ;;
*) exit 99 ;;
esac
''')
        docker('exec', container, 'touch', '/tmp/open-vm-tools-running')
        read_command = auth | {'Origin':origin, 'X-FreeSense-Command':'package.open-vm-tools.stop'}
        assert page.request.put(endpoint + '/stop', headers=read_command,
                                data={'revision':response.json()['data']['values']['revision']}).status == 403
        configure('page-status-services', 'packages.open-vm-tools:write')
        page.reload()
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in', exact=True).click()
        stop = page.get_by_role('button', name='Stop', exact=True)
        expect(stop).to_be_visible()
        page.once('dialog', lambda dialog: dialog.dismiss())
        stop.click()
        expect(page.get_by_text('Running', exact=True)).to_be_visible()
        for action, state in [('stop','Stopped'), ('start','Running'), ('restart','Running')]:
            if action != 'start':
                page.once('dialog', lambda dialog: dialog.accept())
            with page.expect_response(lambda reply: reply.url == endpoint + '/' + action and reply.request.method == 'PUT') as saved:
                page.get_by_role('button', name=action.title(), exact=True).click()
            assert saved.value.ok, saved.value.text()
            expect(page.get_by_text(state, exact=True)).to_be_visible()
        print('Chromium verified VMware status, grants, read-only refusal and confirmed service actions.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
