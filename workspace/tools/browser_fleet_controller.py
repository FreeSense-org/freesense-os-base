"""Exercise the independent node, Fleet controller, inventory and approval grants in Chromium."""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
from xml.etree import ElementTree
from browser_shell import sign_out, menu_link, fill_api_key
from playwright.sync_api import expect
import json


def exercise_fleet_controller(page,origin,credential,container,docker,evidence=None):
    prefix='/check/prefix/'
    cli=['php',prefix+'sbin/freesense-config']
    snapshot=docker('exec',container,*cli,'backup')
    docker('exec',container,'cp','-R','/optional/sysutils/FreeSense-pkg-Fleet/files/usr/local/.',prefix)
    private=r'''
    require '/check/prefix/FreeSense/autoload.php';
    $store=new FreeSense\Config\ConfigurationStore('/check/config');
    $ep=FreeSense\Packages\Fleet\ControllerEndpoint::propose($store->document(),'192.0.2.2',9443);
    $vault=new FreeSense\Packages\Fleet\ControllerVault('/check/config');
    $catalog=new FreeSense\Packages\Fleet\ControllerCatalog($vault);
    $pin=bin2hex(random_bytes(32));$server=new FreeSense\Packages\Fleet\EnrollmentServer($vault,$ep,$pin);
    $out=[];
    foreach (['Office monitor','Branch monitor'] as $label) {
      $export=$catalog->invite($ep,$pin,$label,1);
      $seed=FreeSense\Fleet\FactorySeed::parse(file_get_contents($export['seed']),time());
      $out[]=$seed->withInvitation(function($id,$token,$record) use($seed,$pin,$server,$label) {
        $auth='Bearer '.$token; $now=time();$peer='192.0.2.2';
        [$ok,$reply]=$server->handle('POST','/fleet/join/begin',$auth,['token_id'=>$id],$peer,$now);
        if ($ok!==200) exit(1);
        $ch=base64_decode($reply['challenge']);$nonce=random_bytes(32);$pub=random_bytes(32);
        $material=FreeSense\Packages\Fleet\JoinProof::material($seed,$record,null);
        [$ok]=$server->handle('POST','/fleet/join/enroll',$auth,['token_id'=>$id,'nonce'=>base64_encode($nonce),'node_wg_pub'=>base64_encode($pub),
          'proof'=>FreeSense\Packages\Fleet\JoinProof::exchange($material,$ch,$nonce,$id,$pub,$pin),'inventory'=>['hostname'=>'test-monitor']],$peer,$now);
        if ($ok!==200) exit(2);
        return FreeSense\Packages\Fleet\JoinProof::code($pub,base64_decode($reply['controller_wg_pub']),$ch,$nonce);
      });
    }
    echo json_encode($out);
    '''
    answers=json.loads(docker('exec',container,'php','-r',private))
    auth={'Authorization':'Bearer '+credential}
    controller=origin+'/api/v1/packages/fleet-controller'; dashboard=origin+'/api/v1/packages/fleet-dashboard'
    def login(grant,scope):
        doc=ElementTree.fromstring(snapshot)
        user=doc.find('./system/user')
        for n in user.findall('priv'):user.remove(n)
        for g in ('page-system-restapi-keys',grant):ElementTree.SubElement(user,'priv').text=g
        doc.find('./restapi/apitoken/scopes').text=scope
        docker('exec','-i',container,*cli,'restore',input=ElementTree.tostring(doc))
        page.goto(origin+'/packages');fill_api_key(page, credential)
        page.get_by_role('button',name='Sign in',exact=True).click();expect(page.locator('#workspace')).to_be_visible()
    def signout():sign_out(page)
    try:
        login('page-system-fleet','packages.fleet-controller:write')
        assert page.request.get(controller,headers=auth).status==403
        signout()
        login('page-status-fleet-dashboard','packages.fleet-dashboard:read')
        expect(page.get_by_role('link',name='System: FreeSense Fleet',exact=True)).to_have_count(0)
        menu_link(page,'Status','Status: FreeSense Fleet').click()
        expect(page.get_by_text('No enrolled firewalls.',exact=True)).to_be_visible()
        data=page.request.get(dashboard,headers=auth).json()['data']['values']
        assert set(data)=={'nodes','revision'} and page.request.get(controller,headers=auth).status==403
        signout()
        login('page-system-fleet-controller','packages.fleet-controller:read')
        menu_link(page,'System','System: FreeSense Fleet').click()
        expect(page.get_by_role('heading',name='Pending approvals')).to_be_visible()
        expect(page.get_by_role('button',name='Approve monitor')).to_have_count(0)
        detail=page.request.get(controller,headers=auth).json()['data']
        assert detail['operations']==[] and len(detail['values']['requests'])==2
        assert not any('code' in row for row in detail['values']['requests'])
        signout()
        login('page-system-fleet-controller','packages.fleet-controller:write')
        menu_link(page,'System','System: FreeSense Fleet').click()
        expect(page.get_by_role('heading',name='Office monitor')).to_be_visible()
        row=page.get_by_role('heading',name='Office monitor').locator('..')
        code=row.get_by_label('Comparison number');submit=row.get_by_role('button',name='Approve monitor')
        code.fill('abc');submit.click()
        expect(code).to_have_attribute('aria-invalid','true')
        wrong='000001' if answers[0]=='000000' else '000000'
        code.fill(wrong)
        with page.expect_response(lambda r:r.url==controller+'/approve' and r.request.method=='PUT') as response:submit.click()
        assert response.value.status==422 and 'code' in response.value.json()['error']['fields']
        code.fill(answers[0])
        with page.expect_response(lambda r:r.url==controller+'/approve' and r.request.method=='PUT') as response:submit.click()
        assert response.value.status==200
        expect(row.get_by_text('test-monitor · approved',exact=True)).to_be_visible()
        other=page.get_by_role('heading',name='Branch monitor').locator('..')
        page.once('dialog',lambda prompt:prompt.accept())
        with page.expect_response(lambda r:r.url==controller+'/reject' and r.request.method=='PUT') as response:
            other.get_by_role('button',name='Reject enrollment').click()
        assert response.value.status==200
        expect(page.get_by_role('heading',name='Branch monitor')).to_have_count(0)
        assert 'fsk_' not in page.locator('main').inner_text() and all(answer not in page.locator('main').inner_text() for answer in answers)
        if evidence:page.screenshot(path=str(evidence/'fleet-controller.png'))
    finally:
        docker('exec','-i',container,*cli,'restore',input=snapshot)

