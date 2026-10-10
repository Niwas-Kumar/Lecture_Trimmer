import base64
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import desktop_runtime.desktop_update as update
import lecture_trimmer.runtime.diagnostics as diagnostics

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    def tearDown(self):
        self.work.cleanup()
    def archive(self, files):
        archive = self.root / 'code.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for name, value in files.items():
                bundle.writestr(name, value)
        release = {'version':'1.1.0','runtime':'1',
                   'files':{name:hashlib.sha256(value).hexdigest() for name,value in files.items()},
                   'archive':{'size':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}}
        envelope = {'release':release, 'signature':base64.b64encode(self.key.sign(update.canonical(release))).decode()}
        return archive, envelope
    def test_verified_update_stages_without_touching_data(self):
        data = self.root / 'output'
        data.mkdir()
        (data / 'keep.mp4').write_bytes(b'original')
        archive, envelope = self.archive({'app.py':b'print("new")','nested/helper.py':b'x=1'})
        destination = update.stage_archive(archive,envelope,self.public,self.root/'versions')
        self.assertEqual((destination/'app.py').read_bytes(),b'print("new")')
        self.assertEqual((data/'keep.mp4').read_bytes(),b'original')
        self.assertFalse(list((self.root/'versions').glob('.staging-*')))
    def test_tampered_signature_and_payload_rejected(self):
        archive, envelope = self.archive({'app.py':b'new'})
        envelope['release']['version']='2.0.0'
        with self.assertRaises(update.UpdateError):
            update.stage_archive(archive,envelope,self.public,self.root/'versions')
        archive, envelope = self.archive({'app.py':b'new'})
        archive.write_bytes(archive.read_bytes()+b'tamper')
        with self.assertRaises(update.UpdateError):
            update.stage_archive(archive,envelope,self.public,self.root/'versions')
        self.assertFalse((self.root/'versions').exists())
    def test_malicious_archive_paths_rejected_even_when_signed(self):
        for name in ('../evil.py','C:/evil.py','dir\\evil.py','CON','x./file.py','x//y.py'):
            archive,envelope=self.archive({'app.py':b'ok',name:b'bad'})
            with self.assertRaises(update.UpdateError, msg=name):
                update.stage_archive(archive,envelope,self.public,self.root/'versions')
    def test_same_version_stage_is_idempotent_but_changed_directory_fails(self):
        archive,envelope=self.archive({'app.py':b'ok'})
        path=update.stage_archive(archive,envelope,self.public,self.root/'versions')
        self.assertEqual(path,update.stage_archive(archive,envelope,self.public,self.root/'versions'))
        (path/'app.py').write_bytes(b'modified')
        with self.assertRaises(update.UpdateError):
            update.stage_archive(archive,envelope,self.public,self.root/'versions')
    def test_runtime_mismatch_is_rejected(self):
        _,env=self.archive({'app.py':b'ok'})
        env['release']['runtime']='2'
        env['signature']=base64.b64encode(self.key.sign(update.canonical(env['release']))).decode()
        with self.assertRaises(update.UpdateError):
            update.verify_manifest(env,self.public)
    def test_updates_do_not_interrupt_processing(self):
        with patch.object(update,'FROZEN',True), update.processing_guard():
            self.assertIn('Finish',update.check_and_stage('https://example.org/release.json'))
        self.assertFalse(update.busy())
    def test_drive_file_links_and_https_required(self):
        self.assertIn('export=download',update.download_url('https://drive.google.com/file/d/1234567890abc/view'))
        for link in ('http://example.com/file','file:///C:/data','https://drive.google.com/drive/folders/abc'):
            with self.assertRaises(update.UpdateError):
                update.download_url(link)
    def test_failed_start_rolls_back_pointer_only(self):
        from tools.desktop_launcher import rollback
        state=self.root/'installed-state.json'
        state.write_text(json.dumps({'current':'1.1.0','previous':'bundled','trial':True}))
        self.assertTrue(rollback(self.root))
        self.assertEqual(json.loads(state.read_text())['current'],'bundled')
        self.assertFalse(rollback(self.root))

class DiagnosticTests(unittest.TestCase):
    def test_support_report_excludes_other_users_and_exception_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(diagnostics,'DATA_ROOT',root),patch.object(diagnostics,'LOG_DIR',root/'logs'):
                try:
                    raise RuntimeError('secret token and transcript text')
                except Exception as exc:
                    own=diagnostics.record_error(exc,owner='alice')
                    other=diagnostics.record_error(exc,owner='bob')
                report=diagnostics.export_support('alice')
                with zipfile.ZipFile(report) as bundle:
                    text=''.join(bundle.read(name).decode() for name in bundle.namelist())
                    self.assertIn(own,text)
                    self.assertNotIn(other,text)
                    for private in ('secret token','transcript text','alice','bob'):
                        self.assertNotIn(private,text)
                    self.assertIn('RuntimeError',text)
                    self.assertIn('test_desktop_release.py',text)

if __name__=='__main__':
    unittest.main()
