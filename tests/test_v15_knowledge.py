"""Synthetic MD sources, revision races, sectioning and retrieval protocol checks."""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="v15-knowledge-import-")
if "app.config" not in sys.modules:
    os.environ["LOCAL_AI_ROOT"] = _IMPORT_ROOT.name
os.environ["LOCAL_AI_DISABLE_GPU_PROBE"] = "1"
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import knowledge, context, providers, kb_documents, rag_chunks
from app.api.knowledge import build_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class CharacterTokenizer:
    def encode(self,text,add_special_tokens=False):
        return SimpleNamespace(ids=list(range(len(text))),offsets=[(i,i+1) for i in range(len(text))])


class KnowledgeV15Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="v15-knowledge-case-")
        self.root=Path(self.temp.name)
        self.cfg={"rag_enabled":True,"embedding_model":"qwen3-embedding:0.6b","rag_top_k":2,
                  "rag_chunk_tokens":128,"rag_overlap_tokens":16,"rag_markdown_headers":True}
        self.stack=contextlib.ExitStack()
        for module,name,value in ((knowledge,"ROOT",self.root),(knowledge,"settings",lambda:self.cfg.copy()),
                                  (knowledge,"RUNNING",{}),(knowledge,"DIRTY",set()),
                                  (context,"tokenizer",lambda *a:CharacterTokenizer())):
            self.stack.enter_context(patch.object(module,name,value))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.kb=knowledge.create("合成机械资料")

    async def build(self,embed=None):
        job=knowledge.uid()
        with knowledge.connect() as db:
            db.execute("INSERT INTO builds(id,kb,status,created,updated) VALUES(?,?,'queued',0,0)",(job,self.kb["id"]))
        async def normal(model,texts):
            return [[1.,0.] for _ in texts]
        with patch.object(providers,"embed",embed or normal):
            changed=await knowledge.build(self.kb["id"],job,asyncio.Event())
        return job,changed

    def new_md(self,text="# 合成夹具\n工况：20Hz\n单位：N/mm\n合成数据101。",name="合成维护说明"):
        return knowledge.create_markdown(self.kb["id"],name,text)

    def test_create_edit_rename_and_stale_revision_is_internal_only(self):
        result=self.new_md()
        doc=result["id"]
        source=knowledge.document_source(self.kb["id"],doc)
        self.assertTrue(source["editable"])
        self.assertEqual(source["name"],"合成维护说明.md")
        self.assertEqual(source["revision"],1)
        path=kb_documents.source_path(self.root,self.kb["id"],doc)
        self.assertEqual(path.read_text(encoding="utf-8"),source["text"])
        updated=knowledge.update_document_source(self.kb["id"],doc,{"name":"新的合成名","text":"# 新版\n只有202。","revision":"ignored"})["document"]
        self.assertEqual(updated["revision"],2)
        self.assertEqual(updated["name"],"新的合成名.md")
        self.assertEqual(updated["text"],path.read_text(encoding="utf-8"))
        listing=knowledge.documents(self.kb["id"])["items"][0]
        self.assertEqual(listing["source_kind"],"markdown")
        self.assertTrue(listing["editable"])

    def test_input_cannot_choose_path_and_ids_cannot_escape(self):
        doc=self.new_md()["id"]
        for patch_body in ({"text":"x","path":"../outside"},{"name":"../outside","text":"x"}):
            with self.assertRaises(ValueError):
                knowledge.update_document_source(self.kb["id"],doc,patch_body)
        with self.assertRaises(ValueError):
            kb_documents.source_path(self.root,self.kb["id"],"../outside")
        self.assertEqual(len(list(self.root.rglob("*.md"))),1)

    def test_invalid_text_does_not_change_saved_source_or_revision(self):
        doc=self.new_md()["id"]
        before=knowledge.document_source(self.kb["id"],doc)
        for text in (None,"nul\x00","x"*2_000_001):
            with self.assertRaises(ValueError):
                knowledge.update_document_source(self.kb["id"],doc,{"text":text})
        self.assertEqual(knowledge.document_source(self.kb["id"],doc),before)

    def test_legacy_document_table_migrates_without_dropping_records(self):
        doc="b"*32
        with knowledge.connect() as db:
            db.execute("DROP TABLE chunks")
            db.execute("DROP TABLE documents")
            db.execute("CREATE TABLE documents(id TEXT PRIMARY KEY,kb TEXT NOT NULL,attachment TEXT NOT NULL,name TEXT NOT NULL,sha TEXT NOT NULL,chars INTEGER NOT NULL,created REAL NOT NULL,UNIQUE(kb,sha))")
            db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)",(doc,self.kb["id"],"a"*32,"synthetic-old.md","old-sha",7,0))
        rows=knowledge.documents(self.kb["id"])["items"]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["id"],doc)
        self.assertEqual(rows[0]["source_kind"],"attachment")
        self.assertEqual(rows[0]["revision"],0)

    def test_atomic_file_failure_leaves_previous_source(self):
        doc=self.new_md()["id"]
        before=knowledge.document_source(self.kb["id"],doc)
        with patch.object(kb_documents.os,"replace",side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                knowledge.update_document_source(self.kb["id"],doc,{"text":"new"})
        self.assertEqual(knowledge.document_source(self.kb["id"],doc),before)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_headers_hierarchy_fences_and_raw_offsets(self):
        text="前言\n# 夹具\n工况：20Hz\n## 弹簧\n```python\n# 不是标题\n```\n### 测量\n数据101\n## 阻尼\n数据202\n"
        chunks=list(rag_chunks.split_chunks(text,128,16,True))
        paths=[c["heading_path"] for c in chunks]
        self.assertIn(["夹具","弹簧","测量"],paths)
        self.assertIn(["夹具","阻尼"],paths)
        self.assertFalse(any("不是标题" in p for p in paths))
        covered=set()
        for chunk in chunks:
            self.assertEqual(text[chunk["start"]:chunk["end"]],chunk["text"])
            covered.update(range(chunk["start"],chunk["end"]))
        self.assertEqual(covered,set(range(len(text))))

    def test_long_sections_keep_heading_table_header_and_conditions(self):
        text="# 合成刚度\n工况：20Hz\n单位：N/mm\n| 编号 | 刚度 |\n| --- | --- |\n"+"| A | 101 |\n"*60
        chunks=list(rag_chunks.split_chunks(text,128,16))
        self.assertGreater(len(chunks),3)
        for chunk in chunks[2:]:
            self.assertIn("合成刚度",chunk["source_context"])
            self.assertIn("编号",chunk["source_context"])
            self.assertIn("20Hz",chunk["source_context"])
            self.assertEqual(chunk["heading_path"],["合成刚度"])

    def test_heading_toggle_and_legacy_worksheet_context(self):
        text="# A\nshort\n## B\nshort\n"
        self.assertEqual(len(list(rag_chunks.split_chunks(text,128,16,False))),1)
        sections=knowledge.source_sections("[工作表 一]\n型号 | 单位\n合成101\n")
        self.assertIn("型号",knowledge.source_context(text,10000,sections))
        before=knowledge.get(self.kb["id"])["revision"]
        value=knowledge.update(self.kb["id"],{"config":{"markdown_headers":False}})
        self.assertEqual(value["status"],"needs_rebuild")
        self.assertEqual(value["revision"],before+1)

    def test_parent_conditions_inherit_but_sibling_conditions_do_not(self):
        text="# 合成装置\n单位：N/mm\n## A\n工况：20Hz\n"+"A101\n"*40+"## B\n"+"B202\n"*40
        chunks=list(rag_chunks.split_chunks(text,128,16))
        b=[c for c in chunks if c["heading_path"]==["合成装置","B"]]
        self.assertTrue(b)
        for chunk in b:
            self.assertIn("N/mm",chunk["source_context"])
            self.assertNotIn("20Hz",chunk["source_context"])

    def test_real_public_embedding_tokenizer_preserves_source_spans(self):
        from tokenizers import Tokenizer
        asset=Path(__file__).resolve().parents[1]/"runtime/tokenizers/qwen3-embedding/tokenizer.json"
        if not asset.is_file():
            self.skipTest("Official tokenizer asset not prepared; optional integration test")
        tok=Tokenizer.from_file(str(asset))
        text=" \n# 合成夹具\n工况：20Hz\n"+"合成数据101 N/mm，含中文、English与🙂。\n"*120
        with patch.object(context,"tokenizer",lambda *a:tok):
            chunks=list(rag_chunks.split_chunks(text,128,16))
        covered=set()
        for chunk in chunks:
            self.assertEqual(chunk["text"],text[chunk["start"]:chunk["end"]])
            covered.update(range(chunk["start"],chunk["end"]))
        self.assertEqual(covered,set(range(len(text))))

    def test_imported_text_can_become_managed_md_without_touching_attachment(self):
        from app import attachments
        attachment="a"*32
        meta={"status":"ready","name":"synthetic-import.md","text":"# 合成导入\n101","sha256":"synthetic-sha"}
        with patch.object(attachments,"load",return_value=meta.copy()):
            doc=knowledge.add_document(self.kb["id"],attachment)["id"]
            source=knowledge.document_source(self.kb["id"],doc)
            self.assertEqual(source["source_kind"],"attachment")
            changed=knowledge.update_document_source(self.kb["id"],doc,{"text":"# 用户修订\n202"})["document"]
        self.assertEqual(changed["source_kind"],"markdown")
        self.assertEqual(changed["text"],"# 用户修订\n202")
        self.assertEqual(meta["text"],"# 合成导入\n101")
        self.assertIn(attachment,knowledge.referenced_attachments())

    def test_completed_index_and_query_info_explicit_keyword_weights(self):
        self.new_md("# 工况\nSYNTHETIC-ABC 20Hz 101 N/mm\n")
        async def scenario():
            await self.build()
            calls=[]
            async def embedding(model,texts):
                calls.extend(texts);return [[1.,0.] for _ in texts]
            with patch.object(providers,"embed",embedding):
                result=await knowledge.search("raw",[self.kb["id"]],query_info={"original":"这个合成件呢？","query":"SYNTHETIC-ABC","keywords":["SYNTHETIC-ABC","20Hz"],"status":"extracted","model":"synthetic","fallback":False,"error":"","seconds":.2,"source":"model_keyword_extraction"})
            self.assertEqual(result["query_info"]["original"],"这个合成件呢？")
            self.assertEqual(result["query_info"]["keywords"],["SYNTHETIC-ABC","20Hz"])
            self.assertIn("Query: SYNTHETIC-ABC",calls[0])
            self.assertEqual(result["weights"],{"vector":.85,"keywords":.15})
            self.assertGreaterEqual(result["elapsed_seconds"],.2)
            self.assertEqual(result["timings"]["query_preparation_seconds"],.2)
            self.assertEqual(result["query_info"]["source"],"model_keyword_extraction")
            self.assertEqual(result["items"][0]["title_path"],["工况"])
            self.assertIn("keyword_score",result["items"][0])
        asyncio.run(scenario())

    def test_build_and_query_persist_embedding_device_snapshot(self):
        self.new_md()
        state={"requested_device":"gpu","used_device":"cpu","fallback":True,"reason":"synthetic precheck","gpu_paused":True,"sampled_at":"synthetic-time"}
        async def scenario():
            with patch.object(providers,"embedding_status",return_value=state.copy()):
                await self.build()
            self.assertEqual(knowledge.get(self.kb["id"])["active_config"]["embedding_runtime"],state)
            self.assertEqual(knowledge.tasks(kb=self.kb["id"])["items"][0]["runtime_info"],state)
            async def embedding(model,texts):return [[1.,0.] for _ in texts]
            with patch.object(providers,"embed",embedding),patch.object(providers,"embedding_status",return_value=state.copy()):
                result=await knowledge.search("合成",[self.kb["id"]])
            self.assertEqual(result["embedding_runs"][self.cfg["embedding_model"]],state)
            self.assertTrue(any("CPU" in warning for warning in result["warnings"]))
        asyncio.run(scenario())

    def test_new_source_keeps_previous_complete_index_until_new_ready(self):
        doc=self.new_md("# 旧版\nSYNTHETIC-OLD 101")["id"]
        async def scenario():
            old,_=await self.build()
            knowledge.update_document_source(self.kb["id"],doc,{"text":"# 新版\nSYNTHETIC-NEW 202"})
            self.assertEqual(knowledge.get(self.kb["id"])["generation"],old)
            with knowledge.connect() as db:
                self.assertIn("SYNTHETIC-OLD",db.execute("SELECT text FROM chunks WHERE generation=?",(old,)).fetchone()[0])
            new,_=await self.build()
            self.assertNotEqual(old,new)
            self.assertEqual(knowledge.get(self.kb["id"])["generation"],new)
            with knowledge.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM chunks WHERE generation=?",(old,)).fetchone()[0],0)
        asyncio.run(scenario())

    def test_delete_during_embedding_cannot_revive_document_or_chunks(self):
        doc=self.new_md()["id"]
        async def scenario():
            entered,release=asyncio.Event(),asyncio.Event()
            async def waiting(model,texts):
                entered.set();await release.wait();return [[1.,0.] for _ in texts]
            task=asyncio.create_task(self.build(waiting))
            await entered.wait()
            knowledge.delete_document(self.kb["id"],doc)
            release.set();await task
            self.assertEqual(knowledge.documents(self.kb["id"])["total"],0)
            with knowledge.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM chunks").fetchone()[0],0)
            self.assertFalse(kb_documents.source_path(self.root,self.kb["id"],doc).exists())
        asyncio.run(scenario())

    def test_obsolete_build_cannot_downgrade_newer_ready_index(self):
        doc=self.new_md("# 旧版\nOLD 101")["id"]
        async def scenario():
            entered,release=asyncio.Event(),asyncio.Event()
            async def waiting(model,texts):
                if any("OLD" in text for text in texts):
                    entered.set();await release.wait()
                return [[1.,0.] for _ in texts]
            old=asyncio.create_task(self.build(waiting))
            await entered.wait()
            knowledge.update_document_source(self.kb["id"],doc,{"text":"# 新版\nNEW 202"})
            new,_=await self.build(waiting)
            release.set();await old
            result=knowledge.get(self.kb["id"])
            self.assertEqual(result["generation"],new)
            self.assertEqual(result["status"],"ready")
        asyncio.run(scenario())

    def test_delete_library_during_embedding_cannot_revive_library(self):
        self.new_md()
        async def scenario():
            entered,release=asyncio.Event(),asyncio.Event()
            async def waiting(model,texts):
                entered.set();await release.wait();return [[1.,0.] for _ in texts]
            task=asyncio.create_task(self.build(waiting))
            await entered.wait();knowledge.delete(self.kb["id"]);release.set();await task
            with self.assertRaises(ValueError):knowledge.get(self.kb["id"])
            with knowledge.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM chunks").fetchone()[0],0)
        asyncio.run(scenario())

    def test_edits_during_async_index_are_coalesced_into_latest_revision(self):
        doc=self.new_md("# 合成\nOLD 101")["id"]
        async def scenario():
            entered,release=asyncio.Event(),asyncio.Event()
            calls=[]
            async def waiting(model,texts):
                calls.extend(texts)
                if any("OLD" in text for text in texts):
                    entered.set();await release.wait()
                return [[1.,0.] for _ in texts]
            with patch.object(providers,"embed",waiting):
                first=knowledge.schedule(self.kb["id"],asyncio.create_task)
                await asyncio.wait_for(entered.wait(),2)
                knowledge.update_document_source(self.kb["id"],doc,{"text":"# 合成\nNEW 202"})
                pending=knowledge.schedule(self.kb["id"],asyncio.create_task)
                self.assertEqual(pending["id"],first["id"])
                self.assertEqual(pending["status"],"queued_changes")
                release.set()
                for _ in range(200):
                    if self.kb["id"] not in knowledge.RUNNING:break
                    await asyncio.sleep(.02)
            self.assertFalse(knowledge.RUNNING)
            self.assertEqual(knowledge.get(self.kb["id"])["status"],"ready")
            with knowledge.connect() as db:
                texts=[r[0] for r in db.execute("SELECT text FROM chunks")]
            self.assertTrue(texts and all("NEW" in text for text in texts))
            self.assertTrue(any("OLD" in text for text in calls) and any("NEW" in text for text in calls))
        asyncio.run(scenario())

    def test_explicit_index_cancel_does_not_automatically_retry(self):
        self.new_md()
        async def scenario():
            entered=asyncio.Event()
            calls=[]
            async def waiting(model,texts):
                calls.append(1);entered.set();await asyncio.Event().wait()
            with patch.object(providers,"embed",waiting):
                knowledge.schedule(self.kb["id"],asyncio.create_task)
                await asyncio.wait_for(entered.wait(),2)
                task=knowledge.RUNNING[self.kb["id"]]["task"]
                knowledge.cancel(self.kb["id"])
                await task
                await asyncio.sleep(.6)
            self.assertFalse(knowledge.RUNNING)
            self.assertEqual(len(calls),1)
            self.assertNotEqual(knowledge.get(self.kb["id"])["status"],"ready")
        asyncio.run(scenario())

    def test_md_api_create_get_edit_delete_schedule_without_models(self):
        app=FastAPI()
        app.include_router(build_router(SimpleNamespace(knowledge=knowledge,background=lambda coro:None)))
        from fastapi.responses import JSONResponse
        @app.exception_handler(ValueError)
        async def invalid(request,error):return JSONResponse({"detail":str(error)},status_code=400)
        with TestClient(app) as client, patch.object(knowledge,"schedule",return_value={"id":"synthetic-job","status":"queued"}) as schedule:
            base="/api/knowledge/"+self.kb["id"]+"/documents"
            created=client.post(base+"/md",json={"name":"合成API","text":"# 合成\n101"})
            self.assertEqual(created.status_code,200)
            doc=created.json()["id"]
            source=client.get(base+"/"+doc+"/source").json()
            self.assertEqual(source["text"],"# 合成\n101")
            updated=client.put(base+"/"+doc+"/source",json={"name":"新合成名","text":"# 合成新版\n202"})
            self.assertEqual(updated.json()["document"]["revision"],2)
            self.assertTrue(updated.json()["job"])
            self.assertEqual(client.delete(base+"/"+doc).status_code,200)
            self.assertEqual(client.get(base+"/"+doc+"/source").status_code,400)
            self.assertEqual(schedule.call_count,3)


if __name__=="__main__":unittest.main()
