"""1.2 synthetic regressions; model/network/driver execution is mocked here."""
import asyncio
import contextlib
import hashlib
import json
import os
import time
import unittest
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from test_core import ROOT
from app import attachments, chat, context, jobs, knowledge, lifecycle, memory, network, providers, python_lab, resources, store, tools
from app.config import DEFAULTS, VERSION, read_json, save_settings, settings, write_json
from app.server import app


async def fake_embed(model, texts):
    return [[1.0, 0.1, .05, .02] if "轴承" in text else [.1, 1., .03, .02] for text in texts]


class NewVersionTests(unittest.TestCase):
    def setUp(self):
        lifecycle.reset_managed()
        resources.FAULTS.clear()
        jobs.JOBS.clear()
        providers.CAPS.clear()
        network.ATTEMPTS.clear()
        self.client = TestClient(app)

    def text(self, name="synthetic.dat", value="合成轴承：定期补充润滑脂，检查温升。"):
        meta = attachments.upload(name, value.encode())
        return attachments.extract(meta["id"])

    async def build(self, kb):
        ident = store.uid()
        with knowledge.connect() as db:
            db.execute("INSERT INTO builds(id,kb,status,created,updated) VALUES(?,?,'queued',?,?)", (ident, kb, time.time(), time.time()))
        await knowledge.build(kb, ident, asyncio.Event())
        return ident

    def test_factory_defaults_and_once_only(self):
        self.assertFalse(settings()["auto_recovery"])
        self.assertTrue(settings()["gpu_monitor"])
        self.assertTrue(settings()["allow_large_context"])
        self.assertTrue(settings()["memory_enabled"])
        self.assertTrue(settings()["memory_ai_write"])
        self.assertTrue(python_lab.config()["enabled"])
        cfg = store.create(settings())["options"]
        self.assertNotIn("python_run", cfg["tools"])
        self.assertEqual(cfg["rag_mode"], "original")
        self.assertEqual(settings()["embedding_model"], "qwen3-embedding:0.6b")
        memory.update("人工保留测试", memory.read()["revision"])
        self.assertFalse(lifecycle.initialize_version()["reset"])
        self.assertIn("人工保留", memory.read()["text"])

    def test_long_line_cursor_does_not_skip_unreturned_text(self):
        fixture = ROOT / "cache/synthetic-long-line.dat"
        value = "A" * 230 + "\nTAIL"
        fixture.write_text(value, encoding="utf-8")
        save_settings({"roots": [{"id": "qa", "path": str(fixture.parent)}], "tool_read_mode": "native"})
        first = tools.execute("read_file", {"path": str(fixture), "mode": "lines", "max_chars": 100}, ["read_file"])
        self.assertTrue(first["partial_line"])
        self.assertIsNone(first["next_line"])
        rest = tools.execute("read_file", {"path": str(fixture), "mode": "segment", "offset": first["next_offset"], "max_chars": 100000}, ["read_file"])
        self.assertEqual(first["text"] + rest["text"], value)

    def test_active_gateway_jobs_block_restart_and_unload(self):
        job = jobs.enqueue("local", "合成共享请求", kind="gateway")
        for endpoint, body in (("/api/service/restart", {}), ("/api/models/unload", {"model": "synthetic"}), ("/api/ui/unload-all", {})):
            self.assertEqual(self.client.post(endpoint, json=body).status_code, 400)
        self.assertEqual(self.client.put("/api/settings", json={"gateway_enabled": False}).status_code, 400)
        self.assertEqual(self.client.post("/api/ui/stop-all", json={}).status_code, 200)
        self.assertTrue(job["stop"].is_set())
        jobs.finish(job)

    def test_native_truncated_stream_is_an_error_without_replay(self):
        def response(request):
            return httpx.Response(200, content=b'{"message":{"content":"partial"}}\n')
        def client(*args):
            return httpx.AsyncClient(base_url="http://synthetic", transport=httpx.MockTransport(response))
        async def caps(*args):
            return {"tools": True}
        async def exercise():
            return [p async for p in providers.stream_model("local", {"model": "synthetic", "messages": []})]
        with patch.object(providers, "ollama_client", client), patch.object(providers, "capability", caps), patch.object(resources, "admit"):
            with self.assertRaisesRegex(ValueError, "完成标记"):
                asyncio.run(exercise())

    def test_source_context_preserves_nearest_sheet(self):
        text = "[工作表 一]\n型号\t单位\nA\tN\n[工作表 二]\n温度\t结果\n20\t通过\n"
        sections = knowledge.source_sections(text)
        self.assertIn("工作表 二", knowledge.source_context(text, len(text), sections))
        self.assertIn("温度", knowledge.source_context(text, len(text), sections))

    def test_memory_revision_limit_and_ai_switch(self):
        before = memory.read()
        memory.update("# 偏好\n使用毫米和牛顿。", before["revision"], ai=True)
        memory.update("覆盖", before["revision"], ai=True)
        self.assertEqual(memory.read()["text"], "覆盖")
        save_settings({"memory_ai_write": False})
        with self.assertRaisesRegex(ValueError, "关闭"):
            memory.update("覆盖", memory.read()["revision"], ai=True)
        memory.update("人工仍可修改", memory.read()["revision"])
        with self.assertRaisesRegex(ValueError, "上限"):
            memory.update("a" * 20000, memory.read()["revision"])

    def test_memory_fixed_tools_and_context(self):
        self.assertEqual(len(memory.definitions()), 2)
        memory.update("偏好简明回答", memory.read()["revision"])
        conv = store.create(settings())
        store.append(conv, "user", "你好")
        values = context.history(conv, False)
        self.assertTrue(any("运行状态" in m["content"] for m in values))
        self.assertTrue(any("偏好简明回答" in m["content"] for m in values))
        save_settings({"memory_enabled": False})
        self.assertEqual(memory.definitions(), [])
        self.assertFalse(any("偏好简明回答" in m["content"] for m in context.history(conv, False)))

    def test_memory_ai_updates_through_dedicated_executor(self):
        async def run():
            old = await chat.call_tool("memory_read", {}, ["memory_read"], "native")
            self.assertNotIn("revision", old["result"])
            new = await chat.call_tool("memory_update", {"text": "偏好保留工程单位"}, ["memory_update"], "native")
            self.assertTrue(new["success"])
            denied = await chat.call_tool("memory_update", {}, [], "native")
            self.assertFalse(denied["success"])
        asyncio.run(run())
        self.assertIn("工程单位", memory.read()["text"])

    def test_unknown_text_and_binary_rejection(self):
        self.assertEqual(self.text()["status"], "ready")
        for blob in (b"MZ\x00\x00", b"PK\x03\x04", bytes(range(32))):
            with self.assertRaises(ValueError):
                attachments.upload("unknown.dat", blob)
        value = self.text("synthetic.dat", "字段,数值\n压力,10\n")
        content, _ = attachments.content({"content": "分析", "attachments": [attachments.snapshot(value["id"])]})
        self.assertNotIn("不可信", content)

    def test_authorized_absolute_and_chunk_read_cache(self):
        directory = ROOT / "synthetic-read"
        directory.mkdir(exist_ok=True)
        target = directory / "sample.dat"
        target.write_text("\n".join(f"第{i}行：合成记录" for i in range(50)), encoding="utf-8")
        tools.save_tool_configuration("read_file", {"roots": [{"id": "demo", "path": str(directory)}], "tool_read_mode": "native"})
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        first = tools.execute("read_file", {"path": str(target), "max_chars": 100}, ["read_file"])
        second = tools.execute("read_file", {"path": str(target), "offset": first["next_offset"], "max_chars": 100}, ["read_file"])
        self.assertEqual(first["attachment_id"], second["attachment_id"])
        self.assertTrue(first["truncated"])
        whole = tools.execute("read_file", {"root_id": "demo", "relative_path": "sample.dat", "mode": "full"}, ["read_file"])
        self.assertTrue(whole["complete"])
        lines = tools.execute("read_file", {"path": str(target), "mode": "lines", "start_line": 4, "line_count": 2}, ["read_file"])
        self.assertIn("第3行", lines["text"])
        self.assertNotIn("第5行", lines["text"])
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)
        # A separate synthetic file outside the legacy alias is now readable.
        outside = ROOT / "synthetic-outside-alias.dat"
        outside.write_text("SYNTHETIC OUTSIDE", encoding="utf-8")
        self.assertEqual(tools.execute("read_file", {"path": str(outside)}, ["read_file"])["text"], "SYNTHETIC OUTSIDE")
        with self.assertRaises(ValueError):
            tools.execute("read_file", {"path": "not-an-absolute-path"}, ["read_file"])

    def test_search_pagination(self):
        directory = ROOT / "synthetic-search"
        directory.mkdir(exist_ok=True)
        for i in range(12):
            (directory / f"part-{i:02d}.txt").write_text("test")
        tools.save_tool_configuration("search_files", {"roots": [{"id": "demo", "path": str(directory)}], "search_depth": 1})
        one = tools.execute("search_files", {"root_id": "demo", "query": "part-", "limit": 5}, ["search_files"])
        two = tools.execute("search_files", {"root_id": "demo", "query": "part-", "limit": 5, "offset": one["next_offset"]}, ["search_files"])
        self.assertEqual(one["count"], 12)
        self.assertFalse(set(x["name"] for x in one["items"]) & set(x["name"] for x in two["items"]))

    def test_commit_gate_and_no_gpu_probe_when_disabled(self):
        sample = {"ram_available": 8*1024**3, "ram_total": 32*1024**3, "commit_available": 1*1024**3,
                  "commit_used": 31*1024**3, "commit_limit": 32*1024**3}
        with patch.object(resources, "memory_status", return_value=sample):
            with self.assertRaisesRegex(ValueError, "提交"):
                resources.admit()
        save_settings({"gpu_monitor": False})
        with patch.object(resources.subprocess, "run", side_effect=AssertionError("must not probe")):
            resources.hardware()
        with self.assertRaisesRegex(ValueError, "32K"):
            resources.admit(131072)

    def test_connection_retry_keeps_original_options(self):
        save_settings({"auto_recovery": True})
        seen = []
        async def stream(ident, body):
            seen.append(dict(body["options"]))
            if len(seen) == 1:
                raise httpx.ConnectError("synthetic connection failure")
            yield {"message": {"content": "ok"}, "done": True}
        async def run():
            return [x async for x in providers.resilient_stream("local", {"model": "test", "options": {"num_ctx": 8192}}, asyncio.Event())]
        with patch.object(providers, "stream_model", stream):
            asyncio.run(run())
        self.assertEqual(seen, [{"num_ctx": 8192}, {"num_ctx": 8192}])

    def test_fifo_and_cancelled_waiter_releases_no_other_slot(self):
        async def exercise():
            one = jobs.enqueue("test", "one")
            first = await jobs.acquire(one)
            two = jobs.enqueue("test", "two")
            three = jobs.enqueue("test", "three")
            waiting_two = asyncio.create_task(jobs.acquire(two))
            waiting_three = asyncio.create_task(jobs.acquire(three))
            await asyncio.sleep(.02)
            two["stop"].set()
            with self.assertRaises(asyncio.CancelledError):
                await waiting_two
            self.assertTrue(first.locked())
            self.assertFalse(waiting_three.done())
            jobs.finish(one, first)
            last = await asyncio.wait_for(waiting_three, 1)
            jobs.finish(three, last)
            self.assertFalse(last.locked())
        asyncio.run(exercise())

    def test_kb_build_retrieve_delete_and_no_cross_scope(self):
        kb = knowledge.create("合成设备", "机械")
        meta = self.text()
        doc = knowledge.add_document(kb["id"], meta["id"])
        self.assertFalse(doc["duplicate"])
        self.assertTrue(knowledge.add_document(kb["id"], meta["id"])["duplicate"])
        async def exercise():
            await self.build(kb["id"])
            result = await knowledge.search("轴承润滑", [kb["id"]])
            self.assertTrue(result["items"])
            hit = result["items"][0]
            self.assertIn("轴承", knowledge.read_chunk(hit["id"], [kb["id"]])["text"])
            with self.assertRaises(ValueError):
                knowledge.read_chunk(hit["id"], [])
        with patch.object(providers, "embed", fake_embed):
            asyncio.run(exercise())
        self.assertEqual(knowledge.get(kb["id"])["status"], "ready")
        self.assertIn(meta["id"], knowledge.referenced_attachments())
        knowledge.delete_document(kb["id"], doc["id"])
        with knowledge.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chunks").fetchone()[0], 0)

    def test_embedding_change_uses_complete_old_index_until_rebuild(self):
        kb = knowledge.create("合成资料")
        knowledge.add_document(kb["id"], self.text()["id"])
        models = []
        async def tracked(model, texts):
            models.append(model)
            return await fake_embed(model, texts)
        async def exercise():
            await self.build(kb["id"])
            knowledge.update(kb["id"], {"config": {**kb["config"], "model": "qwen3-embedding:4b"}})
            self.assertEqual(knowledge.get(kb["id"])["status"], "needs_rebuild")
            result = await knowledge.search("轴承", [kb["id"]])
            self.assertTrue(result["warnings"])
            self.assertEqual(models[-1], "qwen3-embedding:0.6b")
            await self.build(kb["id"])
            self.assertEqual(knowledge.get(kb["id"])["active_config"]["model"], "qwen3-embedding:4b")
        with patch.object(providers, "embed", tracked):
            asyncio.run(exercise())

    def test_failed_rebuild_does_not_replace_or_accumulate_index(self):
        kb = knowledge.create("合成资料")
        knowledge.add_document(kb["id"], self.text()["id"])
        with patch.object(providers, "embed", fake_embed):
            asyncio.run(self.build(kb["id"]))
        before = knowledge.get(kb["id"])["generation"]
        async def failure(*args):
            raise ValueError("synthetic embedding offline")
        with patch.object(providers, "embed", failure):
            asyncio.run(self.build(kb["id"]))
        self.assertEqual(knowledge.get(kb["id"])["generation"], before)
        with knowledge.connect() as db:
            self.assertEqual(db.execute("SELECT count(DISTINCT generation) FROM chunks").fetchone()[0], 1)

    def test_library_paging_and_parameters(self):
        for i in range(23):
            knowledge.create("合成资料"+str(i), "测试")
        page = knowledge.listing(2)
        self.assertEqual(page["total"], 23)
        self.assertEqual(len(page["items"]), 3)
        with self.assertRaises(ValueError):
            knowledge.create("无效", config={"chunk_tokens": 128, "overlap_tokens": 128})
        pieces = list(knowledge.split_text("合成段落一。\n"*300, 128, 16))
        self.assertGreater(len(pieces), 1)
        self.assertEqual(pieces[0][1], 0)
        self.assertEqual(pieces[-1][2], len("合成段落一。\n"*300))

    def test_password_remembered_session_and_logout(self):
        self.client.post("/api/auth/password", json={"password": "synthetic-password"}).raise_for_status()
        public = self.client.get("/api/bootstrap")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(set(public.json()), {"version", "authenticated", "can_manage", "login_required", "auth"})
        self.assertTrue(public.json()["login_required"])
        self.assertEqual(self.client.get("/api/settings").status_code, 401)
        login = self.client.post("/api/auth/login", json={"password": "synthetic-password", "remember": True})
        self.assertEqual(login.status_code, 200)
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertIn("Max-Age", login.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/bootstrap").status_code, 200)
        self.assertIn("settings", self.client.get("/api/bootstrap").json())
        self.assertNotIn("token", self.client.get("/api/bootstrap").json())
        self.assertNotIn("synthetic-password", (ROOT / "config/auth.json").read_text())
        self.client.post("/api/auth/logout")
        public = self.client.get("/api/bootstrap")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(set(public.json()), {"version", "authenticated", "can_manage", "login_required", "auth"})
        self.assertTrue(public.json()["login_required"])
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_remote_management_is_blocked(self):
        save_settings({"web_bind": "0.0.0.0"})
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.168.1.42", 1234)), base_url="http://192.168.1.3:8765") as client:
                boot = (await client.get("/api/bootstrap")).json()
                self.assertFalse(boot["local_access"])
                self.assertEqual((await client.post("/api/tools/test", json={"name": "python_run", "arguments": {"code": "print(1)"}})).status_code, 403)
                self.assertEqual((await client.get("/api/settings")).status_code, 403)
                for endpoint, body in (("/api/factory-reset", {"confirm": "初始化"}), ("/api/models/unload", {"model": "synthetic"}), ("/api/downloads", {"model": "synthetic"})):
                    self.assertEqual((await client.post(endpoint, json=body)).status_code, 403)
                self.assertEqual((await client.request("DELETE", "/api/models", json={"model": "synthetic", "confirm": "synthetic"})).status_code, 403)
                self.assertEqual((await client.get("/api/conversations")).status_code, 200)
        asyncio.run(exercise())

    def test_manual_reset_also_clears_finished_queue_metadata(self):
        job = jobs.enqueue("local", "synthetic finished")
        jobs.finish(job)
        providers.DOWNLOADS["synthetic"] = {"done": True, "model": "synthetic"}
        response = self.client.post("/api/factory-reset", json={"confirm": "初始化"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(jobs.JOBS, {})
        self.assertEqual(providers.DOWNLOADS, {})

    def test_cleanup_streams_conversations_instead_of_fetching_all(self):
        for _ in range(3):
            conv = store.create(settings())
            store.append(conv, "user", "合成清理引用检查" * 1000)
            store.put(conv)
        original = store.connect
        class StreamOnly:
            def __init__(self, cursor):
                self.cursor = cursor
            def __iter__(self):
                return iter(self.cursor)
            def fetchall(self):
                raise AssertionError("Cleanup must not load every payload into memory")
        class Database:
            def __init__(self):
                self.db = original()
            def __enter__(self):
                self.db.__enter__()
                return self
            def __exit__(self, *args):
                return self.db.__exit__(*args)
            def execute(self, sql, *args):
                cursor = self.db.execute(sql, *args)
                return StreamOnly(cursor) if sql == "SELECT id,payload FROM conversations" else cursor
        with patch.object(store, "connect", Database):
            self.assertTrue(lifecycle.cleanup()["enabled"])
        self.assertEqual(store.listing_page()["total"], 3)

    def test_cleanup_keeps_historical_file_tool_attachment(self):
        meta = self.text()
        conv = store.create(settings())
        store.append(conv, "assistant", "合成文件读取记录", interactions=[{"name": "read_file", "result": {"attachment_id": meta["id"]}}])
        store.put(conv)
        folder = ROOT / "data/attachments" / meta["id"]
        old = time.time() - 9*86400
        os.utime(folder, (old, old))
        lifecycle.cleanup()
        self.assertTrue(folder.exists(), "A file-tool result referenced by chat must not expire")

    def test_access_log_paging_and_bound(self):
        save_settings({"access_retention_count": 100})
        for i in range(105):
            network.record("192.168.1.2", "/api/chat", "synthetic-model", 200, .1, {"eval_count": 3})
        self.assertEqual(network.access_logs()["total"], 100)
        self.assertEqual(len(network.access_logs(2)["items"]), 50)
        self.assertEqual(network.access_logs()["items"][0]["output_tokens"], 3)
        network.clear_logs()
        self.assertEqual(network.access_logs()["total"], 0)

    def test_gateway_auth_whitelist_stream_and_source_ip(self):
        key = network.rotate_gateway_key()["key"]
        gateway_client = TestClient(network.gateway)
        self.assertEqual(gateway_client.get("/api/tags").status_code, 401)
        gateway_client.headers.update({"Authorization": "Bearer " + key, "X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(gateway_client.delete("/api/delete").status_code, 403)
        def response(request):
            return httpx.Response(200, json={"models": []})
        def client(*args):
            return httpx.AsyncClient(base_url="http://synthetic", transport=httpx.MockTransport(response))
        with patch.object(providers, "local_client", client):
            self.assertEqual(gateway_client.get("/api/tags").status_code, 200)
        last = network.access_logs()["items"][0]
        self.assertEqual(last["ip"], "testclient")
        self.assertNotIn(key, json.dumps(network.access_logs()))

    def test_cloud_lan_endpoint_validation(self):
        with self.assertRaises(ValueError):
            save_settings({"providers": [{"id": "cloud", "name": "bad", "base_url": "http://example.com/v1"}]})
        cfg = save_settings({"providers": [{"id": "lan", "name": "借用主机", "kind": "ollama", "base_url": "http://192.168.1.3:11434"}]})
        self.assertEqual(cfg["providers"][0]["kind"], "ollama")
        self.assertTrue(providers.is_ollama("lan"))

    def test_deepseek_thinking_fields_and_round_trip(self):
        save_settings({"providers": [{"id": "deep", "name": "DeepSeek", "base_url": "https://synthetic.invalid/v1", "thinking_mode": "deepseek"}]})
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content='data: {"choices":[{"delta":{"reasoning_content":"合成思考","content":"答案"}}]}\n\ndata: [DONE]\n\n'.encode())
        def client(*args):
            return httpx.AsyncClient(base_url="https://synthetic.invalid/v1/", transport=httpx.MockTransport(respond))
        async def exercise():
            return [x async for x in providers.stream_model("deep", {"model": "synthetic", "think": True,
                "messages": [{"role": "assistant", "content": "工具前", "thinking": "先检索"}], "options": {"temperature": .7, "num_predict": 128}})]
        with patch.object(providers, "cloud_client", client):
            packets = asyncio.run(exercise())
        self.assertEqual(captured[0]["thinking"]["type"], "enabled")
        self.assertNotIn("temperature", captured[0])
        self.assertEqual(captured[0]["messages"][0]["reasoning_content"], "先检索")
        self.assertEqual(packets[0]["message"]["thinking"], "合成思考")

    def test_reset_clears_memory_kb_auth_but_preserves_all_models(self):
        markers = [ROOT / "models" / name for name in ("synthetic-9b", "synthetic-4b", "synthetic-embedding")]
        for path in markers:
            path.write_text("synthetic")
        memory.update("测试记忆", memory.read()["revision"])
        knowledge.create("测试库")
        network.set_password("synthetic-password")
        lifecycle.reset_managed()
        self.assertEqual(memory.read()["text"], "")
        self.assertEqual(knowledge.listing()["total"], 0)
        self.assertFalse(network.auth_config().get("password"))
        self.assertTrue(all(path.exists() for path in markers))

    def test_chat_queue_cancels_waiter_without_interrupting_current(self):
        entered, release = None, None
        calls = []
        async def capability(*args):
            return {"vision": False, "tools": True, "max_context": 32768}
        async def stream(ident, body, stop):
            calls.append(body["model"])
            entered.set()
            await release.wait()
            yield {"message": {"content": "合成回答"}, "done": True, "eval_count": 3, "eval_duration": 100000000}
        async def exercise():
            nonlocal entered, release
            entered, release = asyncio.Event(), asyncio.Event()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
                one = (await client.post("/api/conversations")).json()["id"]
                two = (await client.post("/api/conversations")).json()["id"]
                first = asyncio.create_task(client.post("/api/chat/"+one, json={"content": "合成一"}))
                await asyncio.wait_for(entered.wait(), 2)
                second = asyncio.create_task(client.post("/api/chat/"+two, json={"content": "合成二"}))
                for _ in range(50):
                    if two in chat.ACTIVE:
                        break
                    await asyncio.sleep(.02)
                self.assertIn(two, chat.ACTIVE)
                self.assertEqual(len(calls), 1)
                await client.post("/api/chat/"+two+"/stop")
                cancelled = await asyncio.wait_for(second, 3)
                self.assertIn('"cancelled": true', cancelled.text)
                self.assertFalse(first.done())
                release.set()
                completed = await asyncio.wait_for(first, 3)
                self.assertIn("合成回答", completed.text)
                self.assertFalse(chat.ACTIVE)
        with patch.object(providers, "capability", capability), patch.object(providers, "resilient_stream", stream):
            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
