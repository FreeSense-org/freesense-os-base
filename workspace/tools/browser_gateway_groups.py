"""Exercise the draft gateway-group workflow on the routed local shell."""
from xml.etree import ElementTree as ET

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_gateway_groups(page, origin, key, container, docker, evidence):
    cli = ['php','/check/prefix/sbin/freesense-config']
    original = docker('exec',container,*cli,'backup')
    grants = ['page-system-gateways','page-system-gateways-editgateway',
              'page-system-gatewaygroups','page-system-gateways-editgatewaygroups']
    site = '/system/routing/groups'
    route = origin+'/api/v1/routing/'
    endpoint = route+'groups'
    auth = {'Authorization':'Bearer '+key}

    def configure(page_grants, scope='routing:write'):
        doc=ET.fromstring(docker('exec',container,*cli,'backup'))
        user=doc.find('./system/user')
        for node in user.findall('priv'):
            user.remove(node)
        for grant in ['page-system-restapi-keys',*page_grants]:
            ET.SubElement(user,'priv').text=grant
        doc.find('./restapi/apitoken/scopes').text=scope
        wan=doc.find('./interfaces/wan')
        for name,value in [('ipaddr','198.51.100.5'),('subnet','24'),('upstream4','198.51.100.1')]:
            element=wan.find(name)
            if element is None:
                element=ET.SubElement(wan,name)
            element.text=value
        docker('exec','-i',container,*cli,'restore',input=ET.tostring(doc))

    def put(url, operation, body):
        return page.request.put(url,headers=auth|{'Origin':origin,'X-FreeSense-Command':operation},data=body)

    def get(url):
        return page.request.get(url,headers=auth)

    def login(path=site):
        page.goto(origin+path)
        fill_api_key(page, key)
        page.get_by_role('button',name='Sign in',exact=True).click()
        expect(page.locator('#workspace')).to_be_visible()

    def click(label,target):
        with page.expect_response(lambda response:response.url==target and response.request.method=='PUT') as event:
            page.get_by_role('button',name=label,exact=True).click()
        return event.value

    try:
        configure(grants)
        settings = get(route+'gateways').json()['data']
        response = put(route+'gateways','createGateway',{'revision':settings['revision'],'enabled':True,'role':'lan',
                       'name':'Backup','address':'192.0.2.31'})
        assert response.status==200, response.text()
        gateway_id = response.json()['data']['id']
        login('/system/routing/gateways')
        page.get_by_role('navigation',name='Routing').get_by_role('link',name='Gateway Groups',exact=True).click()
        expect(page.get_by_role('heading',name='Gateway Groups',exact=True)).to_be_visible()
        baseline=get(endpoint).json()['data']['revision']
        page.get_by_role('link',name='Add gateway group',exact=True).click()
        page.get_by_label('Group Name',exact=True).fill('OfficeReturn')
        page.get_by_role('button',name='Save',exact=True).click()
        expect(page.locator('[data-error-for=members]')).not_to_be_empty()
        page.get_by_label('Tier for WAN default IPv4',exact=True).select_option('1')
        page.get_by_label('Tier for Backup',exact=True).select_option('2')
        page.get_by_label('Trigger Level',exact=True).select_option('downlosslatency')
        page.get_by_label('Keep Failover States',exact=True).select_option('keep')
        page.get_by_label('Description',exact=True).fill('Branch uplinks')
        if evidence:
            page.screenshot(path=evidence/'gateway-group-editor.png',full_page=True)
        result=click('Save',endpoint)
        assert result.status==200,result.text()
        group_id=result.json()['data']['id']
        expect(page.get_by_role('cell',name='OfficeReturn',exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence/'gateway-groups.png',full_page=True)
        listed=get(endpoint).json()['data']['groups']
        assert listed[0]['trigger']=='downlosslatency' and listed[0]['keep_failover_states']=='keep'
        assert {member['gateway']:member['tier'] for member in listed[0]['members']}=={'wan4':1,gateway_id:2}
        assert put(endpoint+'/remove','removeGatewayGroup',{'id':group_id,'revision':baseline}).status==409
        path = site+'/edit/'+group_id
        page.get_by_role('link',name='Edit OfficeReturn',exact=True).click()
        expect(page.get_by_label('Group Name',exact=True)).to_have_js_property('readOnly',True)
        page.get_by_label('Description',exact=True).fill('Reviewed uplinks')
        page.get_by_label('Tier for Backup',exact=True).select_option('')
        result=click('Save',endpoint+'/edit')
        assert result.status==200,result.text()
        pending=get(endpoint).json()['data']
        assert pending['approval']['pending']
        approve=endpoint+'/approve'
        rollback=endpoint+'/rollback'
        assert put(approve,'approveGatewayGroups',{'revision':baseline,'approvalRevision':pending['approval']['revision']}).status==409
        page.once('dialog',lambda dialog:dialog.accept())
        approved=click('Approve saved groups',approve)
        assert approved.status==200,approved.text()
        snapshot=approved.json()['data']
        assert not snapshot['pending'] and snapshot['can_rollback']
        expect(page.get_by_text('Saved and approved group configurations match.',exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence/'gateway-group-approval.png',full_page=True)
        page.once('dialog',lambda dialog:dialog.accept())
        restored=click('Roll back approval',rollback)
        assert restored.status==200 and restored.json()['data']['pending']
        assert put(rollback,'rollbackGatewayGroups',{'revision':snapshot['revision']}).status==409
        configure([grants[2]],'routing:read')
        login()
        expect(page.get_by_role('cell',name='Reviewed uplinks',exact=True)).to_be_visible()
        assert get(endpoint+'/edit/'+group_id).status==403
        assert page.get_by_role('button',name='Delete OfficeReturn').count()==0
        reviewed=get(endpoint).json()['data']
        current=reviewed['revision']
        assert put(approve,'approveGatewayGroups',{'revision':current,'approvalRevision':reviewed['approval']['revision']}).status==403
        assert put(rollback,'rollbackGatewayGroups',{'revision':reviewed['approval']['revision']}).status==403
        assert put(endpoint+'/remove','removeGatewayGroup',{'id':group_id,'revision':current}).status==403
        configure([grants[3]])
        assert get(endpoint).status==403 and get(endpoint+'/edit/'+group_id).status==200
        assert put(approve,'approveGatewayGroups',{'revision':current,'approvalRevision':reviewed['approval']['revision']}).status==403
        login(path)
        expect(page.get_by_label('Group Name',exact=True)).to_have_value('OfficeReturn')
        configure(grants)
        login()
        page.once('dialog',lambda dialog:dialog.accept())
        assert click('Delete OfficeReturn',endpoint+'/remove').status==200
        expect(page.get_by_text('No gateway groups are configured.',exact=True)).to_be_visible()
        assert get(endpoint+'/edit/'+group_id).status==404
        assert get(endpoint+'/apply').status==404
        print('Gateway Groups Chromium verified confirmed delete, stale revisions, central validation, retained members, approval/rollback and distinct list/editor grants.')
    finally:
        docker('exec','-i',container,*cli,'restore',input=original)
