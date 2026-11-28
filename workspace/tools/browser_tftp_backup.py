import tarfile
from pathlib import Path
from xml.etree import ElementTree

from browser_shell import fill_api_key
from playwright.sync_api import expect


def exercise_tftp_backup(page, origin, key, container, docker, evidence=None):
    page_path = '/services/tftp/files'
    api_path = '/api/v1/packages/tftp'
    cli = ['php', '/check/prefix/sbin/freesense-config']
    original = docker('exec', container, *cli, 'backup')
    auth = {'Authorization': 'Bearer ' + key}
    docker('exec', container, 'mkdir', '-p', '/var/db')

    def account(scope, grant='page-services-tftpd', path=page_path):
        document = ElementTree.fromstring(original)
        user = document.find('./system/user')
        for privilege in user.findall('priv'):
            user.remove(privilege)
        for name in ['page-system-restapi-keys', grant]:
            ElementTree.SubElement(user, 'priv').text = name
        document.find('./restapi/apitoken/scopes').text = scope
        docker('exec', '-i', container, *cli, 'restore', input=ElementTree.tostring(document))
        page.goto(origin + path)
        fill_api_key(page, key)
        page.get_by_role('button', name='Sign in').click()
        expect(page.locator('#workspace')).to_be_visible()

    def submit(label, op, status=200):
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_response(lambda r: r.url.endswith(api_path + '/files/' + op) and r.request.method == 'PUT') as response:
            page.get_by_role('button', name=label, exact=True).click()
        assert response.value.status == status, (op, response.value.status, status)

    try:
        account('packages.tftp:write')
        expect(page.get_by_text('No local backup has been created.')).to_be_visible()
        expect(page.get_by_role('button', name='Restore files')).to_have_count(0)
        upload = page.locator('form').filter(has=page.get_by_role('button', name='Upload file', exact=True))
        payload = bytes(range(256)) * 3
        upload.locator('input[type=file]').set_input_files([
            {'name':'boot.bin','mimeType':'application/octet-stream','buffer':payload},
            {'name':'empty.bin','mimeType':'application/octet-stream','buffer':b''}])
        with page.expect_response(lambda r: r.url.endswith('/files/upload') and r.request.method == 'PUT') as uploaded:
            upload.get_by_role('button', name='Upload file', exact=True).click()
        assert uploaded.value.status == 200
        expect(page.locator('li').get_by_text('boot.bin', exact=True)).to_be_visible()
        # Exporting current files has no persistent snapshot side effect.
        with page.expect_download() as fresh:
            page.get_by_role('button', name='Download archive', exact=True).click()
        assert fresh.value.suggested_filename == 'tftp.bak.tgz'
        with tarfile.open(fresh.value.path(), 'r:gz') as archive:
            assert archive.extractfile('tftpboot/boot.bin').read() == payload
        expect(page.get_by_text('No local backup has been created.')).to_be_visible()
        assert page.request.get(origin + api_path,headers=auth).json()['data']['values']['backup'] is None
        submit('Back up files', 'backup')
        restore = page.get_by_role('button', name='Restore files', exact=True)
        expect(restore).to_be_visible()
        page.get_by_label('Filename', exact=True).fill('image.txt')
        submit('Remove file', 'remove')
        expect(page.get_by_text('File removed.', exact=True)).to_be_visible()
        expect(restore).to_be_visible()
        expect(page.locator('li').get_by_text('image.txt', exact=True)).to_have_count(0)
        docker('exec', container, 'sh', '-c', 'printf changed >/tftpboot/boot.bin; printf retained > /tftpboot/new.txt')
        submit('Restore files', 'restore', 409)
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(restore).to_be_visible()
        page.once('dialog', lambda dialog: dialog.dismiss())
        restore.click()
        docker('exec', container, 'test', '!', '-e', '/tftpboot/image.txt')
        submit('Restore files', 'restore')
        expect(page.get_by_text('Saved files restored.', exact=True)).to_be_visible()
        assert docker('exec', container, 'cat', '/tftpboot/image.txt') == b'disposable-file'
        assert docker('exec', container, 'cat', '/tftpboot/new.txt') == b'retained'
        assert docker('exec', container, 'cat', '/tftpboot/boot.bin') == payload
        if evidence:
            page.screenshot(path=evidence / 'tftp-local-backup.png', full_page=True)
        account('packages.tftp:read', path='/packages/tftp')
        expect(page.get_by_role('heading', name='Local backup')).to_be_visible()
        expect(restore).to_have_count(0)
        detail = page.request.get(origin + api_path, headers=auth).json()['data']
        assert detail['values']['backup']['files'] == 4
        archive_path = detail['archive']['path']
        assert archive_path == api_path + '/files/archive'
        assert detail['archive']['filename'] == 'tftp.bak.tgz'
        with page.expect_download() as saved_archive:
            with page.expect_response(lambda r: r.url.startswith(origin + archive_path + '?')) as transfer:
                page.get_by_role('button', name='Download archive', exact=True).click()
        assert transfer.value.status == 200 and transfer.value.request.method == 'GET'
        assert saved_archive.value.suggested_filename == 'tftp.bak.tgz'
        with tarfile.open(saved_archive.value.path(),'r:gz') as archive:
            assert {m.name for m in archive} == {'tftpboot','tftpboot/boot.bin','tftpboot/empty.bin','tftpboot/image.txt','tftpboot/keep.txt','tftpboot/new.txt'}
            assert archive.extractfile('tftpboot/new.txt').read() == b'retained'
            assert archive.extractfile('tftpboot/boot.bin').read() == payload
            assert archive.extractfile('tftpboot/empty.bin').read() == b''
        assert page.request.get(origin + api_path,headers=auth).json()['data']['values'] == detail['values']
        assert page.request.get(origin + archive_path + '?filename=tftp.bak.tgz').status == 401
        assert page.request.get(origin + archive_path + '?filename=boot.bin',headers=auth).status == 404
        assert page.request.put(origin + archive_path,headers=auth,data={}).status == 405
        assert page.request.get(origin + '/api/v1/packages/tftp-settings/files/archive?filename=tftp.bak.tgz',headers=auth).status == 404
        download_path = detail['download']['path']
        assert download_path == api_path + '/files/download'
        for filename, expected in [('boot.bin', payload), ('empty.bin', b''), ('image.txt', b'disposable-file')]:
            with page.expect_download() as saved_file:
                with page.expect_response(lambda r: r.url.startswith(origin + download_path + '?')) as transfer:
                    page.get_by_role('button', name='Download ' + filename, exact=True).click()
            response = transfer.value
            assert response.status == 200 and response.request.method == 'GET'
            assert response.headers['content-type'] == 'application/octet-stream'
            assert response.headers['content-disposition'] == 'attachment; filename="' + filename + '"'
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert response.headers['cache-control'] == 'no-store'
            assert int(response.headers['content-length']) == len(expected)
            assert saved_file.value.suggested_filename == filename and Path(saved_file.value.path()).read_bytes() == expected
        # The central client must enforce its own ceiling and refuse shortened or oversized HTTP bodies.
        transfer_guards = page.evaluate("""async path => {
            const {ApplianceClient}=await import('/assets/shared/commands.js');
            const client=new ApplianceClient(()=>{});
            const original=window.fetch;
            const results=[];
            try {
                for (const [name, limit, length, bytes] of [
                    ['oversize-registration',67108865,0,0], ['oversize-response',67108864,67108865,0],
                    ['short',8,5,3], ['over',8,2,3], ['broken',8,5,-1]
                ]) {
                    let requests=0;
                    window.fetch=async () => {
                        ++requests;
                        const body=bytes<0 ? new ReadableStream({start(c){c.error(new Error('Interrupted transfer'));}})
                            : new Uint8Array(bytes);
                        return new Response(body,{headers:{'Content-Type':'application/octet-stream',
                            'Content-Length':String(length),'Content-Disposition':'attachment; filename="source.bin"'}});
                    };
                    try { await client.download({path,maxBytes:limit},'source.bin'); results.push([name,'accepted',requests]); }
                    catch { results.push([name,'rejected',requests]); }
                }
            } finally { window.fetch=original; }
            return results;
        }""", download_path)
        assert transfer_guards == [[name, 'rejected', count] for name,count in [
            ('oversize-registration',0),('oversize-response',1),('short',1),('over',1),('broken',1)]]
        download_url = origin + download_path
        assert page.request.get(download_url, params={'filename': '../outside'}, headers=auth).status == 422
        assert page.request.get(download_url, params={'filename': 'missing.bin'}, headers=auth).status == 404
        assert page.request.get(download_url + '?filename=a&filename=b', headers=auth).status == 400
        assert page.request.get(download_url + '?filename=boot.bin').status == 401
        assert page.request.put(download_url, headers=auth, data={}).status == 405
        assert page.request.get(origin + '/api/v1/packages/tftp-settings/files/download?filename=boot.bin', headers=auth).status == 404
        docker('exec',container,'rm','/tftpboot/boot.bin')
        page.get_by_role('button', name='Download boot.bin', exact=True).click()
        expect(page.get_by_text('The selected file is unavailable.',exact=True)).to_be_visible()
        if evidence:
            page.screenshot(path=evidence / 'tftp-download-readonly.png', full_page=True)
        headers = {**auth, 'Origin':origin, 'X-FreeSense-Command':'package.tftp.restore'}
        assert page.request.put(origin + api_path + '/files/restore', headers=headers, data={'revision':detail['values']['revision']}).status == 403
        account('packages.tftp:write', 'page-system-gateways')
        assert page.request.get(origin + api_path, headers=auth).status == 403
        assert page.request.get(download_url + '?filename=image.txt', headers=auth).status == 403
        assert page.request.get(origin + archive_path + '?filename=tftp.bak.tgz', headers=auth).status == 403
        print('Chromium verified read-only TFTP binary/empty downloads, authenticated failures, and authorized archive export/backup/restore.')
    finally:
        docker('exec', '-i', container, *cli, 'restore', input=original)

