"""Deterministic SQLite/index races using only synthetic temporary libraries."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="v15-kb-race-import-")
if "app.config" not in sys.modules:
    os.environ["LOCAL_AI_ROOT"] = _IMPORT_ROOT.name
os.environ["LOCAL_AI_DISABLE_GPU_PROBE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import knowledge, context, providers


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=False):
        return SimpleNamespace(ids=list(range(len(text))), offsets=[(i, i + 1) for i in range(len(text))])


class KnowledgeRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v15-kb-race-")
        self.root = Path(self.temp.name)
        self.cfg = {"rag_enabled": True, "embedding_model": "qwen3-embedding:0.6b", "rag_top_k": 2,
                    "rag_chunk_tokens": 128, "rag_overlap_tokens": 16, "rag_markdown_headers": True}
        self.digests = {"qwen3-embedding:0.6b": "synthetic-digest-v1", "qwen3-embedding:4b": "synthetic-digest-4b"}
        self.stack = contextlib.ExitStack()
        for module, name, value in ((knowledge, "ROOT", self.root), (knowledge, "settings", lambda: self.cfg.copy()),
                                    (knowledge, "RUNNING", {}), (knowledge, "DIRTY", set()),
                                    (knowledge, "model_digest", lambda model: self.digests.get(model, model)),
                                    (context, "tokenizer", lambda *args: CharacterTokenizer()),
                                    (providers, "embedding_status", lambda: {"requested_device": "cpu", "used_device": "cpu", "fallback": False})):
            self.stack.enter_context(patch.object(module, name, value))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.kb = knowledge.create("合成并发资料")
        self.doc = knowledge.create_markdown(self.kb["id"], "synthetic", "# 合成旧版\nSYNTHETIC-OLD 101")["id"]

    async def build(self):
        job = knowledge.uid()
        with knowledge.connect() as db:
            db.execute("INSERT INTO builds(id,kb,status,created,updated) VALUES(?,?,'queued',0,0)", (job, self.kb["id"]))
        async def embedding(model, texts):
            return [[1., 0.] for _ in texts]
        with patch.object(providers, "embed", embedding):
            await knowledge.build(self.kb["id"], job, asyncio.Event())
        self.assertEqual(knowledge.get(self.kb["id"])["generation"], job)
        return job

    async def search_while_publishing(self, change):
        old = await self.build()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def embedding(model, texts):
            calls.append((model, list(texts)))
            entered.set()
            await release.wait()
            return [[1., 0.]]
        with patch.object(providers, "embed", embedding):
            task = asyncio.create_task(knowledge.search("SYNTHETIC", [self.kb["id"]]))
            await asyncio.wait_for(entered.wait(), 3)
            try:
                change()
                new = await self.build()
            finally:
                release.set()
            result = await task
        self.assertEqual(len(calls), 1, "A publication must not silently repeat query embedding")
        with knowledge.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chunks WHERE generation=?", (old,)).fetchone()[0], 0)
        return result, old, new

    def test_publish_during_query_embedding_uses_latest_compatible_generation(self):
        async def scenario():
            result, old, new = await self.search_while_publishing(lambda: knowledge.update_document_source(
                self.kb["id"], self.doc, {"text": "# 合成新版\nSYNTHETIC-NEW 202"}))
            self.assertTrue(result["items"])
            self.assertTrue(all(item["index_generation"] == new and "SYNTHETIC-NEW" in item["text"] for item in result["items"]))
            self.assertFalse(any("版本已变化" in warning for warning in result["warnings"]))
        asyncio.run(scenario())

    def test_changed_active_model_warns_without_false_empty_success_or_reembedding(self):
        async def scenario():
            result, _, _ = await self.search_while_publishing(lambda: knowledge.update(
                self.kb["id"], {"config": {"model": "qwen3-embedding:4b"}}))
            self.assertEqual(result["items"], [])
            self.assertTrue(any("版本已变化" in warning and "重新发起检索" in warning for warning in result["warnings"]))
        asyncio.run(scenario())

    def test_changed_active_digest_warns_without_reembedding(self):
        def change():
            self.digests[self.cfg["embedding_model"]] = "synthetic-digest-v2"
            knowledge.update_document_source(self.kb["id"], self.doc, {"text": "# 新摘要版本\nSYNTHETIC-NEW 202"})
        async def scenario():
            result, _, _ = await self.search_while_publishing(change)
            self.assertEqual(result["items"], [])
            self.assertTrue(any("版本已变化" in warning for warning in result["warnings"]))
        asyncio.run(scenario())

    def test_same_model_query_cache_cannot_cross_a_digest_change_between_libraries(self):
        async def scenario():
            await self.build()
            first = self.kb
            second = knowledge.create("第二份合成资料")
            knowledge.create_markdown(second["id"], "second", "# 合成第二库\nSECOND 202")
            self.kb = second
            await self.build()
            self.kb = first
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def embedding(model, texts):
                calls.append(model)
                entered.set()
                await release.wait()
                return [[1., 0.]]
            with patch.object(providers, "embed", embedding):
                task = asyncio.create_task(knowledge.search("SYNTHETIC", [first["id"], second["id"]]))
                await asyncio.wait_for(entered.wait(), 3)
                try:
                    self.digests[self.cfg["embedding_model"]] = "synthetic-digest-v2"
                    await self.build()
                    self.kb = second
                    await self.build()
                finally:
                    self.kb = first
                    release.set()
                result = await task
            self.assertEqual(calls, [self.cfg["embedding_model"]])
            self.assertEqual(result["items"], [])
            self.assertEqual(sum("版本已变化" in warning for warning in result["warnings"]), 2)
        asyncio.run(scenario())

    def test_disabled_during_query_embedding_returns_explicit_warning(self):
        async def scenario():
            result, _, _ = await self.search_while_publishing(lambda: knowledge.update(self.kb["id"], {"enabled": False}))
            self.assertEqual(result["items"], [])
            self.assertTrue(any("停用" in warning for warning in result["warnings"]))
        asyncio.run(scenario())

    def test_read_transaction_keeps_generation_and_chunks_in_one_snapshot(self):
        async def scenario():
            old = await self.build()
            entered, release = threading.Event(), threading.Event()
            real_connect = knowledge.connect
            hit = []
            class ReadBarrier:
                def __init__(self, db):
                    self.db = db
                def __enter__(self):
                    self.db.__enter__()
                    return self
                def __exit__(self, *args):
                    return self.db.__exit__(*args)
                def __getattr__(self, name):
                    return getattr(self.db, name)
                def execute(self, sql, *args):
                    cursor = self.db.execute(sql, *args)
                    if sql == "SELECT * FROM libraries WHERE id=?" and threading.current_thread() is not threading.main_thread() and not hit:
                        hit.append(self.db.in_transaction)
                        entered.set()
                        if not release.wait(4):
                            raise AssertionError("Synthetic read barrier timed out")
                    return cursor
            async def embedding(model, texts):
                return [[1., 0.] for _ in texts]
            with patch.object(knowledge, "connect", side_effect=lambda: ReadBarrier(real_connect())), patch.object(providers, "embed", embedding):
                task = asyncio.create_task(knowledge.search("SYNTHETIC", [self.kb["id"]]))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                try:
                    knowledge.update_document_source(self.kb["id"], self.doc, {"text": "# 合成新版\nSYNTHETIC-NEW 202"})
                    new = await self.build()
                finally:
                    release.set()
                result = await task
            self.assertEqual(hit, [True])
            self.assertTrue(result["items"])
            self.assertTrue(all(item["index_generation"] == old and "SYNTHETIC-OLD" in item["text"] for item in result["items"]))
            self.assertEqual(knowledge.get(self.kb["id"])["generation"], new)
            with real_connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM chunks WHERE generation=?", (old,)).fetchone()[0], 0)
        asyncio.run(scenario())

    def test_concurrent_rename_cannot_overwrite_new_dirty_status_or_config(self):
        asyncio.run(self.build())
        entered, release = threading.Event(), threading.Event()
        real_get = knowledge.get
        errors = []
        def barrier_get(ident):
            value = real_get(ident)
            if threading.current_thread().name == "synthetic-rename" and not entered.is_set():
                entered.set()
                if not release.wait(4):
                    raise AssertionError("Synthetic rename barrier timed out")
            return value
        def rename():
            try:
                knowledge.update(self.kb["id"], {"name": "合成已改名"})
            except BaseException as error:
                errors.append(error)
        with patch.object(knowledge, "get", barrier_get):
            thread = threading.Thread(target=rename, name="synthetic-rename")
            thread.start()
            self.assertTrue(entered.wait(3))
            try:
                knowledge.update_document_source(self.kb["id"], self.doc, {"text": "# 合成新文档\n202"})
                knowledge.update(self.kb["id"], {"config": {"chunk_tokens": 256, "top_k": 7}})
                expected = real_get(self.kb["id"])
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        actual = real_get(self.kb["id"])
        self.assertEqual(actual["name"], "合成已改名")
        self.assertEqual(actual["status"], "needs_rebuild")
        self.assertEqual(actual["config"], expected["config"])
        self.assertEqual(actual["config"]["top_k"], 7)
        self.assertEqual(actual["revision"], expected["revision"])

    def test_update_reads_and_writes_under_one_immediate_transaction(self):
        real_connect = knowledge.connect
        statements = []
        connections = []
        def traced():
            db = real_connect()
            number = len(connections)
            connections.append(number)
            db.set_trace_callback(lambda sql: statements.append((number, sql)))
            return db
        with patch.object(knowledge, "connect", side_effect=traced):
            knowledge.update(self.kb["id"], {"name": "合成事务改名"})
        writer = next(number for number, sql in statements if sql.startswith("UPDATE libraries SET name="))
        sql = [sql for number, sql in statements if number == writer]
        begin = sql.index("BEGIN IMMEDIATE")
        read = next(i for i, statement in enumerate(sql) if statement.startswith("SELECT * FROM libraries WHERE id="))
        write = next(i for i, statement in enumerate(sql) if statement.startswith("UPDATE libraries SET name="))
        self.assertLess(begin, read)
        self.assertLess(read, write)


if __name__ == "__main__":
    unittest.main()
