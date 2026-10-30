"""Independent review regressions, using only isolated synthetic data."""
import asyncio
import copy
import json
import os
import subprocess
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_BOOT = tempfile.TemporaryDirectory(prefix="v16-review-import-")
with patch.dict(os.environ, {"LOCAL_AI_ROOT": _BOOT.name}):
    from app import audit, chat, config, context, lifecycle, providers, skills, snapshots, store, tools
    from app.toolkit import common


class IndependentReview16(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v16-review-")
        self.root = Path(self.temp.name)
        for name in ("config/tools", "plugins/imported", "data", "logs", "cache"):
            (self.root / name).mkdir(parents=True)
        self.patches = [patch.object(module, "ROOT", self.root)
                        for module in (audit, config, context, lifecycle, skills, snapshots, store, tools, common)]
        self.patches.append(patch.dict(os.environ, {"LOCAL_AI_ROOT": str(self.root)}))
        for item in self.patches:
            item.start()
        config.write_json(self.root / "config/settings.json",
                          {**config.DEFAULTS, "memory_enabled": False, "context": 32768})

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_cancel_stops_unstarted_calls_in_same_native_batch(self):
        async def exercise():
            conv = store.create(config.settings())
            conv["options"].update(tools=["text_create"], tool_mode="native")
            store.append(conv, "user", "Create two synthetic files.")
            stop = asyncio.Event()
            invoked = []
            original = chat.call_tool

            async def invoke(name, args, selected, mode, scope=None):
                result = await original(name, args, selected, mode, scope)
                invoked.append(args["name"])
                if len(invoked) == 1:
                    stop.set()
                return result

            async def stream(*args):
                yield {"message": {"tool_calls": [
                    {"function": {"name": "text_create", "arguments": {
                        "directory": str(self.root), "name": name, "text": "synthetic"}}}
                    for name in ("first", "second")]}, "done": True}

            try:
                with patch.object(providers, "resilient_stream", stream), patch.object(chat, "call_tool", invoke):
                    async for _ in chat.run(conv, {"tools": True, "vision": False, "max_context": 32768}, stop):
                        pass
            except asyncio.CancelledError:
                pass
            self.assertEqual(invoked, ["first"])
            self.assertTrue((self.root / "first.md").is_file())
            self.assertFalse((self.root / "second.md").exists())
        asyncio.run(exercise())

    def test_http_error_is_not_a_successful_tool_event(self):
        async def exercise():
            with patch.object(tools, "execute", return_value={"status": 404, "text": "synthetic missing"}):
                result = await chat.call_tool("http_request", {"url": "https://example.invalid/"},
                                              ["http_request"], "native")
            self.assertFalse(result["success"])
            self.assertEqual(result["result"]["status"], 404)
        asyncio.run(exercise())

    def test_exported_builtin_uses_its_own_configuration(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"synthetic response")
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            tools.import_plugin(tools.export_plugin("http_request"))
            config.write_json(self.root / "config/settings.json",
                              {**config.DEFAULTS, "plugins": ["copy_http_request"]})
            for name, value in (("http_request", "SYNTHETIC_ORIGINAL"), ("copy_http_request", "SYNTHETIC_COPY")):
                config.write_json(self.root / "config/tools" / (name + ".json"), {
                    "bearer_token": value, "credential_host": "127.0.0.1", "proxy_mode": "none"})
            result = tools.execute("copy_http_request",
                {"url": f"http://127.0.0.1:{server.server_port}/"}, ["copy_http_request"])
            self.assertEqual(result["status"], 200)
            self.assertEqual(received, ["Bearer SYNTHETIC_COPY"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_completed_turn_prefix_is_stable_when_time_changes(self):
        async def exercise():
            conv = store.create(config.settings())
            conv["options"].update(tools=[], tool_mode="off")
            captured = []
            async def stream(ident, body, stop):
                captured.append(copy.deepcopy(providers.cloud_messages(body["messages"])))
                yield {"message": {"content": "synthetic answer"}, "done": True}
            with patch.object(providers, "resilient_stream", stream):
                first = store.append(conv, "user", "first question")
                async for _ in chat.run(conv, {"tools": True, "vision": False, "max_context": 32768}, asyncio.Event()):
                    pass
                first_context = copy.deepcopy(first["request_context"])
                store.append(conv, "user", "second question")
                async for _ in chat.run(conv, {"tools": True, "vision": False, "max_context": 32768}, asyncio.Event()):
                    pass
            self.assertEqual(first["request_context"], first_context)
            prefix = captured[0] + [{"role": "assistant", "content": "synthetic answer"}]
            self.assertEqual(captured[1][:len(prefix)], prefix)
        asyncio.run(exercise())

    def test_invalid_action_body_is_not_retained_in_audit(self):
        marker = "SYNTHETIC PRIVATE DOCUMENT SENTENCE"
        with self.assertRaises(ValueError):
            tools.execute("file_manage", {"action": marker}, ["file_manage"])
        self.assertNotIn(marker, json.dumps(audit.listing()))

    def test_audit_ttl_cleanup_respects_total_disk_quota(self):
        with patch.object(audit, "MAX_BYTES", 1024 * 1024):
            rows = [(str(i), time.time() - 366*86400, "old", "m"*128, "p"*128, "t"*128,
                     "a"*64, "success", 0, "x"*200, "", "") for i in range(850)]
            try:
                with audit._db() as db:
                    db.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            except sqlite3.OperationalError as error:
                # Reserving worst-case journal space is a valid fix: the larger
                # fixture is then refused before it can exceed the total quota.
                self.assertIn("full", str(error).lower())
                with audit._db() as db:
                    db.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows[:300])
            peak = [0]
            def measure():
                peak[0] = max(peak[0], sum(p.stat().st_size for p in (self.root/"audit").glob("*") if p.is_file()))
                return 0
            with audit._db() as db:
                db.set_progress_handler(measure, 10)
                audit._cleanup(db)
            self.assertLessEqual(peak[0], audit.MAX_BYTES)
            self.assertEqual(audit.listing()["total"], 0)

    def test_factory_reset_clears_new_managed_data_but_keeps_audit(self):
        artifacts = [self.root/"data/skills/example.json", self.root/"data/snapshots/synthetic.bin",
                     self.root/"cache/office-previews/synthetic/page.png"]
        for file in artifacts:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"synthetic")
        audit.record("system_time", {})
        lifecycle.reset_managed()
        for file in artifacts:
            self.assertFalse(file.exists(), str(file.relative_to(self.root)))
        self.assertEqual(audit.listing()["total"], 1)

    @unittest.skipUnless(os.name == "nt", "Windows directory junction semantics")
    def test_recycling_junction_never_recycles_its_target(self):
        from app import file_operations
        target = self.root / "target"
        target.mkdir()
        (target / "synthetic.txt").write_text("synthetic", encoding="utf-8")
        link = self.root / "link"
        created = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                                 capture_output=True)
        if created.returncode:
            self.skipTest("Directory junction creation unavailable")
        try:
            with patch.object(file_operations, "recycle", return_value={"ok": True}) as recycle:
                try:
                    file_operations.file_manage({"action": "recycle", "path": str(link)})
                except ValueError:
                    return  # Explicitly refusing link mutation is also safe.
            self.assertEqual(Path(recycle.call_args.args[0]), link)
        finally:
            link.rmdir()
            self.assertTrue((target / "synthetic.txt").is_file())

    def test_http_stored_credential_is_redacted_if_server_echoes_it(self):
        marker = "SYNTHETIC_STORED_CREDENTIAL"
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write((self.headers.get("Authorization") or "").encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            config.write_json(self.root/"config/tools/http_request.json", {
                "bearer_token": marker, "credential_host": "127.0.0.1", "proxy_mode": "none"})
            result = tools.execute("http_request", {"url": f"http://127.0.0.1:{server.server_port}/"},
                                   ["http_request"])
            self.assertNotIn(marker, json.dumps(result))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
