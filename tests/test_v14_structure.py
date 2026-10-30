"""Synthetic route compatibility and data-preserving upgrade checks; no GPU."""
import asyncio
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from test_core import ROOT, SOURCE_ROOT
from app import config, knowledge, lifecycle, providers, resources, store, upgrades
from app import server


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(ROOT.resolve(), SOURCE_ROOT.resolve())
        self.assertTrue(ROOT.name.startswith("local-ai-test-"))
        lifecycle.reset_managed()
        self.client = TestClient(server.app)

    def seed_old(self):
        config.write_json(ROOT / "config/version.json", {"version": "1.3.0", "reset_at": 123, "backup": False})
        config.write_json(ROOT / "config/settings.json", {"theme": "light", "context": 16384, "gpu_monitor": False,
                          "allow_large_context": False, "memory_ai_write": False, "synthetic_future_key": {"keep": True}})
        files = {}
        for rel, text in (("data/memory/memory.md", "# 合成偏好\n使用虚构单位。"),
                          ("models/SYNTHETIC-UPGRADE-MODEL", "SYNTHETIC MODEL"),
                          ("runtime/SYNTHETIC-UPGRADE-RUNTIME", "SYNTHETIC RUNTIME"),
                          ("plugins/imported/synthetic_upgrade/tool.py", "# SYNTHETIC ONLY"),
                          ("config/tools.json", '{"synthetic-custom":{"enabled":false}}'),
                          ("config/secrets.json", '{"synthetic":"NOT-A-REAL-CREDENTIAL"}')):
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            files[rel] = hashlib.sha256(target.read_bytes()).hexdigest()
        conv = store.create(config.settings())
        store.append(conv, "user", "合成保留升级会话，无业务内容。")
        store.put(conv)
        return files, conv["id"]

    def test_extracted_routes_unique_and_facade_compatible(self):
        records = []
        for route in server.app.routes:
            for method in getattr(route, "methods", ()):
                records.append((route.path, method))
        self.assertEqual(len(records), len(set(records)))
        wanted = {"/api/settings", "/api/memory", "/api/knowledge", "/api/knowledge/tasks", "/api/auth/login", "/api/runtime"}
        for route in server.app.routes:
            if getattr(route, "path", "") in wanted:
                self.assertTrue(route.endpoint.__module__.startswith("app.api."))
                self.assertIs(getattr(server, route.endpoint.__name__), route.endpoint)
        self.assertEqual(self.client.get("/api/knowledge/tasks").status_code, 200)

    def test_no_password_ui_access_for_extracted_routes(self):
        for route in ("/api/settings", "/api/memory", "/api/knowledge"):
            self.assertEqual(self.client.get(route).status_code, 200)
        with patch.object(resources, "reset_monitor", return_value={"status": "synthetic"}):
            self.assertEqual(self.client.post("/api/runtime/monitor-resume").status_code, 200)

    def test_memory_adapter_accepts_text_and_old_optional_revision(self):
        with patch.object(server.memory, "update", return_value={"ok": True}) as update:
            response = self.client.put("/api/memory", json={"text": "# 合成全文"})
            self.assertEqual(response.status_code, 200)
            update.assert_called_once_with("# 合成全文", None, mode="replace")
            update.reset_mock()
            response = self.client.put("/api/memory", json={"text": "追加合成项", "mode": "append", "revision": "old-invalid"})
            self.assertEqual(response.status_code, 200)
            update.assert_called_once_with("追加合成项", "old-invalid", mode="append")

    def test_monitor_resume_is_host_only_and_does_not_probe(self):
        value = {"status": "ready", "error": "", "failures": 0, "paused": False, "next_probe_seconds": 0}
        with patch.object(resources, "reset_monitor", return_value=value, create=True) as reset, \
                patch.object(providers, "hardware", side_effect=AssertionError("No actual GPU query")):
            self.assertEqual(self.client.post("/api/runtime/monitor-resume").json(), value)
            reset.assert_called_once_with()
        config.save_settings({"web_bind": "0.0.0.0"})

        async def remote():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, client=("192.168.1.42", 1234)),
                                         base_url="http://192.168.1.3:8765") as client:
                boot = (await client.get("/api/bootstrap")).json()
                for endpoint in ("/api/runtime/monitor-resume", "/api/factory-reset"):
                    self.assertEqual((await client.post(endpoint, json={"confirm": "初始化"})).status_code, 403)
                self.assertEqual((await client.get("/api/memory")).status_code, 403)
                self.assertEqual((await client.get("/api/settings")).status_code, 403)
                with patch.object(providers, "is_ollama", return_value=False):
                    response = await client.get("/api/runtime?provider=synthetic-cloud")
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["cloud"])
        asyncio.run(remote())

    def test_missing_memory_text_never_implies_clear(self):
        with patch.object(server.memory, "update", return_value={"ok": True}) as update:
            for body in ({}, {"revision": "old"}, {"text": None}, {"text": False}):
                response = self.client.put("/api/memory", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIn("text", response.json()["detail"])
            update.assert_not_called()
            self.assertEqual(self.client.put("/api/memory", json={"text": ""}).status_code, 200)
            update.assert_called_once_with("", None, mode="replace")

    def test_knowledge_schedule_uses_current_facade_background(self):
        replacement = object()
        with patch.object(server, "background", replacement), patch.object(knowledge, "schedule", return_value={"status": "queued"}) as schedule:
            response = self.client.post("/api/knowledge/synthetic/rebuild")
            self.assertEqual(response.status_code, 200)
            schedule.assert_called_once_with("synthetic", replacement)

    def test_preserve_upgrade_keeps_data_models_runtime_and_saved_choices(self):
        originals, conversation = self.seed_old()
        result = lifecycle.initialize_version("preserve")
        self.assertFalse(result["reset"])
        self.assertTrue(result["upgraded"])
        saved = config.read_json(ROOT / "config/settings.json")
        self.assertTrue(set(config.DEFAULTS) <= set(saved))
        self.assertEqual(saved["theme"], "light")
        self.assertEqual(saved["context"], 16384)
        self.assertFalse(saved["gpu_monitor"])
        self.assertFalse(saved["allow_large_context"])
        self.assertFalse(saved["memory_ai_write"])
        self.assertEqual(saved["synthetic_future_key"], {"keep": True})
        for relative, digest in originals.items():
            self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), digest)
        self.assertEqual(store.get(conversation)["messages"][0]["content"], "合成保留升级会话，无业务内容。")
        marker = config.read_json(ROOT / "config/version.json")
        self.assertEqual(marker["version"], config.VERSION)
        self.assertEqual(marker["previous_version"], "1.3.0")
        self.assertEqual(marker["upgrade_mode"], "preserve")
        self.assertEqual(marker["reset_at"], 123)
        self.assertEqual(marker["schema_version"], upgrades.SCHEMA_VERSION)

    def test_same_version_restart_does_not_reset_even_if_policy_changes(self):
        originals, _ = self.seed_old()
        lifecycle.initialize_version("preserve")
        marker = (ROOT / "config/version.json").read_bytes()
        result = lifecycle.initialize_version("test-reset")
        self.assertFalse(result["reset"])
        self.assertEqual((ROOT / "config/version.json").read_bytes(), marker)
        self.assertEqual(hashlib.sha256((ROOT / "data/memory/memory.md").read_bytes()).hexdigest(), originals["data/memory/memory.md"])

    def test_explicit_testing_policy_resets_once_and_preserves_models(self):
        originals, _ = self.seed_old()
        result = lifecycle.initialize_version("test-reset")
        self.assertTrue(result["reset"])
        self.assertEqual(result["upgrade_mode"], "test-reset")
        self.assertEqual(config.settings()["theme"], "dark")
        self.assertEqual(store.listing(), [])
        self.assertFalse((ROOT / "data/memory/memory.md").exists())
        for relative in ("models/SYNTHETIC-UPGRADE-MODEL", "runtime/SYNTHETIC-UPGRADE-RUNTIME"):
            self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), originals[relative])
        marker = config.read_json(ROOT / "config/version.json")
        self.assertEqual(marker["reset_reason"], "version-test")
        self.assertIs(marker["backup"], False)
        config.save_settings({"theme": "light"})
        self.assertFalse(lifecycle.initialize_version("test-reset")["reset"])
        self.assertEqual(config.settings()["theme"], "light")

    def test_environment_policy_is_explicit_and_invalid_mode_fails_closed(self):
        self.seed_old()
        with patch.dict(os.environ, {"LOCAL_AI_UPGRADE_MODE": "preserve"}):
            self.assertFalse(lifecycle.initialize_version()["reset"])
        with self.assertRaisesRegex(ValueError, "升级策略"):
            lifecycle.initialize_version("invalid")
        self.assertTrue((ROOT / "data/memory/memory.md").exists())

    def test_malformed_saved_settings_do_not_reset_or_mark_upgrade_complete(self):
        self.seed_old()
        before = (ROOT / "config/version.json").read_bytes()
        config.write_json(ROOT / "config/settings.json", ["SYNTHETIC INVALID SETTINGS"])
        with self.assertRaisesRegex(ValueError, "未清空"):
            lifecycle.initialize_version("preserve")
        self.assertEqual((ROOT / "config/version.json").read_bytes(), before)
        self.assertTrue((ROOT / "data/memory/memory.md").exists())

    def test_preserve_does_not_follow_config_symlink(self):
        self.seed_old()
        original = ROOT / "cache/synthetic-settings-original.json"
        original.write_text('{"theme":"light"}', encoding="utf-8")
        target = ROOT / "config/settings.json"
        target.unlink()
        try:
            target.symlink_to(original)
        except OSError:
            self.skipTest("Windows symlink privilege unavailable")
        try:
            with self.assertRaisesRegex(ValueError, "链接"):
                lifecycle.initialize_version("preserve")
            self.assertEqual(original.read_text(encoding="utf-8"), '{"theme":"light"}')
        finally:
            target.unlink(missing_ok=True)

    def test_simulated_link_guard_needs_no_windows_symlink_privilege(self):
        self.seed_old()
        target = ROOT / "config/settings.json"
        before = (ROOT / "config/version.json").read_bytes()
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda item: item == target):
            with self.assertRaisesRegex(ValueError, "链接"):
                lifecycle.initialize_version("preserve")
        self.assertEqual((ROOT / "config/version.json").read_bytes(), before)
        self.assertTrue((ROOT / "data/memory/memory.md").exists())

    def test_upgrade_guard_rejects_non_config_target(self):
        with self.assertRaisesRegex(ValueError, "固定配置"):
            upgrades._managed_config_file(ROOT / "data/memory/memory.md")


if __name__ == "__main__":
    unittest.main()
