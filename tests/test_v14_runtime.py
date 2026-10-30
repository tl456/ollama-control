"""1.4 runtime contracts: synthetic roots/streams; no driver/model execution."""
import asyncio
import copy
import os
import subprocess
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from test_core import ROOT
from app import chat, context, memory, resources, store
from app.config import DEFAULTS, settings, write_json

ADAPTER = {"name": "NVIDIA Synthetic QA", "total_mb": 12288, "used_mb": None,
           "free_mb": None, "utilization": None, "source": "synthetic"}
SAMPLE = {**ADAPTER, "used_mb": 100, "free_mb": 12188, "utilization": 1}


class Runtime14Tests(unittest.TestCase):
    def setUp(self):
        write_json(ROOT / "config/settings.json", copy.deepcopy(DEFAULTS))
        self.before = copy.deepcopy(resources.GPU_CACHE)
        resources.GPU_CACHE.clear()
        resources.GPU_CACHE.update(items=[], checked=0.0, next=0.0, error="", failures=0,
                                   paused=False, status="waiting", sampled_at=None)
        self.environment = patch.dict(os.environ, {"LOCAL_AI_DISABLE_GPU_PROBE": "0"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        resources.GPU_CACHE.clear()
        resources.GPU_CACHE.update(self.before)

    def test_new_factory_permissions_do_not_increase_initial_context(self):
        value = settings()
        self.assertTrue(value["gpu_monitor"])
        self.assertTrue(value["allow_large_context"])
        self.assertEqual(value["context"], 8192)
        self.assertFalse(value["auto_recovery"])

    def test_non_nvidia_never_queries(self):
        with patch.object(resources, "inventory", return_value=[]), patch.object(resources, "_query_nvidia") as query:
            value = resources.hardware(force=True)
        query.assert_not_called()
        self.assertEqual(value["gpu_monitor_status"], "unavailable")

    def test_disabled_and_environment_paused_are_explicit(self):
        with patch.object(resources, "inventory", return_value=[ADAPTER]), patch.object(resources, "_query_nvidia") as query:
            with patch.dict(os.environ, {"LOCAL_AI_DISABLE_GPU_PROBE": "1"}):
                value = resources.hardware()
                self.assertTrue(value["gpu_monitor_enabled"])
                self.assertEqual(value["gpu_monitor_status"], "disabled_environment")
            write_json(ROOT / "config/settings.json", {**DEFAULTS, "gpu_monitor": False})
            value = resources.hardware()
            self.assertEqual(value["gpu_monitor_status"], "disabled")
            self.assertEqual(value["gpu_monitor_error"], "")
        query.assert_not_called()

    def test_force_obeys_thirty_second_cache(self):
        with patch.object(resources, "inventory", return_value=[ADAPTER]), patch.object(resources, "_query_nvidia", return_value=[SAMPLE]) as query:
            for stamp in (100, 115, 131):
                with patch.object(resources.time, "monotonic", return_value=stamp):
                    value = resources.hardware(force=True)
            self.assertEqual(query.call_count, 2)
            self.assertEqual(value["gpu_monitor_status"], "ready")

    def test_two_failures_pause_until_explicit_resume(self):
        with patch.object(resources, "inventory", return_value=[ADAPTER]), patch.object(resources, "_query_nvidia", side_effect=OSError("synthetic")) as query:
            for stamp, expected in ((100, "backoff"), (200, "backoff"), (401, "paused"), (1000, "paused")):
                with patch.object(resources.time, "monotonic", return_value=stamp):
                    self.assertEqual(resources.hardware(force=True)["gpu_monitor_status"], expected)
            self.assertEqual(query.call_count, 2)
            self.assertEqual(resources.GPU_CACHE["failures"], 2)
        with patch.object(resources, "inventory", return_value=[ADAPTER]), patch.object(resources, "_query_nvidia", return_value=[SAMPLE]) as query:
            with patch.object(resources.time, "monotonic", return_value=1001):
                result = resources.reset_monitor()
                query.assert_not_called()
                self.assertFalse(result["paused"])
                self.assertEqual(resources.hardware()["gpu_monitor_status"], "ready")
            query.assert_called_once()

    def test_timeout_is_backoff_not_recovery_or_reload(self):
        with patch.object(resources, "inventory", return_value=[ADAPTER]), patch.object(resources, "_query_nvidia", side_effect=subprocess.TimeoutExpired("synthetic", 3)):
            value = resources.hardware()
        self.assertEqual(value["gpu_monitor_status"], "backoff")
        self.assertFalse(settings()["auto_recovery"])

    def test_query_parser_and_timeout_no_subprocess_exec(self):
        with patch.object(resources.shutil, "which", return_value="synthetic-nvidia.exe"), patch.object(resources.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='"NVIDIA QA, adapter", 12288, 100, 12188, 1, synthetic\n')) as run:
            self.assertEqual(resources._query_nvidia()[0]["name"], "NVIDIA QA, adapter")
            self.assertEqual(run.call_args.kwargs["timeout"], 3)
        with patch.object(resources.shutil, "which", return_value="synthetic"), patch.object(resources.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="invalid")):
            with self.assertRaises(ValueError):
                resources._query_nvidia()

    def test_large_context_permission_keeps_commit_gate(self):
        sample = {"ram_available": 16 * resources.GIB, "commit_available": 64 * resources.GIB,
                  "commit_used": 16 * resources.GIB, "commit_limit": 80 * resources.GIB}
        with patch.object(resources, "memory_status", return_value=sample):
            resources.admit(102400, allow_large=True)
            with self.assertRaisesRegex(ValueError, "32K"):
                resources.admit(102400, allow_large=False)
        with patch.object(resources, "memory_status", return_value={**sample, "commit_available": resources.GIB}):
            with self.assertRaisesRegex(ValueError, "提交"):
                resources.admit(102400, allow_large=True)

    def test_runtime_snapshot_is_server_time_and_not_a_tool_call(self):
        snapshot = context.runtime_state({"provider": "local"})
        self.assertIsNotNone(datetime.fromisoformat(snapshot["local_time"]).utcoffset())
        self.assertEqual(snapshot["source"], "server")
        self.assertFalse(snapshot["tool_called"])
        with patch.object(memory, "injection", return_value=""):
            prompt = context.runtime_context({"provider": "local"}, snapshot)
        self.assertIn(snapshot["local_time"], prompt[0]["content"])

    def test_read_tool_omits_revision_write_supports_text_only_and_legacy(self):
        async def exercise():
            selected = ["memory_read", "memory_update"]
            read = await chat.call_tool("memory_read", {}, selected, "native")
            self.assertTrue(read["success"])
            self.assertNotIn("revision", read["result"])
            for args in ({"text": "# SYNTHETIC\nfirst"}, {"text": "# SYNTHETIC\nsecond", "revision": "wrong-old-value"}, {"text": ""}):
                result = await chat.call_tool("memory_update", args, selected, "native")
                self.assertTrue(result["success"], result)
                self.assertEqual(memory.read()["text"], args["text"])
        asyncio.run(exercise())

    def test_running_and_final_event_share_id_and_history_is_single_record(self):
        async def stream(ident, body, stop):
            if body["messages"][-1]["role"] != "tool":
                yield {"message": {"tool_calls": [{"function": {"name": "system_time", "arguments": {}}}]}, "done": True}
            else:
                yield {"message": {"content": "Synthetic complete."}, "done": True}
        conv = store.create(settings())
        conv["options"].update(tool_mode="native", tools=["system_time"], model="synthetic")
        store.append(conv, "user", "synthetic time request")
        store.put(conv)
        async def exercise():
            return [p async for p in chat.run(conv, {"tools": True, "vision": False, "max_context": 8192}, asyncio.Event())]
        with patch("app.providers.resilient_stream", stream):
            packets = asyncio.run(exercise())
        begin = next(p for p in packets if p["type"] == "tool_status")
        finish = next(p for p in packets if p["type"] == "tool")
        self.assertEqual(begin["call_id"], finish["call_id"])
        self.assertEqual(begin["status"], "running")
        self.assertEqual(finish["status"], "success")
        assistant = conv["messages"][-1]
        self.assertEqual(len(assistant["interactions"]), 1)
        runtime = next(p for p in packets if p["type"] == "runtime")
        self.assertEqual(runtime["local_time"], assistant["runtime_state"]["local_time"])
        self.assertFalse(assistant["runtime_state"]["tool_called"])
        self.assertIn(runtime["local_time"], conv["_runtime_context"][0]["content"])


if __name__ == "__main__":
    unittest.main()
