"""Isolated v1.6 tool service tests: real filesystem/HTTP/plugin subprocess."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

_BOOT=tempfile.TemporaryDirectory(prefix="v16-tools-import-")
with patch.dict(os.environ,{"LOCAL_AI_ROOT":_BOOT.name}):
    from app import audit, snapshots, file_operations, tools, config
    from app.toolkit import common
    from app.toolkit.http_request import run as http_run

class Tools16(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="v16-tools-")
        self.root=Path(self.tmp.name)
        for p in ("config/tools","plugins/imported","data","logs"): (self.root/p).mkdir(parents=True,exist_ok=True)
        self.patches=[patch.object(m,"ROOT",self.root) for m in (audit,snapshots,tools,config,common)]
        self.patches.append(patch.dict(os.environ,{"LOCAL_AI_ROOT":str(self.root)}))
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def test_create_collision_preserves_first(self):
        a=file_operations.text_create({"directory":str(self.root),"name":"sample","text":"first","format":"md"})
        b=file_operations.text_create({"directory":str(self.root),"name":"sample","text":"second","format":"md"})
        self.assertNotEqual(a["path"],b["path"]);self.assertEqual(Path(a["path"]).read_text(),"first")
    def test_path_name_rejected(self):
        for name in ("../outside","CON.txt","a:stream","x."):
            with self.assertRaises(ValueError):file_operations.destination(str(self.root),name)
    def test_edit_snapshot_restore(self):
        p=self.root/"sample.txt";p.write_text("old")
        r=file_operations.file_manage({"action":"edit_text","path":str(p),"text":"new"})
        self.assertEqual(p.read_text(),"new")
        result=snapshots.restore(r["snapshot_id"])
        self.assertEqual(p.read_text(),"old");self.assertTrue(result["snapshot_id"])
    def test_snapshot_count_bound(self):
        config.write_json(self.root/"config/tools/file_manage.json",{"snapshot_count":2,"snapshot_mb":1})
        p=self.root/"sample.txt"
        for i in range(4):p.write_text(str(i));snapshots.create(p)
        self.assertEqual(snapshots.listing()["total"],2)
        self.assertEqual(len(list((self.root/"data/snapshots").glob("*.bin"))),2)
    def test_noop_does_not_snapshot(self):
        p=self.root/"same.txt";p.write_text("same")
        self.assertFalse(file_operations.file_manage({"action":"edit_text","path":str(p),"text":"same"})["changed"])
        self.assertEqual(snapshots.listing()["total"],0)
    def test_binary_edit_rejected(self):
        p=self.root/"binary.dat";p.write_bytes(b"\0binary")
        with self.assertRaises(ValueError):file_operations.file_manage({"action":"edit_text","path":str(p),"text":"bad"})
        self.assertEqual(p.read_bytes(),b"\0binary")
    def test_duplicate_call_id_cannot_write_again(self):
        args={"directory":str(self.root),"name":"once","text":"test"}
        scope={"_call_id":"same-id","model":"demo","provider":"local"}
        tools.execute("text_create",args,["text_create"],scope)
        with self.assertRaises(ValueError):tools.execute("text_create",args,["text_create"],scope)
        self.assertEqual(len(list(self.root.glob("once*.md"))),1)
    def test_audit_does_not_store_content(self):
        audit.record("http_request",{"url":"https://example.test/private?token=SECRET","body":"SECRET","headers":{"Authorization":"SECRET"}},
                     {"model":"demo","provider":"cloud"},result={"text":"SECRET"},duration=.2)
        text=json.dumps(audit.listing())
        self.assertNotIn("SECRET",text);self.assertNotIn("/private",text)
        self.assertEqual(audit.listing(model="demo")["total"],1)
    def test_audit_retention(self):
        ident=audit.start("system_time",{})
        with audit._db() as db:db.execute("UPDATE events SET timestamp=? WHERE id=?",(time.time()-366*86400,ident))
        self.assertEqual(audit.listing()["total"],0)
    def test_audit_failure_record(self):
        with self.assertRaises(ValueError):tools.execute("text_create",{"bad":1},["text_create"])
        self.assertEqual(audit.listing()["items"][0]["status"],"error")
    def test_copy_move_and_paging(self):
        p=self.root/"original.txt";p.write_text("copy")
        r=file_operations.file_manage({"action":"copy","path":str(p),"name":"copy.txt"})
        renamed=file_operations.file_manage({"action":"rename","path":r["path"],"name":"renamed.txt"})
        self.assertEqual(Path(renamed["path"]).read_text(),"copy");self.assertTrue(p.exists())
        with self.assertRaises(ValueError):file_operations.file_manage({"action":"copy","path":str(p),"name":"renamed.txt"})
    def test_recycle_failure_has_no_delete_fallback(self):
        p=self.root/"keep.txt";p.write_text("keep")
        with patch.object(file_operations,"recycle",side_effect=ValueError("Unavailable")):
            with self.assertRaises(ValueError):file_operations.file_manage({"action":"recycle","path":str(p)})
        self.assertTrue(p.exists())
    def zip_demo(self):
        out=io.BytesIO()
        source=Path(__file__).resolve().parents[1]/"plugins/examples/tool_interface_demo"
        with zipfile.ZipFile(out,"w") as z:
            for p in source.glob("*"):
                if p.is_file():z.write(p,p.name)
        return out.getvalue()
    def test_plugin_export_import_roundtrip(self):
        tools.import_plugin(self.zip_demo())
        cfg=config.settings();cfg["plugins"]=["tool_interface_demo"];config.write_json(self.root/"config/settings.json",cfg)
        common.save_configuration("tool_interface_demo",{"prefix":"custom"})
        args={"action":"calculate","operation":"multiply","a":6,"b":7}
        self.assertEqual(tools.execute("tool_interface_demo",args,["tool_interface_demo"])["result"],42)
        blob=tools.export_plugin("tool_interface_demo")
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            self.assertNotIn("config",str(z.namelist()))
            self.assertNotIn("custom",z.read("manifest.json").decode())
        # Fresh destination root; no original configuration travels with export.
        other=self.root/"blank";(other/"plugins/imported").mkdir(parents=True)
        (other/"config").mkdir()
        with contextlib.ExitStack() as stack:
            for m in (tools,config,common,audit): stack.enter_context(patch.object(m,"ROOT",other))
            stack.enter_context(patch.dict(os.environ,{"LOCAL_AI_ROOT":str(other)}))
            self.assertEqual(tools.import_plugin(blob)["id"],"tool_interface_demo")
            config.write_json(other/"config/settings.json",{"plugins":["tool_interface_demo"]})
            self.assertEqual(tools.execute("tool_interface_demo",args,["tool_interface_demo"])["result"],42)
        with self.assertRaises(ValueError):tools.import_plugin(blob)
    def test_plugin_schema_and_controlled_failure(self):
        tools.import_plugin(self.zip_demo());config.write_json(self.root/"config/settings.json",{"plugins":["tool_interface_demo"]})
        with self.assertRaises(ValueError):tools.execute("tool_interface_demo",{"action":"unknown"},["tool_interface_demo"])
        with self.assertRaises(ValueError):tools.execute("tool_interface_demo",{"action":"fail"},["tool_interface_demo"])
        self.assertEqual(tools.execute("tool_interface_demo",{"action":"echo","text":"ok"},["tool_interface_demo"])["text"],"示例：ok")
    def test_plugin_path_escape_rejected(self):
        out=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.zip_demo())) as original,zipfile.ZipFile(out,"w") as z:
            for name in original.namelist():z.writestr(name,original.read(name))
            z.writestr("../escape.txt","bad")
        with self.assertRaises(ValueError):tools.import_plugin(out.getvalue())
        self.assertFalse((self.root/"escape.txt").exists())
    def test_builtin_adapter_export_import(self):
        blob=tools.export_plugin("system_time")
        manifest=tools.import_plugin(blob)
        self.assertEqual(manifest["id"],"copy_system_time")
        config.write_json(self.root/"config/settings.json",{"plugins":["copy_system_time"]})
        result=tools.execute("copy_system_time",{},["copy_system_time"])
        self.assertTrue(result)
    def test_real_local_http_and_no_post_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.end_headers()
                self.wfile.write(b"<p>hello</p><script>secret</script>")
            def do_POST(self):
                self.send_response(302);self.send_header("Location","/again");self.end_headers()
        srv=ThreadingHTTPServer(("127.0.0.1",0),Handler);thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
        common.save_configuration("http_request",{"proxy_mode":"none"})
        url="http://127.0.0.1:"+str(srv.server_port)
        try:
            result=http_run({"url":url})
            self.assertIn("hello",result["text"]);self.assertNotIn("secret",result["text"])
            with self.assertRaises(ValueError):http_run({"url":url,"method":"POST","json":{"x":1}})
        finally:srv.shutdown();srv.server_close();thread.join()
if __name__=="__main__":unittest.main()
