"""Memory token-cap checks use synthetic text and temporary roots only."""
from __future__ import annotations
import contextlib
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix="v15-memory-import-")
if "app.config" not in sys.modules:
    os.environ["LOCAL_AI_ROOT"] = _IMPORT_ROOT.name
os.environ["LOCAL_AI_DISABLE_GPU_PROBE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import memory, context


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=False):
        return SimpleNamespace(ids=list(range(len(text))), offsets=[(i, i+1) for i in range(len(text))])


class MemoryTokenLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v15-memory-case-")
        self.root = Path(self.temp.name)
        self.path = self.root / "data/memory/memory.md"
        self.cfg = {"memory_enabled": True, "memory_ai_write": True, "memory_max_kb": 16, "memory_tokens": 128}
        self.stack = contextlib.ExitStack()
        for module, name, value in ((memory,"ROOT",self.root), (memory,"PATH",self.path),
                                   (memory,"settings",lambda:self.cfg.copy()), (context,"tokenizer",lambda *a:CharacterTokenizer())):
            self.stack.enter_context(patch.object(module,name,value))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)

    def test_wrapper_included_exact_boundary_and_read_counters(self):
        text = "x" * (128 - len(memory.INJECTION_HEADER))
        result = memory.update(text, ai=True)
        self.assertEqual(result["used_tokens"],128)
        self.assertEqual(result["token_limit"],128)
        self.assertTrue(result["tokens_exact"])
        self.assertEqual(memory.read()["counting_method"],"qwen3.5_tokenizer")
        self.assertEqual(len(memory.injection()),128)

    def test_one_token_over_keeps_old_file_for_ai_and_human(self):
        memory.update("old")
        old = self.path.read_bytes()
        for ai in (False,True):
            with self.assertRaises(memory.MemoryOperationError) as caught:
                memory.update("x"*128,ai=ai)
            self.assertEqual(caught.exception.code,"memory_token_limit")
            self.assertEqual(caught.exception.details["used_tokens"],128+len(memory.INJECTION_HEADER))
            self.assertEqual(self.path.read_bytes(),old)

    def test_append_checks_final_whole_text(self):
        text = "x"*(127-len(memory.INJECTION_HEADER))
        memory.update(text)
        old=self.path.read_bytes()
        with self.assertRaises(memory.MemoryOperationError):
            memory.update("y",mode="append",ai=True)
        self.assertEqual(self.path.read_bytes(),old)

    def test_lowered_budget_warns_without_changing_source_and_caps_injection(self):
        self.cfg["memory_tokens"]=512
        memory.update("x"*300)
        old=self.path.read_bytes()
        self.cfg["memory_tokens"]=128
        result=memory.read()
        self.assertTrue(result["over_budget"] and result["over_limit"])
        self.assertTrue(result["warning"])
        self.assertLessEqual(len(memory.injection()),128)
        self.assertEqual(self.path.read_bytes(),old)

    def test_clear_repairs_oversize_legacy_memory(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("x"*1000,encoding="utf-8")
        result=memory.update("",ai=True)
        self.assertEqual(result["used_tokens"],0)
        self.assertEqual(memory.injection(),"")
        self.assertEqual(self.path.read_bytes(),b"")

    def test_byte_cap_remains_independent(self):
        self.cfg.update(memory_tokens=100000,memory_max_kb=1)
        with self.assertRaises(memory.MemoryOperationError) as caught:
            memory.update("x"*1025)
        self.assertEqual(caught.exception.code,"memory_limit")
        self.assertFalse(self.path.exists())

    def test_unknown_tokenizer_explicitly_reports_estimate(self):
        with patch.object(context,"tokenizer",lambda *a:None):
            result=memory.update("synthetic preference")
            self.assertEqual(result["counting_method"],"conservative_estimate")
            self.assertFalse(result["tokens_exact"])

    def test_public_qwen_asset_counts_saved_text_plus_wrapper(self):
        from tokenizers import Tokenizer
        asset=Path(__file__).resolve().parents[1]/"runtime/tokenizers/qwen35/tokenizer.json"
        if not asset.is_file():
            self.skipTest("Official tokenizer asset not prepared; optional integration test")
        tok=Tokenizer.from_file(str(asset))
        text="# 合成偏好\n单位使用毫米，工况需要单独核实。"
        with patch.object(context,"tokenizer",lambda *a:tok):
            result=memory.update(text,ai=True)
            expected=len(tok.encode(memory.INJECTION_HEADER+text,add_special_tokens=False).ids)
            self.assertEqual(result["used_tokens"],expected)
            self.assertEqual(memory.read()["used_tokens"],expected)
            self.assertTrue(result["tokens_exact"])

    def test_revision_still_optional_and_stale_ignored(self):
        memory.update("first")
        memory.update("second",revision="obsolete",ai=True)
        self.assertEqual(self.path.read_text(encoding="utf-8"),"second")

    def test_atomic_failure_preserves_old_content_and_removes_temporary(self):
        memory.update("before")
        with patch.object(memory.os,"replace",side_effect=OSError("synthetic atomic failure")):
            with self.assertRaises(memory.MemoryOperationError):
                memory.update("after")
        self.assertEqual(self.path.read_text(encoding="utf-8"),"before")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_concurrent_append_enforces_cap_against_latest_locked_file(self):
        memory.update("")
        def append(_):
            try:
                memory.update("1234567890",mode="append",ai=True)
                return True
            except memory.MemoryOperationError as error:
                self.assertEqual(error.code,"memory_token_limit")
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(append,range(20)))
        result=memory.read()
        self.assertLessEqual(result["used_tokens"],128)
        self.assertGreater(sum(results),0)
        self.assertLess(sum(results),20)
        self.assertEqual(result["text"].splitlines(),["1234567890"]*sum(results))


if __name__ == "__main__":
    unittest.main()
