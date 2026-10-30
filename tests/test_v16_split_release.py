"""Tiny split ZIP fixtures plus actual Windows PowerShell merge, no application run."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("split_release_v16",REPO/"scripts/split_release.py")
splitter=importlib.util.module_from_spec(spec)
spec.loader.exec_module(splitter)

class SplitRelease16Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="split-release-synthetic-")
        self.root=Path(self.temp.name)
        self.archive=self.root/"测试便携包.zip"
        with zipfile.ZipFile(self.archive,"w",compression=zipfile.ZIP_STORED) as bundle:
            bundle.writestr("LocalAIStudio/启动.bat","@echo Synthetic never executed\n")
            bundle.writestr("LocalAIStudio/models/synthetic.bin",b"Synthetic model fixture; not real weights.\n"*300)
        self.output=self.root/"parts"
        self.original=self.archive.read_bytes()
        self.sha=hashlib.sha256(self.original).hexdigest()
    def tearDown(self):self.temp.cleanup()
    def split(self):
        return splitter.split(self.archive,self.output,chunk_bytes=2048,expected_sha256=self.sha)
    def manifest(self):
        return json.loads((self.output/splitter.MANIFEST).read_text(encoding="utf-8"))
    def save_manifest(self,value):
        (self.output/splitter.MANIFEST).write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    def test_chunk_size_order_hash_and_windows_assets(self):
        result=self.split();data=self.manifest()
        self.assertTrue(result["passed"])
        self.assertEqual(result["sha256"],self.sha)
        self.assertTrue(all(p["bytes"]==2048 for p in data["parts"][:-1]))
        self.assertLessEqual(data["parts"][-1]["bytes"],2048)
        self.assertEqual(b"".join((self.output/p["name"]).read_bytes() for p in data["parts"]),self.original)
        self.assertTrue((self.output/"Merge-Portable.ps1").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue((self.output/"合并便携包.cmd").is_file())
        self.assertIn("portable-parts.json",(self.output/"portable-parts-SHA256SUMS.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.output/self.archive.name).exists())
        self.assertTrue(splitter.verify_split(self.output)["passed"])
    def test_bad_expected_hash_and_nonempty_output_rejected(self):
        with self.assertRaisesRegex(ValueError,"verified portable"):
            splitter.split(self.archive,self.output,chunk_bytes=2048,expected_sha256="0"*64)
        with self.assertRaisesRegex(ValueError,"empty"):
            self.split()
    def test_missing_or_changed_part_rejected(self):
        self.split();part=self.output/self.manifest()["parts"][1]["name"]
        blob=part.read_bytes();part.write_bytes(bytes([blob[0]^1])+blob[1:])
        with self.assertRaisesRegex(ValueError,"digest"):splitter.verify_split(self.output)
        part.unlink()
        with self.assertRaises(ValueError):splitter.verify_split(self.output)
    def test_manifest_whitelist_order_and_size_are_checked(self):
        self.split();original=self.manifest()
        changed=json.loads(json.dumps(original));changed["parts"][0]["name"]="../outside.zip.001"
        self.save_manifest(changed)
        with self.assertRaises(ValueError):splitter.verify_split(self.output)
        changed=json.loads(json.dumps(original));changed["parts"].reverse()
        self.save_manifest(changed)
        with self.assertRaises(ValueError):splitter.verify_split(self.output)
        changed=json.loads(json.dumps(original));changed["chunk_bytes"]=splitter.SLICE_BYTES+1
        self.save_manifest(changed)
        with self.assertRaises(ValueError):splitter.verify_split(self.output)
    def test_final_hash_mismatch_rejected(self):
        self.split();data=self.manifest();data["archive_sha256"]="0"*64;self.save_manifest(data)
        with self.assertRaisesRegex(ValueError,"Reassembled"):splitter.verify_split(self.output)
    def test_windows_filename_rules(self):
        for name in ("../x.zip","CON.zip","x:stream.zip","x.zip.","x.zip ","a\\b.zip"):
            self.assertFalse(splitter.leaf(name))
    def run_merge(self):
        return subprocess.run(["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(self.output/"Merge-Portable.ps1")],
            capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=30)
    @unittest.skipUnless(os.name=="nt" and shutil.which("powershell.exe"),"Windows PowerShell merge is verified on Windows")
    def test_real_powershell_merge_hash_crc_and_no_overwrite(self):
        self.split();result=self.run_merge()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        merged=self.output/self.archive.name
        self.assertEqual(merged.read_bytes(),self.original)
        with zipfile.ZipFile(merged) as archive:self.assertIsNone(archive.testzip())
        self.assertEqual(self.run_merge().returncode,1)
        self.assertEqual(merged.read_bytes(),self.original)
        self.assertFalse(list(self.output.glob("*.partial-*")))
    @unittest.skipUnless(os.name=="nt" and shutil.which("powershell.exe"),"Windows PowerShell merge is verified on Windows")
    def test_real_powershell_corrupt_part_stops_and_cleans_own_partial(self):
        self.split();part=self.output/self.manifest()["parts"][2]["name"]
        blob=part.read_bytes();part.write_bytes(bytes([blob[0]^1])+blob[1:])
        result=self.run_merge()
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertFalse((self.output/self.archive.name).exists())
        self.assertFalse(list(self.output.glob("*.partial-*")))
        self.assertTrue(part.exists())
    @unittest.skipUnless(os.name=="nt" and shutil.which("powershell.exe"),"Windows PowerShell merge is verified on Windows")
    def test_real_powershell_invalid_order_and_total_hash_are_rejected(self):
        self.split();original=self.manifest()
        changed=json.loads(json.dumps(original));changed["parts"].reverse();self.save_manifest(changed)
        self.assertEqual(self.run_merge().returncode,1)
        changed=json.loads(json.dumps(original));changed["archive_sha256"]="0"*64;self.save_manifest(changed)
        self.assertEqual(self.run_merge().returncode,1)
        self.assertFalse((self.output/self.archive.name).exists())
        self.assertFalse(list(self.output.glob("*.partial-*")))

if __name__=="__main__":unittest.main()
