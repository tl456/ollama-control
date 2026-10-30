"""Reset regression tests using only the synthetic test root and fixture bytes."""
import json
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_core import ROOT, SOURCE_ROOT
from app import config, downloads, lifecycle, store, tools


class ResetV15Tests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(ROOT.resolve(), SOURCE_ROOT.resolve())
        self.assertTrue(ROOT.name.startswith("local-ai-test-"))
        lifecycle.reset_managed()
        self.original = tempfile.TemporaryDirectory(prefix="synthetic-reset-original-")
        self.addCleanup(self.original.cleanup)
        self.original_path = Path(self.original.name) / "original.txt"
        self.original_path.write_text("SYNTHETIC ORIGINAL: 保留原件", encoding="utf-8")
        self.original_bytes = self.original_path.read_bytes()
        self.preserved = {}
        for relative in ("models/SYNTHETIC-V15-RESET-MODEL", "runtime/SYNTHETIC-V15-RESET-RUNTIME"):
            path = ROOT / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("SYNTHETIC PRESERVED " + relative).encode())
            self.preserved[path] = path.read_bytes()
            self.addCleanup(path.unlink, missing_ok=True)

    def seed(self):
        tools.save_tool_configuration("search_files", {"search_depth": 3, "start_directory": self.original.name})
        config.write_json(ROOT / "config/tools/synthetic_plugin.json", {"synthetic": "清理独立插件配置"})
        config.write_json(ROOT / "config/tools.json", {"filesystem": {"search_depth": 2}})
        config.write_json(ROOT / "config/settings.json", {**config.DEFAULTS,
            "roots": [{"id": "synthetic_original", "path": self.original.name}]})
        stamp = time.time()
        downloads.remember({"id": "synthetic-v15-reset-download", "model": "synthetic:no-download",
                            "status": "成功", "done": True, "created": stamp, "ended": stamp})
        store.create(config.settings())
        self.assertEqual(len(downloads.listing({})), 1)
        self.assertEqual(tools.tool_configuration("search_files")["values"]["search_depth"], 3)

    def assert_cleared_and_preserved(self):
        self.assertTrue((ROOT / "config/tools").is_dir())
        self.assertEqual(list((ROOT / "config/tools").iterdir()), [])
        self.assertFalse((ROOT / "cache/download-history.json").exists())
        self.assertEqual(downloads.listing({}), [])
        self.assertEqual(config.read_json(ROOT / "config/tools.json"), {})
        values = tools.tool_configuration("search_files")["values"]
        self.assertEqual(values["search_depth"], config.DEFAULTS["search_depth"])
        self.assertEqual(values["start_directory"], "")
        self.assertEqual(store.listing_page()["total"], 0)
        self.assertEqual(self.original_path.read_bytes(), self.original_bytes)
        for path, content in self.preserved.items():
            self.assertEqual(path.read_bytes(), content)

    def test_manual_reset_clears_scoped_tool_settings_and_download_history(self):
        self.seed()
        result = lifecycle.reset_managed("manual")
        self.assertIsNone(result["backup"])
        self.assertTrue(result["models_preserved"] and result["runtime_preserved"])
        self.assert_cleared_and_preserved()
        marker = config.read_json(ROOT / "config/version.json")
        self.assertEqual(marker["reset_reason"], "manual")

    def test_version_test_reset_clears_once_and_same_version_keeps_new_settings(self):
        self.seed()
        config.write_json(ROOT / "config/version.json", {"version": "1.4.0"})
        result = lifecycle.initialize_version("test-reset")
        self.assertTrue(result["reset"])
        self.assert_cleared_and_preserved()
        self.assertEqual(config.read_json(ROOT / "config/version.json")["reset_reason"], "version-test")
        self.seed()
        files = {path: path.read_bytes() for path in
                 (ROOT / "config/tools/search_files.json", ROOT / "cache/download-history.json")}
        self.assertFalse(lifecycle.initialize_version("test-reset")["reset"])
        for path, content in files.items():
            self.assertEqual(path.read_bytes(), content)

    def test_preserve_upgrade_keeps_scoped_tool_settings_and_download_history(self):
        self.seed()
        config.write_json(ROOT / "config/version.json", {"version": "1.4.0"})
        files = {path: path.read_bytes() for path in
                 (ROOT / "config/tools/search_files.json", ROOT / "cache/download-history.json")}
        self.assertFalse(lifecycle.initialize_version("preserve")["reset"])
        for path, content in files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.original_path.read_bytes(), self.original_bytes)

    def test_safe_remove_rejects_outside_boundary_and_parent_traversal(self):
        for target, parent in ((self.original_path, self.original_path.parent),
                               (ROOT / "data/../models/SYNTHETIC-V15-RESET-MODEL", ROOT / "data"),
                               (ROOT, ROOT)):
            with self.subTest(target=str(target)), self.assertRaisesRegex(ValueError, "管理范围"):
                lifecycle.safe_remove(target, parent)
        self.assertEqual(self.original_path.read_bytes(), self.original_bytes)
        for path, content in self.preserved.items():
            self.assertEqual(path.read_bytes(), content)

    def test_symlink_or_junction_parent_is_rejected_before_opening_database(self):
        self.seed()
        for blocked, method in ((ROOT / "config/tools", "is_symlink"),
                                (ROOT / "data", "is_junction"),
                                (ROOT / "data/chat.db", "is_symlink")):
            original = getattr(Path, method, lambda value: False)
            def flagged(path, *, blocked=blocked, original=original):
                return path.absolute() == blocked or original(path)
            with self.subTest(blocked=str(blocked), method=method), \
                    patch.object(Path, method, flagged, create=True), patch.object(store, "connect") as connect:
                with self.assertRaisesRegex(ValueError, "链接/联接点"):
                    lifecycle.reset_managed()
                connect.assert_not_called()
            self.assertEqual(self.original_path.read_bytes(), self.original_bytes)
            self.assertTrue((ROOT / "config/tools/search_files.json").is_file())

    def test_reparse_target_or_ancestor_is_rejected_before_database_open(self):
        self.seed()
        original = Path.lstat
        class ReparseStat:
            st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
        for blocked in (ROOT / "config/tools", ROOT / "config", ROOT / "cache/download-history.json"):
            def flagged(path, *args, blocked=blocked, **kwargs):
                result = original(path, *args, **kwargs)
                return ReparseStat(result) if path.absolute() == blocked else result
            with self.subTest(blocked=str(blocked)), patch.object(Path, "lstat", flagged), \
                    patch.object(store, "connect") as connect:
                with self.assertRaisesRegex(ValueError, "重解析"):
                    lifecycle.reset_managed()
                connect.assert_not_called()
            self.assertEqual(self.original_path.read_bytes(), self.original_bytes)

    def test_recursive_remove_refuses_linked_child(self):
        self.seed()
        blocked = ROOT / "config/tools/search_files.json"
        original = Path.is_symlink
        def flagged(path):
            return path.absolute() == blocked or original(path)
        with patch.object(Path, "is_symlink", flagged):
            with self.assertRaisesRegex(ValueError, "链接/联接点"):
                lifecycle.safe_remove(ROOT / "config/tools", ROOT / "config")
        self.assertTrue(blocked.is_file())
        self.assertEqual(self.original_path.read_bytes(), self.original_bytes)



if __name__ == "__main__":
    unittest.main()
