from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_ping(page, origin, key, container, docker, evidence=None):
    verify_probe(page,origin,key,container,docker,evidence,'ping')


def exercise_traceroute(page, origin, key, container, docker, evidence=None):
    verify_probe(page,origin,key,container,docker,evidence,'traceroute')


def verify_probe(page, origin, key, container, docker, evidence, diagnostic):
    title = diagnostic.capitalize()
    operation = 'run'+title
    grant = 'page-diagnostics-'+diagnostic
    scope = 'diagnostics.'+diagnostic
    maximum = ('count',10) if diagnostic == 'ping' else ('hops',64)
    result_display = 'Native result fixture: '+('1 packets received.' if diagnostic == 'ping' else '1 2001:db8:1::2')
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    route = '/diagnostics/'+diagnostic
    endpoint = origin + '/api/v1' + route
    auth = {'Authorization': 'Bearer ' + key}
    write = auth | {'Origin':origin, 'X-FreeSense-Command':operation}
    entered = {'host':'::1', 'family':'ipv4', 'source':'auto'}
    entered |= {'count':3,'interval':1} if diagnostic == 'ping' else {'hops':18,'resolve':False,'icmp':False}

    def configure(grant, scope):
        config = ET.fromstring(original)
        user = config.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for item in ['page-system-restapi-keys',grant]:
            ET.SubElement(user,'priv').text = item
        config.find('./restapi/apitoken/scopes').text = scope
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(config))

    try:
        # Containers have no FreeBSD ifconfig. Supply a synthetic snapshot in this isolated fixture.
        docker('exec','-i',container,'sh','-c','cat > /sbin/ifconfig && chmod 0555 /sbin/ifconfig',
               input=b'#!/bin/sh\n' + b'printf "em0: flags=8843<UP,BROADCAST,RUNNING,MULTICAST> mtu 1500\\n inet 192.0.2.1/24\\n inet6 2001:db8:1::1/64\\n"\n')
        configure('page-diagnostics-routingtables','status:read')
        assert page.request.get(endpoint,headers=auth).status == 403
        assert page.request.put(endpoint,headers=write,data=entered).status == 403
        configure(grant,scope+':read')
        assert page.request.put(endpoint,headers=write,data=entered).status == 403
        configure(grant,scope+':write')
        page.goto(origin + route)
        fill_api_key(page, key)
        page.get_by_role('button',name='Sign in',exact=True).click()
        descriptor = page.request.get(origin+'/api/v1/forms',headers=auth).json()['data']['operations'][operation]
        assert descriptor['path'] == '/api/v1'+route and descriptor['fields'][maximum[0]]['max'] == maximum[1]
        host = page.get_by_role('textbox',name='Hostname or IP address')
        submit = page.get_by_role('button',name=title,exact=True)
        assert page.request.put(endpoint,headers=write,data=entered).status == 422
        wrong = entered | {'host':'192.0.2.2','source':'lan/192.0.2.111'}
        failed = page.request.put(endpoint,headers=write,data=wrong)
        assert failed.status == 422 and 'source' in failed.json()['error']['fields'], failed.text()
        requests = []
        def completed(request):
            requests.append(request.request.post_data_json)
            request.fulfill(json={'data':{'ok':True,'timed_out':False,'output':result_display}})
        page.route(endpoint, lambda request: completed(request) if request.request.method == 'PUT' else request.continue_())
        host.fill('::1')
        submit.click()
        expect(host).to_have_attribute('aria-invalid','true')
        assert not requests
        page.get_by_role('combobox',name='IP protocol').select_option('ipv6')
        submit.click()
        expect(page.get_by_text(result_display)).to_be_visible()
        assert requests == [entered | {'family':'ipv6'}]
        if evidence:
            page.screenshot(path=evidence/('diagnostics-'+diagnostic+'.png'),full_page=True)
        page.goto(origin + route + '?host=2001%3Adb8%3A%3A2')
        fill_api_key(page, key)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(host).to_have_value('2001:db8::2')
        expect(page.get_by_role('combobox',name='IP protocol')).to_have_value('ipv6')
        assert len(requests) == 1, 'Following a lookup link must wait for explicit submission'
        print(f'Chromium verified {title} grants, direct validation, AJAX contract and result presentation.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
