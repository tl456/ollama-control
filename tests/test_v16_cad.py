"""Synthetic-only CAD adapter tests. Real CAD installation/license checks are separate."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
PLUGIN = SOURCE / "plugins/examples/cad_reader"

def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

cad = load_module("test_cad_adapter", "tool.py")
builder = load_module("test_cad_zip", "build_plugin.py")

class CADAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cad-synthetic-test-")
        self.folder = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def source(self, extension=".step"):
        result = self.folder / ("synthetic" + extension)
        result.write_bytes(b"Synthetic test fixture; not CAD geometry")
        return result
    def test_copy_paging_source_hash_and_cleanup(self):
        source = self.source()
        original = source.read_bytes()
        copies = []
        def read(working, settings, folder, page, size):
            self.assertNotEqual(working, source)
            self.assertEqual(working.read_bytes(), original)
            self.assertEqual((page,size), (3,7))
            copies.append(working)
            return {"ok":True,"backend":"nx","total_bodies":0}
        with patch.object(cad,"nx_read",side_effect=read):
            result=cad.run({"action":"inspect","path":str(source),"page":3},{"settings":{"page_size":7}})
        self.assertTrue(result["source_unchanged"])
        self.assertEqual(result["source"]["sha256"],cad.digest(source))
        self.assertEqual(source.read_bytes(),original)
        self.assertFalse(copies[0].exists())
    def test_changed_source_invalidates_read(self):
        source=self.source()
        def read(*args):
            source.write_bytes(b"Changed by synthetic external editor")
            return {"ok":True}
        with patch.object(cad,"nx_read",side_effect=read):
            with self.assertRaisesRegex(ValueError,"外部修改"):
                cad.run({"action":"inspect","path":str(source)},{})
    def test_path_extension_backend_and_action_validation(self):
        for args in ({"action":"wrong"},{"action":"inspect","path":"relative.step"},
                     {"action":"inspect","path":str(self.source(".txt"))},
                     {"action":"inspect","path":str(self.source()),"backend":"caxa"}):
            with self.assertRaises(ValueError):cad.run(args,{})
    def test_size_limit_before_backend(self):
        source=self.source()
        source.write_bytes(b"x"*(1024*1024+1))
        with patch.object(cad,"nx_read") as read:
            with self.assertRaisesRegex(ValueError,"大小上限"):
                cad.run({"action":"inspect","path":str(source)},{"settings":{"max_file_mb":1}})
            read.assert_not_called()
    def test_bad_copy_not_passed_to_cad(self):
        source=self.source()
        with patch.object(cad,"digest",side_effect=["before","different"]),patch.object(cad,"nx_read") as read:
            with self.assertRaisesRegex(ValueError,"副本字节校验失败"):
                cad.run({"action":"inspect","path":str(source)},{})
            read.assert_not_called()
    def test_probe_does_not_claim_license_and_reports_missing(self):
        with patch.object(cad,"nx_install",return_value=None),patch.object(cad,"caxa_registered",return_value=False):
            result=cad.run({"action":"probe"},{})
        self.assertTrue(result["ok"])
        self.assertTrue(all(not b["available"] for b in result["backends"]))
        self.assertTrue(all("未验证" in b["license"] for b in result["backends"]))
    def test_explicit_nx_install(self):
        (self.folder/"NXBIN").mkdir()
        (self.folder/"NXBIN/run_journal.exe").write_bytes(b"Fake executable; never run")
        self.assertEqual(cad.nx_install({"nx_directory":str(self.folder)}),self.folder.resolve())
    def com_environment(self, instance):
        pythoncom=types.SimpleNamespace(CoInitialize=MagicMock(),CoUninitialize=MagicMock(),CLSCTX_INPROC_SERVER=1)
        client=types.SimpleNamespace(DispatchEx=MagicMock(return_value=instance))
        win32com=types.SimpleNamespace(client=client)
        return {"pythoncom":pythoncom,"win32com":win32com,"win32com.client":client},pythoncom,client
    def test_caxa_open_false_is_failure_and_component_closes(self):
        instance=MagicMock();instance.OpenFile.return_value=False
        modules,pythoncom,client=self.com_environment(instance)
        with patch.dict(sys.modules,modules),patch.object(cad,"caxa_registered",return_value=True):
            with self.assertRaisesRegex(ValueError,"未读取成功"):
                cad.caxa_read(self.source(".dwg"),1,10)
        instance.GetHeaderInfo.assert_not_called()
        instance.CloseFile.assert_called_once()
        pythoncom.CoUninitialize.assert_called_once()
        client.DispatchEx.assert_called_once_with("CAXAInfo.DWGInfo",clsctx=1)
    def test_caxa_paging_bound_and_missing_fields(self):
        instance=MagicMock();instance.OpenFile.return_value=True
        instance.GetPaperCount.return_value=1
        instance.GetPaperSizeAndScale.return_value=(297.,210.,"1:1")
        instance.GetHeaderInfo.return_value=(True,1,"字"*50000)
        instance.GetBomHeaderInfo.return_value=(False,0,"")
        instance.GetBomInfo.side_effect=RuntimeError("Unavailable field")
        instance.GetAllTextAndPosInDB.return_value=(["字"*1000]*250,["位置"*100]*250)
        modules,_,_=self.com_environment(instance)
        with patch.dict(sys.modules,modules),patch.object(cad,"caxa_registered",return_value=True):
            result=cad.caxa_read(self.source(".dwg"),2,100)
        self.assertEqual(result["texts"][0]["index"],101)
        self.assertEqual(result["total_texts"],250)
        self.assertEqual(len(result["texts"]),100)
        self.assertTrue(result["texts"][0]["truncated"])
        self.assertIn("title_block",result["truncated_fields"])
        self.assertIn("bom",result["not_obtained"])
        self.assertLess(len(json.dumps(result,ensure_ascii=False).encode("utf-8")),60000)
    def test_nx_no_result_is_not_success(self):
        with patch.object(cad,"nx_install",return_value=self.folder),patch.object(cad,"run_journal"):
            with self.assertRaisesRegex(ValueError,"未把进程退出当作读取成功"):
                cad.nx_read(self.source(),cad.DEFAULTS,self.folder,1,10)
    def test_nx_returned_failure_is_not_success(self):
        (self.folder/"result.json").write_text(json.dumps({"ok":False,"error":"Missing test license"}))
        with patch.object(cad,"nx_install",return_value=self.folder),patch.object(cad,"run_journal"):
            with self.assertRaisesRegex(ValueError,"Missing test license"):
                cad.nx_read(self.source(),cad.DEFAULTS,self.folder,1,10)
    def test_job_assigned_before_process_runs_and_timeout_closes_only_job(self):
        events=[]
        job=MagicMock();job.Close.side_effect=lambda:events.append("close_job")
        process=MagicMock();thread=MagicMock()
        job_api=types.SimpleNamespace(CreateJobObject=lambda *_:job,
            QueryInformationJobObject=lambda *_:{"BasicLimitInformation":{"LimitFlags":0}},
            SetInformationJobObject=lambda *_:None,JobObjectExtendedLimitInformation=9,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=8192,
            AssignProcessToJobObject=lambda *_:events.append("assign"))
        process_api=types.SimpleNamespace(CreateProcess=lambda *args:(process,thread,123,456),
            ResumeThread=lambda *_:events.append("resume"),CREATE_SUSPENDED=4,STARTUPINFO=lambda:None)
        event_api=types.SimpleNamespace(WaitForSingleObject=MagicMock(side_effect=[258,0]),WAIT_TIMEOUT=258)
        with patch.dict(sys.modules,{"win32job":job_api,"win32process":process_api,"win32event":event_api}),patch.object(cad.subprocess,"CREATE_NO_WINDOW",0,create=True):
            with self.assertRaises(TimeoutError):cad.run_journal(["test.exe"],{},self.folder,5)
        self.assertEqual(events,["assign","resume","close_job"])
        process.Close.assert_called_once();thread.Close.assert_called_once()
    def test_zip_contains_original_sources_only(self):
        package=builder.build(self.folder/"cad.zip")
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(set(archive.namelist()),set(builder.FILES))
            self.assertIsNone(archive.testzip())
            for name in archive.namelist():
                blob=archive.read(name)
                self.assertFalse(blob.startswith(b"%TSD-Header"))
                blob.decode("utf-8-sig")
        with self.assertRaises(ValueError):builder.build(package)
    def test_real_plugin_import_execute_export_fresh_import(self):
        with patch.dict(os.environ,{"LOCAL_AI_ROOT":str(self.folder)}):
            from app import tools, config, audit
            from app.toolkit import common
        package=builder.build(self.folder/"cad.zip").read_bytes()
        def at_root(root):
            stack=contextlib.ExitStack()
            for path in ("plugins/imported","config","logs"):(root/path).mkdir(parents=True,exist_ok=True)
            for module in (tools,config,audit,common):stack.enter_context(patch.object(module,"ROOT",root))
            stack.enter_context(patch.dict(os.environ,{"LOCAL_AI_ROOT":str(root)}))
            return stack
        with at_root(self.folder/"first"):
            self.assertEqual(tools.import_plugin(package)["id"],"cad_reader")
            cfg=config.settings();cfg["plugins"]=["cad_reader"];config.write_json(config.ROOT/"config/settings.json",cfg)
            self.assertTrue(tools.execute("cad_reader",{"action":"probe"},["cad_reader"])["read_only"])
            exported=tools.export_plugin("cad_reader")
        with at_root(self.folder/"second"):
            tools.import_plugin(exported)
            cfg=config.settings();cfg["plugins"]=["cad_reader"];config.write_json(config.ROOT/"config/settings.json",cfg)
            result=tools.execute("cad_reader",{"action":"probe"},["cad_reader"])
            self.assertEqual([item["id"] for item in result["backends"]],["caxa","nx"])
            with self.assertRaises(ValueError):tools.import_plugin(exported)
            with self.assertRaises(ValueError):tools.execute("cad_reader",{"action":"invalid"},["cad_reader"])

if __name__=="__main__":unittest.main()
