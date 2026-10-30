"""Office transaction and preview regression tests; real COM uses manual_office_v16.py."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext
from app import office_operations as office

class OfficeTransactions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.src=self.root/'source.docx'
        self.src.write_bytes(b'original')
    def tearDown(self):
        self.temp.cleanup()
    def test_edit_worker_failure_preserves_original(self):
        with patch.object(office,'ROOT',self.root),patch.object(office,'PREVIEW_ROOT',self.root/'previews'),patch.object(office,'require_available',lambda p:nullcontext()),patch('app.snapshots.create',return_value={'id':'snapshot'}),patch.object(office,'run_worker',side_effect=ValueError('failed')):
            with self.assertRaisesRegex(ValueError,'failed'):
                office.execute({'action':'edit','path':str(self.src),'operations':[{'op':'insert_text','text':'test'}]})
        self.assertEqual(self.src.read_bytes(),b'original')
        self.assertEqual(list(self.root.glob('.*.docx')),[])
    def test_edit_commits_saved_staging_once(self):
        def worker(payload,timeout):
            Path(payload['path']).write_bytes(b'edited')
            return {'ok':True,'verified_saved':True}
        with patch.object(office,'ROOT',self.root),patch.object(office,'PREVIEW_ROOT',self.root/'previews'),patch.object(office,'require_available',lambda p:nullcontext()),patch('app.snapshots.create',return_value={'id':'snapshot'}) as snap,patch.object(office,'run_worker',side_effect=worker):
            result=office.execute({'action':'edit','path':str(self.src),'operations':[{'op':'insert_text','text':'test'}],'preview':False})
        self.assertEqual(self.src.read_bytes(),b'edited')
        self.assertEqual(result['snapshot_id'],'snapshot')
        snap.assert_called_once()
    def test_external_edit_is_not_overwritten(self):
        def worker(payload,timeout):
            Path(payload['path']).write_bytes(b'edited')
            self.src.write_bytes(b'external')
            return {'ok':True}
        with patch.object(office,'ROOT',self.root),patch.object(office,'PREVIEW_ROOT',self.root/'previews'),patch.object(office,'require_available',lambda p:nullcontext()),patch('app.snapshots.create',return_value={'id':'snapshot'}),patch.object(office,'run_worker',side_effect=worker):
            with self.assertRaisesRegex(ValueError,'其他程序修改'):
                office.execute({'action':'edit','path':str(self.src),'operations':[{'op':'insert_text','text':'test'}]})
        self.assertEqual(self.src.read_bytes(),b'external')
    def test_no_overwrite_on_create(self):
        with self.assertRaisesRegex(ValueError,'已存在'):
            office.execute({'action':'create','path':str(self.src)})
    def test_attachment_scope_checked(self):
        with self.assertRaisesRegex(ValueError,'不属于'):
            office.image_arguments([{'op':'insert_picture','attachment_id':'a'*32}],{})
    def test_preview_rejects_traversal(self):
        with self.assertRaises(ValueError): office.preview_info('../escape')
    def test_picture_ratio(self):
        from PIL import Image
        from app.office_worker import picture_size
        path=self.root/'image.png'; Image.new('RGB',(200,100)).save(path)
        self.assertEqual(picture_size({'image_path':str(path),'width':100}),(100,50))

if __name__=='__main__': unittest.main()
