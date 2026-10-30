"""1.1 regressions: synthetic input only, no system files or real cloud calls."""
import asyncio
import copy
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from test_core import ROOT, SOURCE_ROOT
from app import attachments, context, lifecycle, providers, python_lab, store, tools
from app.config import DEFAULTS, VERSION, read_json, save_settings, settings, write_json
from app.server import app

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        write_json(ROOT / "config/settings.json", DEFAULTS)
        write_json(ROOT / "config/tools.json", {})
        self.client = TestClient(app)

    def test_text_encoding_and_binary_detection(self):
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "gb18030"):
            text, name = attachments.decode_text("合成记录：尺寸 10.02 mm".encode(encoding))
            self.assertIn("尺寸", text)
        for blob in (b"MZ\x00\x00", b"PK\x03\x04test", b"\x01\x02\x03\x04"):
            with self.assertRaises(ValueError):
                attachments.decode_text(blob)

    def test_attachment_budget_preserves_original(self):
        meta = attachments.upload("synthetic-long.txt", ("虚构试验条件 10 N\n" * 4000).encode())
        meta = attachments.extract(meta["id"])
        conv = store.create(settings())
        user = store.append(conv, "user", "归纳关键条件", attachments=[attachments.snapshot(meta["id"], mode="text")])
        full = user["attachments"][0]["text"]
        report = context.preview(conv, False)
        self.assertLess(report["attachments"][0]["ratio"], 1)
        self.assertGreater(report["attachments"][0]["ratio"], 0)
        self.assertEqual(user["attachments"][0]["text"], full)
        self.assertLessEqual(report["total"], conv["options"]["context"])
        self.assertIn("不能声称已阅读全文", context.history(conv, False)[-1]["content"])
        store.put(conv)
        page = self.client.get('/api/conversations/' + conv["id"]).json()
        self.assertEqual(page["messages"][-1]["attachments"][0]["text"], "")
        snapshot = self.client.get(f'/api/conversations/{conv["id"]}/messages/{user["id"]}/attachments/{meta["id"]}').json()
        self.assertEqual(snapshot["text"], full)

    def test_auto_summary_and_branch_invalidation(self):
        conv = store.create(settings())
        for number in range(12):
            store.append(conv, "user" if number % 2 == 0 else "assistant", "虚构工艺 10 mm，结论待验证。" * 80)
        store.append(conv, "user", "接着分析")
        before = len(conv["messages"])
        async def stream(*args):
            yield {"message": {"content": "虚构工艺：10 mm，结论待验证，须回查原文。"}, "done": True}
        async def run():
            return [p async for p in context.prepare(conv, {"vision": False}, asyncio.Event())]
        with patch.object(providers, "stream_model", stream):
            packets = asyncio.run(run())
        self.assertTrue(any(p["type"] == "phase" for p in packets))
        self.assertEqual(len(conv["messages"]), before)
        self.assertTrue(conv["context_state"]["summary"])
        self.assertLessEqual(packets[-1]["total"], conv["options"]["context"])
        conv["messages"][0]["content"] = "Changed synthetic branch"
        self.assertFalse(context.valid_state(conv, store.path(conv)))

    def test_truncation_and_manual_policy(self):
        conv = store.create(settings()); conv["options"]["context_policy"] = "truncate"
        for n in range(12):
            store.append(conv, "user" if n % 2 == 0 else "assistant", "虚构长历史" * 500)
        store.append(conv, "user", "问题")
        async def run():
            return [p async for p in context.prepare(conv, {"vision": False}, asyncio.Event())]
        packets = asyncio.run(run())
        self.assertEqual(conv["context_state"]["summary"], "")
        self.assertLess(packets[-1]["total"], 8192)
        conv.pop("context_state"); conv["options"]["context_policy"] = "manual"
        with self.assertRaises(ValueError):
            asyncio.run(run())

    def test_server_message_paging(self):
        conv = store.create(settings())
        for n in range(135):
            store.append(conv, "user" if n % 2 == 0 else "assistant", "Synthetic " + str(n))
        store.put(conv)
        first = self.client.get('/api/conversations/' + conv["id"]).json()
        second = self.client.get('/api/conversations/' + conv["id"] + '?offset=50').json()
        self.assertEqual(len(first["messages"]), 50)
        self.assertEqual(first["message_page"]["total"], 135)
        self.assertFalse(set(m["id"] for m in first["messages"]) & set(m["id"] for m in second["messages"]))
        self.assertEqual(len(self.client.get('/api/conversations/' + conv["id"] + '/branches').json()["items"]), 50)

    def test_python_full_syntax_io_and_error(self):
        tools.save_tool_configuration("python_run", {"enabled": True, "timeout": 10, "output_kb": 20})
        code = 'import statistics\nclass Demo:\n def run(self, x):\n  return sum(v*v for v in x)\nprint(Demo().run([1,2,3]))\nprint(input())\n'
        with patch.dict(os.environ, {"SYNTHETIC_PRIVATE_KEY": "SHOULD-NOT-INHERIT"}):
            result = tools.execute("python_run", {"code": code, "stdin": "Synthetic input\n"}, ["python_run"])
        self.assertEqual(result["status"], "finished")
        self.assertIn("14", result["stdout"]); self.assertIn("Synthetic input", result["stdout"])
        result = python_lab.execute({"code": 'import os\nprint(os.environ.get("SYNTHETIC_PRIVATE_KEY", "ABSENT"))'})
        self.assertIn("ABSENT", result["stdout"])
        result = python_lab.execute({"code": "print(1/0)"})
        self.assertEqual(result["status"], "failed"); self.assertIn("ZeroDivisionError", result["stderr"])

    def test_python_timeout_output_and_pagination(self):
        tools.save_tool_configuration("python_run", {"enabled": True, "timeout": 1, "output_kb": 1})
        result = python_lab.execute({"code": "while True: pass"})
        self.assertIn("超时", result["note"])
        result = python_lab.execute({"code": 'while True: print("X"*1000)'})
        self.assertIn("输出", result["note"])
        self.assertLessEqual(len(result["stdout"].encode()), 1024)
        self.assertFalse(python_lab.active_ids())
        self.assertLessEqual(len(python_lab.listing(page_size=1)["items"]), 1)
        with self.assertRaises(ValueError):
            python_lab.folder("../")

    def test_sampling_presets(self):
        opts = {"model": "qwen3.5:9b", "think": False}
        self.assertEqual(context.sampling(opts)["top_p"], .8)
        opts["think"] = True
        self.assertEqual(context.sampling(opts)["temperature"], 1)
        opts["sampling_mode"] = "model"
        self.assertEqual(context.sampling(opts), {})
        opts.update(sampling_mode="custom", temperature=.3)
        self.assertEqual(context.sampling(opts)["temperature"], .3)

    def test_resource_failure_never_cascades_to_cpu(self):
        attempts = []
        async def stream(ident, body):
            attempts.append(body["options"].copy())
            if len(attempts) < 3:
                raise ValueError("GGML_ASSERT(ctx->mem_buffer != NULL) failed")
            yield {"message": {"content": "Synthetic recovery"}, "done": True}
        async def capability(*args):
            return {"details": {"model_info": {"qwen35.block_count": 32}}}
        async def models(*args):
            return {"models": []}
        async def run():
            return [p async for p in providers.resilient_stream("local", {"model": "test", "options": {"num_ctx": 8192}}, asyncio.Event())]
        from app import resources
        resources.FAULTS.clear()
        save_settings({"auto_recovery": True})
        try:
            with patch.object(providers, "stream_model", stream), patch.object(providers, "capability", capability), patch.object(providers, "models", models):
                with self.assertRaisesRegex(ValueError, "GGML_ASSERT"):
                    asyncio.run(run())
            self.assertEqual(len(attempts), 1)
            self.assertNotIn("num_gpu", attempts[0])
            with self.assertRaisesRegex(ValueError, "冷却"):
                resources.check_fault("local")
        finally:
            resources.FAULTS.clear()

    def test_version_reset_once_and_models_preserved(self):
        sentinel = ROOT / "models/synthetic-v11-marker"; sentinel.write_text("synthetic")
        write_json(ROOT / "config/version.json", {"version": "old"})
        save_settings({"theme": "light"})
        lifecycle.initialize_version("test-reset")
        self.assertEqual(settings()["theme"], "dark")
        self.assertTrue(sentinel.exists())
        save_settings({"theme": "light"})
        self.assertFalse(lifecycle.initialize_version("test-reset")["reset"])
        self.assertEqual(settings()["theme"], "light")
        with self.assertRaises(ValueError):
            lifecycle.safe_remove(ROOT, ROOT)

    def test_tool_result_budget_and_cleanup_references(self):
        original = {"text": "虚构结果" * 10000}
        wire, clipped = context.fit_tool_result(original, [{"role": "user", "content": "test"}], 8192, 2048)
        self.assertTrue(clipped); self.assertIn("完整记录", wire)
        self.assertEqual(len(original["text"]), 40000)
        meta = attachments.upload("orphan-synthetic.txt", b"Synthetic orphan")
        attachments.extract(meta["id"])
        orphan = attachments.directory(meta["id"])
        os.utime(orphan, (time.time() - 9 * 86400,) * 2)
        protected = attachments.upload("referenced-synthetic.txt", b"Synthetic reference")
        attachments.extract(protected["id"])
        conv = store.create(settings()); store.append(conv, "user", "Reference", attachments=[attachments.snapshot(protected["id"], mode="text")]); store.put(conv)
        linked = attachments.directory(protected["id"])
        os.utime(linked, (time.time() - 9 * 86400,) * 2)
        lifecycle.cleanup()
        self.assertFalse(orphan.exists()); self.assertTrue(linked.exists())

    def test_first_start_strict_threshold(self):
        async def models(*args):
            return {"models": [{"name": "qwen3.5:9b"}, {"name": "qwen3.5:4b"}]}
        def client(*args):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": {"content": "OK"}})), base_url="http://127.0.0.1:11434")
        for mb, expected in ((10240, "qwen3.5:4b"), (12288, "qwen3.5:9b")):
            write_json(ROOT / "config/first-start.json", {})
            with patch.object(providers, "models", models), patch.object(providers, "hardware", return_value={"gpus": [{"total_mb": mb}]}), patch.object(providers, "local_client", client):
                asyncio.run(providers.first_start())
            self.assertEqual(settings()["default_model"], expected)

if __name__ == "__main__":
    unittest.main(verbosity=2)
