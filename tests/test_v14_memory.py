"""Isolated synthetic tests for the single freely editable Markdown memory.

No application services, models, GPU queries, real memory, or user attachments.
"""
from __future__ import annotations

import contextlib
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

import jsonschema
from test_core import ROOT as _SYNTHETIC_SUITE_ROOT
from app import memory


class FreeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory(prefix="local-ai-v14-memory-")
        self.root = Path(self.files.name)
        self.path = self.root / "data/memory/memory.md"
        self.cfg = {"memory_enabled": True, "memory_ai_write": True,
                    "memory_max_kb": 16, "memory_tokens": 1024}
        self.patches = contextlib.ExitStack()
        self.patches.enter_context(patch.object(memory, "ROOT", self.root))
        self.patches.enter_context(patch.object(memory, "PATH", self.path))
        self.patches.enter_context(patch.object(memory, "settings", lambda: self.cfg.copy()))
        self.addCleanup(self.files.cleanup)
        self.addCleanup(self.patches.close)

    def disk(self):
        # The verification reads the saved file independently of memory.read().
        return self.path.read_text(encoding="utf-8-sig")

    def test_first_write_without_read_or_revision(self):
        result = memory.update("# Synthetic preference\nUse concise explanations.", ai=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(self.disk(), "# Synthetic preference\nUse concise explanations.")
        self.assertEqual(result["revision"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result["bytes"], len(self.path.read_bytes()))
        self.assertEqual(result["mode"], "replace")

    def test_first_empty_write_creates_the_canonical_file(self):
        result = memory.update("", ai=True)
        self.assertTrue(self.path.is_file())
        self.assertTrue(result["changed"])
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(result["note"], "长期记忆已清空")

    def test_arbitrary_stale_revision_no_longer_blocks_overwrite(self):
        old = memory.read()["revision"]
        memory.update("Human synthetic edit")
        for revision in (old, "wrong", "", None, {"legacy": "ignored"}):
            result = memory.update("AI replacement", revision, True)
            self.assertTrue(result["ok"])
            self.assertEqual(self.disk(), "AI replacement")
        self.assertEqual(result["write_policy"], "last_successful_save_wins")

    def test_full_free_edit_can_delete_reorder_merge_and_clear(self):
        memory.update("# A\nOld preference.\n# B\nKeep this fact.\n# C\nTemporary.")
        edited = "# B\nKeep this fact, clarified.\n# A\nNew preference."
        memory.update(edited, ai=True)
        self.assertEqual(self.disk(), edited)
        self.assertNotIn("# C", self.disk())
        memory.update("", ai=True)
        self.assertEqual(self.disk(), "")

    def test_human_and_ai_share_the_same_last_save_wins_file(self):
        memory.update("AI first", ai=True)
        memory.update("Human edit", "old-human-revision")
        memory.update("AI final full edit", "old-ai-revision", True)
        self.assertEqual(self.disk(), "AI final full edit")
        self.assertEqual(list(self.path.parent.glob("*.md")), [self.path])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_append_uses_latest_content_and_only_needed_newline(self):
        memory.update("First")
        memory.update("Second", mode="append", ai=True)
        memory.update("\nThird", mode="append")
        memory.update("\n", mode="append")
        memory.update("Fourth", mode="append")
        self.assertEqual(self.disk(), "First\nSecond\nThird\nFourth")
        # An explicit repeated append is not silently discarded as a duplicate.
        memory.update("Fourth", mode="append")
        self.assertEqual(self.disk(), "First\nSecond\nThird\nFourth\nFourth")

    def test_empty_append_preserves_existing_utf8_bom_and_bytes(self):
        self.path.parent.mkdir(parents=True)
        original = b"\xef\xbb\xbf# Synthetic\r\nText."
        self.path.write_bytes(original)
        result = memory.update("", mode="append")
        self.assertFalse(result["changed"])
        self.assertEqual(self.path.read_bytes(), original)
        memory.update("Tail", mode="append")
        self.assertEqual(self.path.read_bytes(), original + b"\nTail")

    def test_identical_replace_is_successful_noop_without_rewriting(self):
        memory.update("Synthetic unchanged")
        timestamp = self.path.stat().st_mtime_ns
        with patch.object(memory.os, "replace", side_effect=AssertionError("must not rewrite")):
            result = memory.update("Synthetic unchanged", ai=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.path.stat().st_mtime_ns, timestamp)
        self.assertEqual(self.disk(), "Synthetic unchanged")

    def test_concurrent_full_writes_are_serial_and_last_save_wins(self):
        first_inside, permit_first, second_started = threading.Event(), threading.Event(), threading.Event()
        real_replace = memory.os.replace
        saved = []

        def replace(source, target):
            value = Path(source).read_bytes()
            if value == b"FIRST":
                first_inside.set()
                self.assertTrue(permit_first.wait(5))
            result = real_replace(source, target)
            saved.append(value)
            return result

        def second():
            second_started.set()
            return memory.update("SECOND", ai=True)

        with patch.object(memory.os, "replace", replace), ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(memory.update, "FIRST", ai=True)
            self.assertTrue(first_inside.wait(5))
            two = pool.submit(second)
            self.assertTrue(second_started.wait(5))
            permit_first.set()
            self.assertTrue(one.result(timeout=5)["ok"])
            self.assertTrue(two.result(timeout=5)["ok"])
        self.assertEqual(saved, [b"FIRST", b"SECOND"])
        self.assertEqual(self.path.read_bytes(), b"SECOND")

    def test_concurrent_appends_do_not_drop_other_appends(self):
        memory.update("HEADER")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda number: memory.update(f"ITEM-{number:02d}", ai=True, mode="append"), range(20)))
        self.assertTrue(all(row["ok"] for row in results))
        lines = self.disk().splitlines()
        self.assertEqual(lines[0], "HEADER")
        self.assertEqual(set(lines[1:]), {f"ITEM-{n:02d}" for n in range(20)})
        self.assertEqual(len(lines), 21)

    def test_plain_tool_schema_has_no_revision_or_path(self):
        definition = memory.TOOLS[1]["parameters"]
        self.assertEqual(definition["required"], ["text"])
        self.assertNotIn("revision", definition["properties"])
        self.assertNotIn("path", definition["properties"])
        jsonschema.validate({"text": "Synthetic"}, definition)
        jsonschema.validate({"text": "", "mode": "replace"}, definition)
        jsonschema.validate({"text": "More", "mode": "append"}, definition)

    def test_normalizer_accepts_only_legacy_revision_as_extra(self):
        original = {"text": "Synthetic", "revision": "wrong"}
        args = memory.normalize_update_arguments(original)
        self.assertEqual(args, {"text": "Synthetic", "mode": "replace"})
        self.assertEqual(original, {"text": "Synthetic", "revision": "wrong"})
        for bad in (None, [], {}, {"text": 1}, {"text": "x", "path": "elsewhere"}, {"text": "x", "mode": "merge"}):
            with self.assertRaises(memory.MemoryOperationError):
                memory.normalize_update_arguments(bad)

    def test_tool_read_omits_long_digest_but_legacy_read_retains_it(self):
        memory.update("Synthetic")
        modern, legacy = memory.read_for_tool(), memory.read()
        self.assertNotIn("revision", modern)
        self.assertFalse(modern["revision_required"])
        self.assertEqual(len(legacy["revision"]), 64)
        self.assertEqual(modern["text"], legacy["text"])

    def test_ai_switch_blocks_ai_without_preventing_manual_edits(self):
        for disabled in ("memory_enabled", "memory_ai_write"):
            self.cfg[disabled] = False
            with self.assertRaises(memory.MemoryOperationError) as raised:
                memory.update("AI denied", ai=True)
            self.assertEqual(raised.exception.code, "memory_ai_disabled")
            memory.update("Human can still edit")
            self.assertEqual(self.disk(), "Human can still edit")
            self.cfg[disabled] = True

    def test_definitions_respect_global_and_ai_write_switches(self):
        self.assertEqual(len(memory.definitions()), 2)
        self.assertEqual([d["function"]["name"] for d in memory.definitions(False)], ["memory_read"])
        self.cfg["memory_ai_write"] = False
        self.assertEqual([d["function"]["name"] for d in memory.definitions()], ["memory_read"])
        self.cfg["memory_enabled"] = False
        self.assertEqual(memory.definitions(), [])

    def test_utf8_capacity_and_append_capacity_are_enforced(self):
        self.cfg["memory_max_kb"] = 1
        memory.update("A" * 1000)
        for text, mode in (("字" * 342, "replace"), ("B" * 24, "append")):
            with self.assertRaises(memory.MemoryOperationError) as raised:
                memory.update(text, mode=mode, ai=True)
            self.assertEqual(raised.exception.code, "memory_limit")
            self.assertEqual(self.path.read_bytes(), b"A" * 1000)

    def test_invalid_text_and_mode_do_not_mutate_memory(self):
        memory.update("Kept")
        for text in (None, 1, b"bytes", "bad\x00text", "bad\ud800text"):
            with self.assertRaises(memory.MemoryOperationError):
                memory.update(text, ai=True)
        with self.assertRaises(memory.MemoryOperationError) as raised:
            memory.update("not saved", mode="unknown")
        self.assertEqual(raised.exception.code, "memory_invalid_mode")
        self.assertEqual(self.disk(), "Kept")

    def test_full_replace_repairs_invalid_utf8_but_append_does_not(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfeinvalid")
        for operation in (memory.read, lambda: memory.update("more", mode="append")):
            with self.assertRaises(memory.MemoryOperationError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "memory_invalid_encoding")
        memory.update("Repaired", ai=True)
        self.assertEqual(self.disk(), "Repaired")

    def test_full_replace_repairs_oversized_file_without_reading_all(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"X" * (memory.HARD_LIMIT + 20))
        with self.assertRaises(memory.MemoryOperationError) as raised:
            memory.read()
        self.assertEqual(raised.exception.code, "memory_read_limit")
        memory.update("", ai=True)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_fixed_path_cannot_be_redirected(self):
        outside = self.root / "not-the-memory.md"
        with patch.object(memory, "PATH", outside):
            with self.assertRaises(memory.MemoryOperationError) as raised:
                memory.update("denied")
        self.assertEqual(raised.exception.code, "memory_unsafe_path")
        self.assertFalse(outside.exists())

    def test_parent_link_or_junction_is_rejected(self):
        linked_parent = self.root / "data"
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda target: target == linked_parent):
            with self.assertRaises(memory.MemoryOperationError) as raised:
                memory.update("denied")
        self.assertEqual(raised.exception.code, "memory_unsafe_path")
        self.assertFalse(self.path.exists())

    def test_target_directory_is_not_replaced(self):
        self.path.mkdir(parents=True)
        with self.assertRaises(memory.MemoryOperationError) as raised:
            memory.update("denied")
        self.assertEqual(raised.exception.code, "memory_unsafe_path")
        self.assertTrue(self.path.is_dir())

    def test_atomic_replace_failure_preserves_original_and_cleans_temporary(self):
        memory.update("Original")
        with patch.object(memory.os, "replace", side_effect=PermissionError("synthetic denied")):
            with self.assertRaises(memory.MemoryOperationError) as raised:
                memory.update("Not committed", ai=True)
        self.assertEqual(raised.exception.code, "memory_write_failed")
        self.assertEqual(self.path.read_bytes(), b"Original")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_flush_failure_preserves_original_and_cleans_temporary(self):
        memory.update("Original")
        with patch.object(memory.os, "fsync", side_effect=OSError("synthetic disk")):
            with self.assertRaises(memory.MemoryOperationError) as raised:
                memory.update("Not committed", ai=True)
        self.assertEqual(raised.exception.code, "memory_write_failed")
        self.assertEqual(self.disk(), "Original")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_injection_budget_and_off_switch_remain_effective(self):
        # Deterministic text tokens make the wrapper/body boundary explicit.
        # A lowered setting keeps the source; even the truncation notice must
        # fit the same injection budget. It is not a body-only allowance.
        tokenizer = SimpleNamespace(encode=lambda text, add_special_tokens=False: SimpleNamespace(ids=[0] * len(text)))
        body = "SYNTHETIC-LONG-FACTS|" * 30
        with patch("app.context.tokenizer", lambda *args: tokenizer):
            memory.update(body)
            original = self.path.read_bytes()
            self.cfg["memory_tokens"] = 128
            injected = memory.injection()
            allowance = 128 - len(memory.INJECTION_HEADER + memory.TRUNCATION_NOTICE)
            self.assertGreater(allowance, 0)
            self.assertEqual(injected, memory.INJECTION_HEADER + body[:allowance] + memory.TRUNCATION_NOTICE)
            self.assertEqual(len(injected), 128)
            self.assertEqual(self.path.read_bytes(), original)
            # Defensive handling of an externally lowered, invalid setting:
            # if even the wrapper cannot fit, inject nothing rather than exceed.
            self.cfg["memory_tokens"] = 5
            self.assertEqual(memory.injection(), "")
        self.cfg["memory_enabled"] = False
        with patch.object(memory, "read", side_effect=AssertionError("disabled must not read")):
            self.assertEqual(memory.injection(), "")


if __name__ == "__main__":
    unittest.main()
