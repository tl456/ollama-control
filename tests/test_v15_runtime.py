"""Synthetic runtime boundaries; never starts a service or samples a driver."""
import asyncio
import copy
import hashlib
import io
import json
import logging
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from starlette.requests import Request
from test_core import ROOT, SOURCE_ROOT
from app import config, jobs, lifecycle, network, providers, resources, service_config, store, turn_queue


class RuntimeV15Tests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(ROOT.resolve(), SOURCE_ROOT.resolve())
        self.assertTrue(ROOT.name.startswith("local-ai-test-"))
        lifecycle.reset_managed()
        jobs.JOBS.clear()
        jobs.MAINTENANCE.clear()
        resources.FAULTS.clear()
        providers.reset_embedding_device()
        config.write_json(ROOT / "config/settings.json", {**config.DEFAULTS, "gpu_monitor": False})

    def settings(self, **patches):
        config.write_json(ROOT / "config/settings.json", {**config.settings(), **patches})

    def request(self, ip="192.0.2.1", cookie=""):
        return Request({"type": "http", "method": "GET", "path": "/", "scheme": "http",
            "server": ("192.0.2.2", 8765), "client": (ip, 1234), "headers": [(b"cookie", cookie.encode())]})

    def test_role_ports_and_external_queue_are_distinct(self):
        self.assertEqual((service_config.WEB_PORT, service_config.GATEWAY_PORT, service_config.CHAT_PORT, service_config.EMBED_PORT), (8765, 11436, 11437, 11438))
        self.assertTrue(providers.LOCAL_URL.endswith(":11437"))
        self.assertTrue(providers.EMBED_URL.endswith(":11438"))
        for port, expected in ((11434, False), (11435, False), (11437, True)):
            with patch.object(providers, "provider", return_value={"kind": "ollama", "base_url": f"http://127.0.0.1:{port}"}):
                self.assertEqual(providers.queue_key("synthetic") == "local", expected)

    def test_remote_management_requires_flag_and_optional_password_session(self):
        self.assertTrue(network.can_manage(self.request("127.0.0.1")))
        self.settings(remote_full_access=True)
        self.assertTrue(network.can_manage(self.request()))
        config.write_json(ROOT / "config/auth.json", {"password": {"synthetic": True}})
        self.assertFalse(network.can_manage(self.request()))
        token = "SYNTHETIC-NOT-A-CREDENTIAL"
        with network.connect() as db:
            db.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + 60))
        remote = self.request(cookie=network.COOKIE + "=" + token)
        self.assertTrue(network.can_manage(remote))
        self.assertFalse(network.is_local(remote))
        self.settings(remote_full_access=False)
        self.assertFalse(network.can_manage(remote))

    def test_chat_limit_is_atomic_and_does_not_auto_archive(self):
        def create(_):
            try:
                return store.create(config.settings())["id"]
            except ValueError as error:
                self.assertIn("手动", str(error))
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(create, range(34)))
        self.assertEqual(sum(value is not None for value in result), 30)
        self.assertEqual(store.listing_page()["total"], 30)
        self.assertEqual(store.listing_page(deleted=True)["total"], 0)
        conv = store.get(next(value for value in result if value))
        store.append(conv, "user", "合成现有对话继续使用")
        store.put(conv)
        conv["deleted"] = True
        store.put(conv)
        store.create(config.settings())
        conv["deleted"] = False
        with self.assertRaisesRegex(ValueError, "活动对话已达"):
            store.put(conv)

    def test_trash_has_no_count_limit_and_ttl_uses_deleted_at(self):
        for index in range(45):
            store.put({"id": "synthetic-trash-" + str(index), "title": "合成回收站项目", "deleted": True, "messages": [], "options": {}})
        self.assertEqual(store.listing_page(deleted=True)["total"], 45)
        expired = store.get("synthetic-trash-0")
        expired["deleted_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        # Initial prior timestamps are authoritative; seed the synthetic old
        # record directly, then use the public edit entry to prove TTL stability.
        with store.connect() as db:
            db.execute("UPDATE conversations SET payload=? WHERE id=?", (json.dumps(expired), expired["id"]))
        expired["title"] = "合成回收站改名不能延期"
        store.put(expired)
        self.assertEqual(store.get(expired["id"])["deleted_at"], expired["deleted_at"])
        lifecycle.cleanup()
        with self.assertRaises(KeyError):
            store.get(expired["id"])
        self.assertEqual(store.listing_page(deleted=True)["total"], 44)

    def test_trash_references_protect_old_attachments(self):
        ident = "a" * 32
        target = ROOT / "data/attachments" / ident
        target.mkdir(exist_ok=True)
        (target / "metadata.json").write_text("{}", encoding="utf-8")
        os.utime(target, (time.time() - 9 * 86400,) * 2)
        store.put({"id": "synthetic-keep-reference", "deleted": True, "messages": [{"attachments": [{"id": ident}]}], "options": {}})
        lifecycle.cleanup()
        self.assertTrue(target.is_dir())

    def test_log_ttl_only_removes_owned_names(self):
        old = ROOT / "logs/app.log.1"
        other = ROOT / "logs/SYNTHETIC-KEEP.txt"
        for path in (old, other):
            path.write_text("synthetic log", encoding="utf-8")
            os.utime(path, (time.time() - 8 * 86400,) * 2)
        result = lifecycle.cleanup_logs(7)
        self.assertGreaterEqual(result["removed"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(other.exists())

    def test_child_log_reader_is_bounded_without_launching_process(self):
        target = ROOT / "logs/ollama.log"
        process = SimpleNamespace(stdout=io.BytesIO(b"SYNTHETIC PIPE OUTPUT\n"))
        thread = lifecycle.capture_process_log(process, target)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIn("SYNTHETIC PIPE OUTPUT", target.read_text(encoding="utf-8"))
        handler = lifecycle.ManagedLogHandler(target)
        try:
            handler.day -= timedelta(days=1)
            self.assertTrue(handler.shouldRollover(logging.makeLogRecord({"msg": "synthetic"})))
        finally:
            handler.close()

    def test_cpu_preference_applies_to_plain_auxiliary_requests(self):
        self.settings(inference_device="cpu")
        body = {"model": "synthetic", "options": {"num_ctx": 2048}}
        self.assertEqual(providers.local_device_body("local", body)["options"]["num_gpu"], 0)
        explicit = providers.local_device_body("local", {**body, "inference_device": "auto"})
        self.assertNotIn("num_gpu", explicit["options"])
        self.assertNotIn("inference_device", explicit)

    def test_queue_snapshot_retains_devices_effort_and_query_prompt(self):
        self.settings(inference_device="cpu", rag_query_prompt="合成原提示", rag_query_enabled=True)
        conv = store.create(config.settings())
        body = {"content": "合成输入", "options": {"tools": ["system_time"], "reasoning_effort": "max"}}
        snap = asyncio.run(turn_queue._snapshot(conv, body, True, True))
        self.assertTrue(snap["remote"])
        self.assertFalse(snap["restricted"])
        self.assertEqual(snap["options"]["inference_device"], "cpu")
        self.assertEqual(snap["options"]["reasoning_effort"], "max")
        self.settings(inference_device="auto", rag_query_prompt="合成新提示")
        self.assertEqual(snap["options"]["_generation_settings"]["rag_query_prompt"], "合成原提示")
        self.assertEqual(snap["options"]["rag_query_prompt"], "合成原提示")
        limited = asyncio.run(turn_queue._snapshot(conv, body, True, False))
        self.assertTrue(limited["restricted"])

    def test_inherited_effort_is_visible_and_resolved_server_side(self):
        conv = store.create(config.settings())
        with patch.object(providers, "provider", return_value={"reasoning_effort": "low"}):
            snap = asyncio.run(turn_queue._snapshot(conv, {"content": "合成快照"}, False, True))
        self.assertEqual(snap["options"]["reasoning_effort"], "inherit")
        self.assertEqual(snap["options"]["_resolved_reasoning_effort"], "low")

    def test_new_submission_in_old_conversation_uses_new_global_device(self):
        conv = store.create(config.settings())
        self.settings(inference_device="cpu")
        snap = asyncio.run(turn_queue._snapshot(conv, {"content": "合成CPU请求"}, False, True))
        self.assertEqual(snap["options"]["inference_device"], "cpu")
        explicit = asyncio.run(turn_queue._snapshot(conv, {"content": "合成自动请求", "options": {"inference_device": "auto"}}, False, True))
        self.assertEqual(explicit["options"]["inference_device"], "auto")

    def test_gpu_preflight_reads_only_existing_cache(self):
        with patch.object(resources, "GPU_CACHE", {"items": [{"free_mb": 8000}]}), \
             patch.object(resources, "admit"), patch.object(resources, "hardware") as hardware, \
             patch.object(resources, "_query_nvidia") as query:
            self.assertEqual(providers.embedding_gpu_reason(1024 ** 3), "")
            hardware.assert_not_called()
            query.assert_not_called()
        with patch.object(resources, "GPU_CACHE", {"items": []}):
            self.assertIn("CPU", providers.embedding_gpu_reason(1024 ** 3))

    def test_gpu_failure_falls_back_once_and_stays_paused(self):
        self.settings(embedding_device="gpu")
        async def run():
            calls = []
            async def request(model, texts, device, size):
                calls.append(device)
                if device == "gpu":
                    raise ValueError("SYNTHETIC GPU FAILURE")
                return [[1.0, 0.0]]
            with patch.object(providers, "embedding_gpu_reason", return_value=""), patch.object(providers, "_embed_request", side_effect=request):
                self.assertEqual(await providers.embed("synthetic-embedding", ["合成"]), [[1.0, 0.0]])
                await providers.embed("synthetic-embedding", ["另一次合成"])
            self.assertEqual(calls, ["gpu", "cpu", "cpu"])
            self.assertTrue(providers.embedding_status()["gpu_paused"])
            self.assertTrue(providers.embedding_status()["fallback"])
        asyncio.run(run())

    def test_gpu_embedding_reentrant_in_chat_slot_and_cpu_independent(self):
        async def run():
            async with jobs.slot("local", "synthetic chat"):
                with patch.object(providers, "embedding_gpu_reason", return_value=""), patch.object(providers, "_embed_request", new=AsyncMock(return_value=[[1.0]])):
                    self.settings(embedding_device="gpu")
                    await asyncio.wait_for(providers.embed("synthetic-embedding", ["合成"]), 2)
                    self.settings(embedding_device="cpu")
                    await asyncio.wait_for(providers.embed("synthetic-embedding", ["合成"]), 2)
        asyncio.run(run())

    def test_gpu_embedding_waits_for_other_chat(self):
        self.settings(embedding_device="gpu")
        async def run():
            lock = await jobs.acquire(jobs.enqueue("local", "synthetic chat"))
            request = AsyncMock(return_value=[[1.0]])
            with patch.object(providers, "embedding_gpu_reason", return_value=""), patch.object(providers, "_embed_request", new=request):
                task = asyncio.create_task(providers.embed("synthetic-embedding", ["合成"]))
                await asyncio.sleep(.02)
                request.assert_not_called()
                jobs.finish(next(v for v in jobs.JOBS.values() if v["label"] == "synthetic chat"), lock)
                await asyncio.wait_for(task, 2)
                request.assert_awaited_once()
        asyncio.run(run())

    def test_device_changes_do_not_stop_external_services(self):
        with patch.object(providers, "service_ownership", return_value={"managed": False}), \
             patch.object(providers, "stop_owned_service") as stop, patch.object(providers, "restart_ollama") as restart:
            result = providers.apply_device_preferences({"inference_device": "auto"}, {"inference_device": "cpu"})
            self.assertTrue(result["external_untouched"])
            stop.assert_not_called()
            restart.assert_not_called()
        jobs.JOBS["synthetic"] = {"status": "running", "key": "local"}
        with self.assertRaisesRegex(ValueError, "已有"):
            providers.apply_device_preferences({"inference_device": "auto"}, {"inference_device": "cpu"})
        self.assertFalse(jobs.MAINTENANCE.is_set())

    def test_managed_launch_uses_new_role_and_device_environment_without_start(self):
        executable = str(ROOT / "runtime/SYNTHETIC-ollama.exe")
        for embedding, device, port in ((False, "auto", 11437), (False, "cpu", 11437), (True, "cpu", 11438), (True, "gpu", 11438)):
            with patch.object(providers, "ollama_exe", return_value=executable), \
                 patch.object(providers.httpx, "get", side_effect=httpx.ConnectError("synthetic unavailable")), \
                 patch.object(providers.subprocess, "Popen", return_value=SimpleNamespace(pid=24680)) as popen, \
                 patch.object(providers.psutil, "Process", return_value=Mock(create_time=Mock(return_value=100))), \
                 patch.object(lifecycle, "capture_process_log") as capture, \
                 patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "SYNTHETIC-DEVICE"}):
                record = providers.launch_ollama(embedding, device)
                env = popen.call_args.kwargs["env"]
                self.assertEqual(env["OLLAMA_HOST"], "127.0.0.1:" + str(port))
                self.assertEqual(env["OLLAMA_MODELS"], str(ROOT / "models"))
                self.assertEqual(env["OLLAMA_NUM_PARALLEL"], "1")
                self.assertEqual(record["port"], port)
                self.assertEqual(record["device"], device)
                self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "-1" if embedding and device == "cpu" else "SYNTHETIC-DEVICE")
                capture.assert_called_once()

    def test_old_or_external_role_is_not_terminated_even_with_same_models(self):
        executable = str(ROOT / "runtime/SYNTHETIC-ollama.exe")
        process = Mock(create_time=Mock(return_value=100), exe=Mock(return_value=executable), cmdline=Mock(return_value=[executable, "serve"]))
        process.environ.return_value = {"OLLAMA_MODELS": str(ROOT / "models"), "OLLAMA_HOST": "127.0.0.1:11434"}
        record = {"pid": 24680, "created": 100, "exe": executable, "models": str(ROOT / "models"), "port": 11434}
        with patch.object(providers.psutil, "Process", return_value=process):
            with self.assertRaisesRegex(ValueError, "归属"):
                providers.stop_owned_service(record)
        process.terminate.assert_not_called()

    def test_deepseek_effort_and_reasoning_stream_roundtrip(self):
        async def run(effort, think=True, generic=False, supported=True):
            requests = []
            async def transport(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, text='data: {"choices":[{"delta":{"reasoning_content":"合成思考","content":"合成答复","tool_calls":[{"index":0}]}}],"usage":{"completion_tokens":1}}\n\ndata: [DONE]\n\n')
            @asynccontextmanager
            async def client(*args, **kwargs):
                async with httpx.AsyncClient(base_url="https://synthetic.invalid/", transport=httpx.MockTransport(transport)) as value:
                    yield value
            cfg = {"kind": "cloud", "thinking_mode": "off" if generic else "deepseek", "reasoning_effort": "high", "reasoning_effort_supported": supported}
            with patch.object(providers, "provider", return_value=cfg), patch.object(providers, "cloud_client", client):
                packets = [p async for p in providers.stream_model("synthetic", {"model": "synthetic", "think": think,
                    "messages": [{"role": "user", "content": "合成短文本"}], "options": {"reasoning_effort": effort}})]
            self.assertEqual(packets[0]["message"]["thinking"], "合成思考")
            self.assertTrue(packets[0]["message"]["tool_calls"])
            self.assertEqual(packets[0]["usage"]["completion_tokens"], 1)
            return requests[0]
        for effort in ("off", "low", "high", "max"):
            body = asyncio.run(run(effort))
            if effort == "off":
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertNotIn("reasoning_effort", body)
            else:
                self.assertEqual(body["reasoning_effort"], effort)
        self.assertNotIn("reasoning_effort", asyncio.run(run("max", False)))
        self.assertNotIn("reasoning_effort", asyncio.run(run("max", generic=True, supported=False)))
        self.assertEqual(asyncio.run(run("low", generic=True))["reasoning_effort"], "low")
