"""Pure mocks and temporary files for cancellation/OS-denial audit regressions."""
from __future__ import annotations
import asyncio
import contextlib
import contextvars
import importlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_IMPORT_ROOT=tempfile.TemporaryDirectory(prefix="v15-audit-import-")
if "app.config" not in sys.modules:
    os.environ["LOCAL_AI_ROOT"]=_IMPORT_ROOT.name
os.environ["LOCAL_AI_DISABLE_GPU_PROBE"]="1"
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.api import upgrade
from app import providers,resources,jobs
search_module=importlib.import_module("app.toolkit.search_files")


class RagTestCancellationTests(unittest.IsolatedAsyncioTestCase):
    def router(self,slot,allowed=True):
        context=SimpleNamespace(network=SimpleNamespace(can_manage=lambda request:allowed),
            settings=lambda:{"provider":"local","default_model":"synthetic"},
            providers=SimpleNamespace(queue_key=lambda provider:"synthetic"),jobs=SimpleNamespace(slot=slot))
        return next(route.endpoint for route in upgrade.build_router(context).routes if route.path=="/api/rag-query/test")

    async def test_visible_test_job_cancellation_reaches_preparer_and_releases_slot(self):
        stop,entered=asyncio.Event(),asyncio.Event()
        released=[]
        @contextlib.asynccontextmanager
        async def slot(key,label):
            self.assertEqual(key,"synthetic")
            try:yield {"stop":stop}
            finally:released.append(True)
        async def prepare(text,opts,**kwargs):
            self.assertIs(kwargs["stop"],stop)
            self.assertTrue(kwargs["force"])
            entered.set()
            await kwargs["stop"].wait()
            raise asyncio.CancelledError()
        endpoint=self.router(slot)
        with patch.object(upgrade.rag_query,"prepare",prepare):
            task=asyncio.create_task(endpoint({"query":"合成取消验收"},SimpleNamespace()))
            await asyncio.wait_for(entered.wait(),.5)
            stop.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,.5)
        self.assertEqual(released,[True])

    async def test_reentrant_slot_without_new_job_remains_compatible(self):
        captured=[]
        @contextlib.asynccontextmanager
        async def slot(key,label):yield None
        async def prepare(text,opts,**kwargs):
            captured.append(kwargs["stop"])
            return {"status":"extracted","original":text}
        with patch.object(upgrade.rag_query,"prepare",prepare):
            value=await self.router(slot)({"query":"合成问题"},SimpleNamespace())
        self.assertEqual(captured,[None])
        self.assertEqual(value["query_info"]["status"],"extracted")

    async def test_unmanaged_request_cannot_start_test_job(self):
        def slot(*args,**kwargs):raise AssertionError("Unauthorized job was started")
        with self.assertRaisesRegex(ValueError,"管理权限"):
            await self.router(slot,False)({"query":"合成问题"},SimpleNamespace())


class FakeEntry:
    def __init__(self,root,name,denied=False):
        self.name=name;self.path=str(root/name);self.denied=denied
    def is_dir(self):return False
    def stat(self):
        if self.denied:raise PermissionError("synthetic item metadata denied")
        return SimpleNamespace(st_size=1)


class EmbeddingQueuedFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_gpu_embedding_rechecks_pause_after_prior_failure(self):
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def request(model,texts,device,size):
            calls.append((texts[0],device))
            if texts[0]=="A" and device=="gpu":
                entered.set()
                await release.wait()
                raise ValueError("synthetic GPU request failed")
            await asyncio.sleep(0)
            return [[1.,0.]]
        state={"requested_device":"cpu","used_device":None,"fallback":False,"reason":"",
               "gpu_paused":False,"preference":None,"sampled_at":None}
        with contextlib.ExitStack() as stack:
            for module,name,value in ((providers,"settings",lambda:{"embedding_device":"gpu"}),
                    (providers,"EMBED_STATE",state),(providers,"embedding_gpu_reason",lambda size:""),
                    (providers,"_embed_request",request),(resources,"check_fault",lambda key:None),
                    (jobs,"JOBS",{}),(jobs,"LOCKS",{}),(jobs,"settings",lambda:{"queue_limit":20}),
                    (jobs,"MAINTENANCE",threading.Event()),(jobs,"OWNER",contextvars.ContextVar("synthetic_embedding_owner",default=None))):
                stack.enter_context(patch.object(module,name,value))
            first=asyncio.create_task(providers.embed("synthetic",["A"]))
            await asyncio.wait_for(entered.wait(),1)
            second=asyncio.create_task(providers.embed("synthetic",["B"]))
            for _ in range(100):
                if any(row["key"]==providers.GPU_QUEUE and row["status"]=="queued" for row in jobs.JOBS.values()):break
                await asyncio.sleep(0)
            self.assertTrue(any(row["key"]==providers.GPU_QUEUE and row["status"]=="queued" for row in jobs.JOBS.values()))
            release.set()
            result=await asyncio.wait_for(asyncio.gather(first,second),2)
            final=providers.embedding_status()
        self.assertEqual(result,[[[1.,0.]],[[1.,0.]]])
        self.assertEqual(calls,[("A","gpu"),("A","cpu"),("B","cpu")])
        self.assertTrue(final["gpu_paused"] and final["fallback"])
        self.assertEqual(final["used_device"],"cpu")


class FakeScan:
    def __init__(self,entries):self.entries=entries
    def __enter__(self):return iter(self.entries)
    def __exit__(self,*args):return False


class SearchDenialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="v15-search-audit-")
        # Windows TEMP can use an 8.3 alias (e.g. RUNNER~1). The real reader
        # resolves that alias; mock entry paths must use the same canonical root.
        self.root=Path(self.temp.name).resolve()
        self.depth=1
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.object(search_module,"tool_values",lambda ident:{"search_depth":self.depth}))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)

    def search(self,**kwargs):
        return search_module.search_files({"path":str(self.root),"query":"match",**kwargs})

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 path integration requires Windows")
    def test_real_windows_short_path_search_keeps_relative_match(self):
        import ctypes
        from ctypes import wintypes

        get_short_path=ctypes.WinDLL("kernel32",use_last_error=True).GetShortPathNameW
        get_short_path.argtypes=(wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD)
        get_short_path.restype=wintypes.DWORD
        required=get_short_path(str(self.root),None,0)
        if not required:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer=ctypes.create_unicode_buffer(required)
        written=get_short_path(str(self.root),buffer,len(buffer))
        if not written:
            raise ctypes.WinError(ctypes.get_last_error())
        self.assertLess(written,len(buffer),"GetShortPathNameW buffer unexpectedly grew")
        short=Path(buffer.value)
        if os.path.normcase(str(short))==os.path.normcase(str(self.root)):
            self.skipTest("Temporary volume does not provide a distinct Windows 8.3 directory path")
        self.assertEqual(short.resolve(),self.root)
        match=self.root/"synthetic-match.txt"
        match.write_text("SYNTHETIC short-path search fixture",encoding="utf-8")

        # Exercise the actual directory enumerator and product path resolver.
        result=self.search(path=str(short))

        self.assertEqual(result["count"],1)
        self.assertEqual(len(result["items"]),1)
        self.assertEqual(result["items"][0]["relative_path"],match.name)
        self.assertEqual(Path(result["items"][0]["path"]).resolve(),match)
        self.assertEqual(Path(result["path"]),self.root)
        self.assertFalse(result["partial"] or result["truncated"])

    def test_start_directory_permission_denial_is_error_not_empty_success(self):
        with patch.object(search_module.os,"scandir",side_effect=PermissionError("synthetic root denied")):
            with self.assertRaisesRegex(ValueError,"搜索起点.*权限") as raised:self.search()
        self.assertIsInstance(raised.exception.__cause__,PermissionError)

    def test_other_start_directory_io_failure_is_reported(self):
        with patch.object(search_module.os,"scandir",side_effect=OSError("synthetic enumeration failed")):
            with self.assertRaisesRegex(ValueError,"搜索起点无法读取"):self.search()

    def test_child_denial_preserves_accessible_matches_and_marks_partial(self):
        denied=self.root/"denied";denied.mkdir()
        good=self.root/"good";good.mkdir();(good/"match.txt").write_text("synthetic",encoding="utf-8")
        original=search_module.os.scandir
        def scan(path):
            if Path(path)==denied:raise PermissionError("synthetic child denied")
            return original(path)
        with patch.object(search_module.os,"scandir",scan):result=self.search()
        self.assertEqual(result["count"],1)
        self.assertEqual(result["items"][0]["name"],"match.txt")
        self.assertEqual(result["denied_count"],1)
        self.assertEqual(result["warnings"][0]["error"],"PermissionError")
        self.assertEqual(result["warnings"][0]["path"],str(denied))
        self.assertTrue(result["partial"] and result["truncated"])
        self.assertFalse(result["warnings_truncated"])

    def test_many_denied_children_have_bounded_warnings(self):
        for number in range(30):(self.root/("denied-"+str(number))).mkdir()
        original=search_module.os.scandir
        def scan(path):
            if Path(path)!=self.root:raise PermissionError("synthetic child denied")
            return original(path)
        with patch.object(search_module.os,"scandir",scan):result=self.search()
        self.assertEqual(result["denied_count"],30)
        self.assertEqual(result["warning_count"],30)
        self.assertEqual(len(result["warnings"]),search_module.MAX_WARNINGS)
        self.assertTrue(result["warnings_truncated"])
        self.assertTrue(all(len(row["path"])<=500 for row in result["warnings"]))

    def test_item_metadata_denial_is_visible_and_bounded(self):
        entries=[FakeEntry(self.root,"private-match.dat",True),FakeEntry(self.root,"visible-match.dat")]
        with patch.object(search_module.os,"scandir",return_value=FakeScan(entries)):result=self.search()
        self.assertEqual(result["denied_count"],1)
        self.assertEqual(result["count"],1)
        self.assertEqual(result["items"][0]["name"],"visible-match.dat")

    def test_search_depth_and_pagination_remain_effective(self):
        (self.root/"match-0.txt").write_text("synthetic")
        child=self.root/"child";child.mkdir();(child/"match-1.txt").write_text("synthetic")
        grand=child/"grand";grand.mkdir();(grand/"match-2.txt").write_text("synthetic")
        first=self.search(limit=1);second=self.search(limit=1,offset=1)
        self.assertEqual(first["count"],2)
        self.assertEqual(first["next_offset"],1)
        self.assertIsNone(second["next_offset"])
        self.assertEqual({first["items"][0]["name"],second["items"][0]["name"]},{"match-0.txt","match-1.txt"})
        self.assertFalse(first["partial"] or first["truncated"])
        self.assertEqual(first["warnings"],[])

    def test_match_cap_remains_one_thousand(self):
        entries=[FakeEntry(self.root,f"match-{number:04}.txt") for number in range(1500)]
        with patch.object(search_module.os,"scandir",return_value=FakeScan(entries)):result=self.search()
        self.assertEqual(result["count"],1000)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["visited"],5000)

    def test_scan_cap_remains_five_thousand_without_matches(self):
        entries=[FakeEntry(self.root,f"synthetic-{number:04}.txt") for number in range(6000)]
        with patch.object(search_module.os,"scandir",return_value=FakeScan(entries)):result=self.search()
        self.assertEqual(result["count"],0)
        self.assertEqual(result["visited"],5000)
        self.assertTrue(result["truncated"])


if __name__=="__main__":unittest.main()
