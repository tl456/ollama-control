"""Public-release policy and publication guard tests use synthetic data only."""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import ROOT, SOURCE_ROOT
from app import config, lifecycle, store, upgrades

spec = importlib.util.spec_from_file_location("public_source_audit", SOURCE_ROOT / "scripts/audit_public.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        lifecycle.reset_managed()

    def test_default_public_upgrade_preserves_conversation_settings_and_memory(self):
        config.save_settings({"theme": "light"})
        conversation = store.create(config.settings())
        memory = ROOT / "data/memory/memory.md"
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text("Synthetic preference: millimetres.", encoding="utf-8")
        config.write_json(ROOT / "config/version.json", {"version": "0.0.0-fixture"})
        self.assertEqual(upgrades.RELEASE_UPGRADE_MODE, "preserve")
        with patch.dict(os.environ):
            os.environ.pop("LOCAL_AI_UPGRADE_MODE", None)
            result = lifecycle.initialize_version()
        self.assertFalse(result["reset"])
        self.assertEqual(config.settings()["theme"], "light")
        self.assertIsNotNone(store.get(conversation["id"]))
        self.assertEqual(memory.read_text(encoding="utf-8"), "Synthetic preference: millimetres.")
        self.assertEqual(config.read_json(ROOT / "config/version.json")["version"], config.VERSION)

    def test_private_paths_refused_before_payload_inspection(self):
        for name in ("config/auth.json", "data/memory/memory.md", "runtime/tokenizers/file.json",
                     "plugins/imported/plugin.py", "logs/server.log", "../credentials.txt"):
            with self.subTest(name=name):
                result = audit.inspect_bytes(name, b"synthetic private fixture")
                self.assertEqual(result[0]["type"], "file_outside_public_source_allowlist")

    def test_secret_values_are_reported_without_reprinting_the_value(self):
        secret = "sk-" + "A1b2C3d4" * 5
        result = audit.inspect_bytes("app/fixture.py", ("value = '" + secret + "'\n").encode())
        self.assertEqual(result[0]["type"], "api_secret")
        self.assertNotIn(secret, repr(result))

    def test_personal_paths_and_protected_headers_fail(self):
        result = audit.inspect_bytes("README.md", ("C:/" + "Users/" + "actual-person/Documents/secret.txt").encode())
        self.assertEqual(result[0]["type"], "personal_windows_path")
        self.assertEqual(audit.inspect_bytes("README.md", b"%TSD-Header-001%opaque")[0]["type"], "protected_file_header")

    def test_public_synthetic_placeholders_and_utf8_are_distinguished(self):
        self.assertEqual(audit.inspect_bytes("README.md", b"C:/Users/Public/Documents/example"), [])
        self.assertEqual(audit.inspect_bytes("app/example.py", b"value = 'sk-synthetic-example-never-real-secret'\n"), [])
        self.assertEqual(audit.inspect_bytes("README.md", b"\xff\xfe")[0]["type"], "invalid_utf8_public_text")

    def test_gitignore_covers_private_directories_and_outputs(self):
        text = (SOURCE_ROOT / ".gitignore").read_text(encoding="utf-8")
        for rule in ("config/", "data/", "models/", "runtime/", "logs/", "cache/", "plugins/imported/", "dist/"):
            self.assertIn(rule, text.splitlines())

    @unittest.skipUnless(shutil.which("git"), "Git is required for staged-publication integration")
    def test_staged_secret_is_detected_after_working_tree_was_cleaned(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-public-index-") as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            (root / "app").mkdir()
            for name, value in {"README.md": "Synthetic public project", "LICENSE": "Synthetic license fixture",
                                "main.py": "pass\n", "app/config.py": "VERSION = '0.0.0'\n"}.items():
                (root / name).write_text(value, encoding="utf-8")
            secret = "sk-" + "Z9y8X7w6" * 5
            (root / "README.md").write_text(secret, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md", "LICENSE", "main.py", "app/config.py"], check=True, capture_output=True)
            (root / "README.md").write_text("Synthetic clean worktree", encoding="utf-8")
            report = audit.scan(root, tracked=True)
            self.assertFalse(report["passed"])
            self.assertTrue(report["reads_exact_staged_blobs"])
            self.assertIn("api_secret", {row["type"] for row in report["findings"]})
            self.assertNotIn(secret, repr(report))

    @unittest.skipUnless(shutil.which("git"), "Git is required for staged-publication integration")
    def test_empty_index_cannot_be_certified_as_a_public_release(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-empty-public-index-") as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            report = audit.scan(root, tracked=True)
            self.assertFalse(report["passed"])
            self.assertEqual(report["files_checked"], 0)
            self.assertEqual(len(report["findings"]), 4)


if __name__ == "__main__":
    unittest.main()
