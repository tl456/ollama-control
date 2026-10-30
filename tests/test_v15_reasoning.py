"""Declared cloud reasoning levels; synthetic settings and MockTransport only."""
import asyncio
import copy
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
from test_core import ROOT, SOURCE_ROOT
from app import config, lifecycle, providers, store, turn_queue


class ReasoningLevelsTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(ROOT.resolve(), SOURCE_ROOT.resolve())
        self.assertTrue(ROOT.name.startswith("local-ai-test-"))
        lifecycle.reset_managed()
        config.write_json(ROOT / "config/settings.json", {**config.DEFAULTS, "gpu_monitor": False})

    def connection(self, **values):
        return {"id": "synthetic_cloud", "name": "合成连接", "kind": "cloud", "base_url": "https://synthetic.invalid/v1",
                "models": ["synthetic-model"], **values}

    def save(self, item):
        config.save_settings({"providers": [item]})
        return config.settings()["providers"][0]

    def request(self, cfg, effort, think=True):
        calls = []
        async def transport(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"合成","reasoning_content":"合成思考"}}]}\n\ndata: [DONE]\n\n')
        @asynccontextmanager
        async def client(*args, **kwargs):
            async with httpx.AsyncClient(base_url="https://synthetic.invalid/", transport=httpx.MockTransport(transport)) as value:
                yield value
        async def run():
            with patch.object(providers, "provider", return_value=cfg), patch.object(providers, "cloud_client", client):
                return [packet async for packet in providers.stream_model("synthetic_cloud", {
                    "model": "synthetic-model", "messages": [{"role": "user", "content": "合成输入"}],
                    "think": think, "options": {"reasoning_effort": effort}})]
        return calls, run

    def test_declared_levels_and_none_alias_roundtrip_without_credentials(self):
        value = self.save(self.connection(reasoning_effort_supported=True,
            reasoning_effort_levels=["high", "none", "medium", "low"], reasoning_effort="none"))
        self.assertEqual(value["reasoning_effort"], "off")
        self.assertEqual(value["reasoning_effort_levels"], ["off", "low", "medium", "high"])
        saved = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8"))["providers"][0]
        self.assertEqual(saved, value)
        public = config.public_settings()["providers"][0]
        self.assertEqual(public["reasoning_effort_levels"], value["reasoning_effort_levels"])
        self.assertFalse(public["has_key"])

    def test_deepseek_default_is_high_and_off_low_high_max(self):
        value = self.save(self.connection(thinking_mode="deepseek"))
        self.assertEqual(value["reasoning_effort"], "high")
        self.assertEqual(value["reasoning_effort_levels"], ["off", "low", "high", "max"])
        self.assertTrue(value["reasoning_effort_supported"])

    def test_invalid_or_unsupported_declarations_do_not_modify_saved_settings(self):
        self.save(self.connection())
        before = (ROOT / "config/settings.json").read_bytes()
        invalid = [
            {"reasoning_effort_levels": "low,high"}, {"reasoning_effort_levels": []},
            {"reasoning_effort_levels": ["high", "high"]}, {"reasoning_effort_levels": ["none", "off"]},
            {"reasoning_effort_levels": [None]}, {"reasoning_effort_levels": ["ultra"]},
            {"reasoning_effort_levels": ["low"]}, {"reasoning_effort_supported": "yes"},
            {"reasoning_effort": None}, {"thinking_mode": "deepseek", "reasoning_effort": "medium"},
            {"thinking_mode": "deepseek", "reasoning_effort_levels": ["off", "medium", "high"]},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.save(self.connection(**values))
            self.assertEqual((ROOT / "config/settings.json").read_bytes(), before)

    def test_generic_ui_needs_both_explicit_support_and_levels(self):
        for cfg in ({}, {"reasoning_effort_supported": True},
                    {"reasoning_effort_supported": False, "reasoning_effort_levels": ["off", "high"]}):
            with self.subTest(cfg=cfg):
                profile = providers.reasoning_profile(cfg)
                self.assertFalse(profile["reasoning_effort_supported"])
                self.assertEqual(profile["reasoning_effort_levels"], [])
        cfg = self.connection(reasoning_effort_supported=True,
            reasoning_effort_levels=["off", "medium", "high"], reasoning_effort="medium")
        with patch.object(providers, "provider", return_value=cfg):
            caps = asyncio.run(providers.capability("synthetic_cloud", "synthetic-model"))
        self.assertTrue(caps["reasoning_effort_supported"])
        self.assertEqual(caps["reasoning_effort_levels"], ["off", "medium", "high"])
        self.assertEqual(caps["reasoning_effort"], "medium")

    def test_optional_explicit_model_metadata_overrides_without_guessing(self):
        cfg = {"reasoning_effort_supported": True, "reasoning_effort_levels": ["off", "high"], "reasoning_effort": "high"}
        original = copy.deepcopy(cfg)
        profile = providers.reasoning_profile(cfg, {"reasoning_effort_supported": True,
            "reasoning_effort_levels": ["off", "medium"], "reasoning_effort": "medium"})
        self.assertEqual(profile["reasoning_effort_levels"], ["off", "medium"])
        self.assertEqual(profile["reasoning_effort"], "medium")
        self.assertFalse(providers.reasoning_profile(cfg, {"reasoning_effort_supported": False})["reasoning_effort_supported"])
        self.assertEqual(cfg, original)

    def test_deepseek_off_alias_disables_thinking_but_generic_medium_is_transmitted(self):
        for effort in ("off", "none"):
            cfg = self.connection(thinking_mode="deepseek", reasoning_effort="high")
            calls, run = self.request(cfg, effort)
            packets = asyncio.run(run())
            self.assertEqual(calls[0]["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", calls[0])
            self.assertEqual(packets[0]["message"]["thinking"], "合成思考")
        cfg = self.connection(reasoning_effort_supported=True, reasoning_effort_levels=["off", "medium", "high"], reasoning_effort="high")
        calls, run = self.request(cfg, "medium")
        asyncio.run(run())
        self.assertEqual(calls[0]["reasoning_effort"], "medium")
        self.assertNotIn("thinking", calls[0])
        self.assertNotIn("reasoning_effort_levels", calls[0])

    def test_unsupported_request_is_not_silently_replaced_or_sent(self):
        configs = [self.connection(thinking_mode="deepseek", reasoning_effort="high"),
                   self.connection(reasoning_effort_supported=True, reasoning_effort_levels=["off", "high"], reasoning_effort="high")]
        for cfg in configs:
            calls, run = self.request(cfg, "medium")
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                asyncio.run(run())
            self.assertEqual(calls, [])

    def test_deepseek_display_subset_still_allows_auxiliary_disabled_mode(self):
        cfg = self.connection(thinking_mode="deepseek", reasoning_effort_levels=["low", "high"], reasoning_effort="high")
        for effort in ("off", "none"):
            calls, run = self.request(cfg, effort, think=False)
            asyncio.run(run())
            self.assertEqual(calls[0]["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", calls[0])
        calls, run = self.request(cfg, "medium", think=False)
        with self.assertRaises(ValueError):
            asyncio.run(run())
        self.assertEqual(calls, [])

    def test_legacy_supported_generic_medium_remains_usable_but_not_advertised(self):
        cfg = self.connection(reasoning_effort_supported=True, reasoning_effort="medium")
        calls, run = self.request(cfg, "inherit")
        asyncio.run(run())
        self.assertEqual(calls[0]["reasoning_effort"], "medium")
        self.assertFalse(providers.reasoning_profile(cfg)["reasoning_effort_supported"])

    def test_reasoning_metadata_never_leaks_to_ollama(self):
        value = providers.local_device_body("local", {"inference_device": "auto", "reasoning_effort": "medium",
            "reasoning_effort_levels": ["medium"], "reasoning_effort_supported": True,
            "options": {"num_ctx": 8192, "reasoning_effort": "medium", "reasoning_effort_levels": ["medium"],
                        "reasoning_effort_supported": True}})
        self.assertEqual(value["options"], {"num_ctx": 8192})
        for key in ("reasoning_effort", "reasoning_effort_levels", "reasoning_effort_supported", "inference_device"):
            self.assertNotIn(key, value)

    def test_queued_medium_stays_frozen_after_connection_or_next_turn_changes(self):
        item = self.connection(reasoning_effort_supported=True,
            reasoning_effort_levels=["off", "medium", "high"], reasoning_effort="medium")
        self.save(item)
        conv = store.create(config.settings())
        body = {"content": "合成排队快照", "cloud_confirmed": True,
                "options": {"provider": item["id"], "model": "synthetic-model", "reasoning_effort": "medium", "think": True}}
        first = asyncio.run(turn_queue._snapshot(conv, body, False, True))
        self.save({**item, "reasoning_effort": "high"})
        second = asyncio.run(turn_queue._snapshot(conv, {**body, "options": {**body["options"], "reasoning_effort": "high"}}, False, True))
        self.assertEqual(first["options"]["reasoning_effort"], "medium")
        self.assertEqual(first["options"]["_resolved_reasoning_effort"], "medium")
        self.assertEqual(second["options"]["_resolved_reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()
