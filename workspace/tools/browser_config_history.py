from xml.etree import ElementTree

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_config_history(page, origin, key, container, docker, evidence=None):
    path = '/diagnostics/configuration-history'
    endpoint = origin + '/api/v1' + path
    headers = {'Authorization': 'Bearer ' + key}
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')

    def restore(grant, scope, marker, hostname="printer"):
        doc = ElementTree.fromstring(original)
        doc.find("./unbound/hosts/host").text = hostname
        user = doc.find('./system/user')
        for item in user.findall('priv'):
            user.remove(item)
        for item in ['page-system-restapi-keys', grant]:
            ElementTree.SubElement(user, 'priv').text = item
        doc.find('./restapi/apitoken/scopes').text = scope
        ElementTree.SubElement(doc.find('./system'), 'history_fixture').text = marker
        docker('exec', '-i', container, *cli, 'restore', input=ElementTree.tostring(doc))

    marker = 'configuration-content-privacy-fixture'
    try:
        restore('page-status-dhcpleases','config:read','baseline')
        assert page.request.get(endpoint, headers=headers).status == 403
        restore('page-diagnostics-configurationhistory','config:read','baseline')
        restore('page-diagnostics-configurationhistory','config:read',marker)
        page.goto(origin + path)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in').click()
        compare = page.get_by_role('button', name='Compare with current').first
        expect(compare).to_be_visible()
        receipt = page.request.get(endpoint, headers=headers)
        recents = receipt.json()['data']['revisions']
        assert receipt.status == 200 and 1 <= len(recents) <= 30 and marker not in receipt.text()
        metadata = receipt.json()['data']
        assert metadata['format'] == '2.0'
        assert metadata['currentBytes'] == len(docker('exec', container, *cli, 'backup'))
        assert set(metadata['revisionBytes']) == set(recents)
        assert all(count > 0 for count in metadata['revisionBytes'].values())
        expect(page.get_by_role('columnheader',name='Version',exact=True)).to_be_visible()
        expect(page.get_by_role('columnheader',name='Size',exact=True)).to_be_visible()
        search = page.get_by_role('searchbox',name='Search configuration history')
        search.fill('deliberately unmatched revision')
        expect(compare).to_have_count(0)
        expect(page.get_by_text('No backups match the search.')).to_be_visible()
        expect(page.get_by_role('cell',name='Current',exact=True)).to_be_visible()
        search.fill(next(iter(recents)))
        expect(page.get_by_role('button',name='Compare with current')).to_have_count(1)
        search.clear()
        expect(page.get_by_role('cell',name='Current',exact=True)).to_be_visible()
        revision = next(iter(recents))
        older = list(recents)[1]
        page.get_by_role('combobox',name='Older revision',exact=True).select_option(older)
        page.get_by_role('combobox',name='Newer revision',exact=True).select_option(revision)
        with page.expect_response(lambda res: 'against=' in res.url) as pair:
            page.get_by_role('button',name='Compare revisions').click()
        data = pair.value.json()['data']
        assert pair.value.status == 200 and data['revision'] == older and data['against'] == revision and 'editRevision' not in data
        assert page.request.get(endpoint+f'?revision={revision}&against={older}',headers=headers).status == 422
        expect(page.get_by_role('heading',name=f'Changes from {older} to {revision}')).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / 'compare-revisions.png', full_page=True)

        with page.expect_response(lambda res: res.url.startswith(endpoint + '?revision=')) as compared:
            compare.click()
        result = compared.value
        assert result.json()['data'] == {'revision':revision,'changes':[{'section':'System','change':'changed'}]}
        assert marker not in result.text()
        expect(page.get_by_role('cell', name='System', exact=True)).to_be_visible()
        assert page.request.get(endpoint+'?revision=bad', headers=headers).status == 422
        assert page.request.get(endpoint+'?revision=cfg-0000000000000000', headers=headers).status == 404
        assert page.request.put(endpoint, headers=headers).status == 405
        cleanup = endpoint + '/remove'
        delete_headers = headers | {'Origin':origin,'X-FreeSense-Command':'deleteConfigurationBackup'}
        assert page.request.put(cleanup,headers=delete_headers,data={'id':revision}).status == 403
        assert page.get_by_role('button',name='Delete backup '+revision).count() == 0
        settings_endpoint = endpoint + '/retention'
        retention_headers = headers | {'Origin':origin,'X-FreeSense-Command':'saveConfigurationBackupRetention'}
        assert page.request.put(settings_endpoint,headers=retention_headers,
                                data={'revision':metadata['retention']['editRevision'], 'limit':0}).status == 403
        assert page.get_by_text('Configuration history settings', exact=True).count() == 0
        if evidence:
            page.screenshot(path=evidence / 'configuration-history.png', full_page=True)
        command = endpoint + '/restore'
        write_headers = headers | {'Origin':origin,'X-FreeSense-Command':'restoreNetworkConfiguration'}
        assert page.request.put(command,headers=write_headers,data={}).status == 403
        restore('page-diagnostics-configurationhistory','config:read','old-identity','older-settings')
        restore('page-diagnostics-configurationhistory','config:write','current-identity','newer-settings')
        page.reload()
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in').click()
        recents = page.request.get(endpoint,headers=headers).json()['data']['revisions']
        target = next(iter(recents))
        preview = endpoint + '?revision=' + target
        reviewed = page.request.get(preview,headers=headers).json()['data']
        assert reviewed['canRestore']
        restore('page-diagnostics-configurationhistory','config:write','newer-identity','newer-settings')
        assert page.request.put(command,headers=write_headers,data={'id':target,'revision':reviewed['editRevision']}).status == 409
        page.get_by_role('button', name='Refresh', exact=True).click()
        row = page.get_by_role('row').filter(has_text=target)
        row.get_by_role('button',name='Compare with current').click()
        submit = page.get_by_role('button',name='Restore network settings')
        expect(submit).to_be_visible()
        before = ElementTree.fromstring(docker('exec',container,*cli,'backup'))
        page.once('dialog',lambda dialog: dialog.dismiss())
        submit.click()
        assert ElementTree.canonicalize(docker('exec',container,*cli,'backup')) == ElementTree.canonicalize(ElementTree.tostring(before))
        page.once('dialog',lambda dialog: dialog.accept())
        with page.expect_response(lambda res: res.url == command and res.request.method == 'PUT') as saved:
            submit.click()
        assert saved.value.status == 200
        assert saved.value.json()['data']['restored'] == ['DNS Resolver']
        after = ElementTree.fromstring(docker('exec',container,*cli,'backup'))
        assert after.findtext('./unbound/hosts/host') == 'older-settings'
        for section in ('system','restapi'):
            assert ElementTree.tostring(before.find(section)) == ElementTree.tostring(after.find(section))
        assert page.request.get(endpoint,headers=headers).status == 200
        current_xml = docker('exec',container,*cli,'backup')
        revisions = page.request.get(endpoint,headers=headers).json()['data']['revisions']
        spare = next(iter(revisions))
        remove = page.get_by_role('button', name='Delete backup '+spare,exact=True)
        page.once('dialog',lambda dialog: dialog.dismiss())
        remove.click()
        assert spare in page.request.get(endpoint,headers=headers).json()['data']['revisions']
        page.once('dialog',lambda dialog: dialog.accept())
        with page.expect_response(lambda res: res.url == cleanup and res.request.method == 'PUT') as deleted:
            remove.click()
        assert deleted.value.status == 200 and deleted.value.json()['data']['deleted'] == spare
        expect(remove).to_have_count(0)
        updated = page.request.get(endpoint,headers=headers).json()['data']['revisions']
        assert spare not in updated and len(updated) >= len(revisions) - 1
        assert page.request.put(cleanup,headers=delete_headers,data={'id':spare}).status == 409
        assert docker('exec',container,*cli,'backup') == current_xml
        prechange = page.request.get(endpoint,headers=headers).json()['data']['retention']
        assert prechange['limit'] is None and prechange['defaultLimit'] == 30
        assert page.request.put(settings_endpoint,headers=retention_headers,
                                data={'revision':prechange['editRevision'], 'limit':-1}).status == 422
        page.get_by_text('Configuration history settings', exact=True).click()
        limit = page.get_by_role('spinbutton', name='Maximum backups')
        limit.fill('0')
        with page.expect_response(lambda res: res.url == settings_endpoint and res.request.method == 'PUT') as updated:
            page.get_by_role('button', name='Save retention settings').click()
        assert updated.value.status == 200
        expect(page.get_by_text('No previous configurations saved.')).to_be_visible()
        assert page.request.put(settings_endpoint, headers=retention_headers,
                                data={'revision':prechange['editRevision'], 'limit':1}).status == 409
        page.get_by_text('Configuration history settings', exact=True).click()
        expect(limit).to_have_value('0')
        limit.clear()
        with page.expect_response(lambda res: res.url == settings_endpoint and res.request.method == 'PUT') as reset:
            page.get_by_role('button', name='Save retention settings').click()
        assert reset.value.status == 200 and reset.value.json()['data']['effectiveLimit'] == 30
        assert page.request.get(endpoint,headers=headers).json()['data']['revisions'] == {}
        if evidence:
            page.get_by_text('Configuration history settings', exact=True).click()
            page.screenshot(path=evidence / 'history-retention.png', full_page=True)
        print('Chromium verified history metadata, comparison, scoped restore, deletion and retained backup settings.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)
