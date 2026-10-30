"""Synthetic skills CRUD, export/import and immutable queued-rule tests."""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from test_core import ROOT
from app import skills
from app.api.skills import build_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

class SkillCenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.patch=patch.object(skills,"ROOT",Path(self.tmp.name));self.patch.start()
    def tearDown(self):
        self.patch.stop();self.tmp.cleanup()
    def create(self):
        return skills.save({"name":"合成步骤","description":"仅合成测试","instructions":"先核对单位，再执行计算。","requires_tools":["python_run"]})
    def test_only_one_generic_preset(self):
        items=skills.listing()["items"]
        self.assertEqual([x["id"] for x in items],["evidence-summary"])
        self.assertTrue(items[0]["builtin"])
    def test_crud_snapshot_remains_stable_after_edit_delete(self):
        item=self.create()
        old=skills.snapshot([item["id"]])
        edited=skills.save({**item,"instructions":"新的方法"},item["id"])
        self.assertNotEqual(item["revision"],edited["revision"])
        self.assertIn("先核对单位",skills.injection({"skill_snapshot":old}))
        skills.remove(item["id"])
        self.assertIn("先核对单位",skills.injection({"skill_snapshot":old}))
        with self.assertRaises(ValueError):skills.detail(item["id"])
    def test_duplicate_and_zip_roundtrip_are_independent(self):
        original=skills.detail("evidence-summary")
        cloned=skills.duplicate(original["id"])
        blob,name,mime=skills.export(cloned["id"])
        imported=skills.import_file(name,blob)
        self.assertNotEqual(cloned["id"],imported["id"])
        self.assertEqual(imported["instructions"],original["instructions"])
        self.assertEqual(imported["requires_tools"],original["requires_tools"])
        self.assertFalse(imported["builtin"])
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            self.assertEqual(set(z.namelist()),{"manifest.json","SKILL.md","README.md"})
    def test_md_and_limits_and_traversal(self):
        item=skills.import_file("合成规则.md","# 方法\n核对来源".encode())
        self.assertEqual(skills.export(item["id"],"md")[0].decode(),"# 方法\n核对来源")
        with self.assertRaises(ValueError):skills.save({"name":"大规则","instructions":"x"*12001})
        with self.assertRaises(ValueError):skills.detail("../outside")
        with self.assertRaises(ValueError):skills.remove("evidence-summary")
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,"w") as z:z.writestr("../SKILL.md","x")
        with self.assertRaises(ValueError):skills.import_file("bad.zip",stream.getvalue())
        with self.assertRaises(ValueError):skills.import_file('broken.zip',b'not-a-zip')
    def test_pagination_and_search(self):
        for n in range(23):skills.save({"name":f"合成 {n}","instructions":"核对"})
        first=skills.listing(1,"合成");second=skills.listing(2,"合成")
        self.assertEqual((len(first["items"]),len(second["items"]),first["total"]),(20,3,23))
        self.assertEqual(len({x["id"] for x in first["items"]+second["items"]}),23)
    def test_http_export_import_and_delete(self):
        app=FastAPI();app.include_router(build_router())
        with TestClient(app) as client:
            created=client.post("/api/skills",json={"name":"合成HTTP","instructions":"核验"}).json()
            exported=client.get("/api/skills/"+created["id"]+"/export")
            self.assertEqual(exported.status_code,200)
            imported=client.post("/api/skills/import",files={"file":("test.zip",exported.content,"application/zip")})
            self.assertEqual(imported.status_code,200)
            self.assertNotEqual(imported.json()["id"],created["id"])
            self.assertEqual(client.delete("/api/skills/"+created["id"]).status_code,200)
if __name__=="__main__":unittest.main()
