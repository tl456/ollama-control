"""Synthetic lifecycle settings and native-window configuration checks."""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from test_core import ROOT
from app import lifecycle, providers
from app.config import DEFAULTS, save_settings, settings


class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        lifecycle.reset_managed()

    def test_management_settings_roundtrip(self):
        save_settings({"ollama_management": "external", "ollama_autostart": False,
                       "ollama_stop_on_exit": False, "ollama_keep_alive": -1})
        value = settings()
        self.assertEqual(value["ollama_management"], "external")
        self.assertFalse(value["ollama_autostart"])
        self.assertFalse(value["ollama_stop_on_exit"])
        self.assertEqual(value["ollama_keep_alive"], -1)

    def test_unknown_management_rejected(self):
        with self.assertRaises(ValueError):
            save_settings({"ollama_management": "force_takeover"})

    def test_keepalive_clamped(self):
        save_settings({"ollama_keep_alive": 1000000})
        self.assertEqual(settings()["ollama_keep_alive"], 86400)

    def test_autostart_disabled_no_contact(self):
        with patch.object(providers, "settings", return_value={**DEFAULTS, "ollama_autostart": False}), patch.object(providers.httpx, "get") as get, patch.object(providers, "launch_ollama") as launch:
            self.assertFalse(providers.start_managed_if_needed()["started"])
            get.assert_not_called()
            launch.assert_not_called()

    def test_external_mode_no_start(self):
        with patch.object(providers, "settings", return_value={**DEFAULTS, "ollama_management": "external"}), patch.object(providers, "launch_ollama") as launch:
            self.assertFalse(providers.start_managed_if_needed()["started"])
            launch.assert_not_called()

    def test_running_external_not_adopted(self):
        response = Mock()
        with patch.object(providers.httpx, "get", return_value=response), patch.object(providers, "service_ownership", return_value={"managed": False}), patch.object(providers, "launch_ollama") as launch:
            result = providers.start_managed_if_needed()
            self.assertFalse(result["started"])
            self.assertIn("未接管", result["reason"])
            launch.assert_not_called()

    def test_manual_start_external_port_rejected(self):
        with patch.object(providers.httpx, "get", return_value=Mock()), patch.object(providers, "service_ownership", return_value={"managed": False}), patch.object(providers.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "外部"):
                providers.launch_ollama()
            popen.assert_not_called()

    def test_external_stop_and_restart_rejected(self):
        with patch.object(providers, "service_ownership", return_value={"managed": False}), patch("app.jobs.active", return_value=False), patch.object(providers, "stop_owned_service") as stop:
            with self.assertRaises(ValueError):
                providers.stop_local_service()
            with self.assertRaises(ValueError):
                providers.restart_ollama()
            stop.assert_not_called()

    def test_active_job_prevents_stop(self):
        with patch("app.jobs.active", return_value=True), patch.object(providers, "stop_owned_service") as stop:
            with self.assertRaisesRegex(ValueError, "任务"):
                providers.stop_local_service()
            stop.assert_not_called()

    def test_exit_keep_service_does_not_stop_chat(self):
        with patch.object(providers, "settings", return_value={**DEFAULTS, "ollama_stop_on_exit": False}), patch.object(providers, "stop_embedding_service") as cpu, patch.object(providers, "stop_owned_service") as stop:
            providers.shutdown_owned_services()
            cpu.assert_called_once()
            stop.assert_not_called()

    def test_native_window_text_selection_and_shared_ico(self):
        project = Path(__file__).resolve().parents[1]
        old_cwd = Path.cwd()
        fake = types.SimpleNamespace(create_window=Mock(return_value=object()), start=Mock())
        try:
            spec = importlib.util.spec_from_file_location("qa_v13_main", project / "main.py")
            main = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(main)
            with patch.dict(sys.modules, {"webview": fake}):
                self.assertEqual(main.launch_window("webview2"), "webview2")
            self.assertTrue(fake.create_window.call_args.kwargs["text_select"])
            self.assertEqual(fake.create_window.call_args.args[0], "本地智能工作台")
            self.assertEqual(Path(fake.start.call_args.kwargs["icon"]), project / "web/app.ico")
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
