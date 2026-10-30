"""Independent v1.6 QA: synthetic queues, protocol boundaries and side effects."""
import asyncio
import copy
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from test_core import ROOT
from app import chat, config, jobs, lifecycle, port_manager, providers, store, tools, turn_queue
from app import user_interaction as interaction


class AstraIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await turn_queue.shutdown()
        lifecycle.reset_managed()
        turn_queue.WORKERS.clear()
        turn_queue.CHANNELS.clear()
        turn_queue.STOPS.clear()
        jobs.JOBS.clear()
        jobs.LOCKS.clear()
        chat.ACTIVE.clear()
        interaction.SIGNALS.clear()
        interaction.STEERING_PENDING.clear()
        turn_queue.recover()
        config.write_json(ROOT / "config/settings.json", {
            **copy.deepcopy(config.DEFAULTS), "memory_enabled": False,
            "rag_enabled": False, "context": 131072, "model": "synthetic-astra",
            "tools": ["ask_user", "system_time", "text_create", "http_request"],
            "tool_mode": "native", "max_tool_calls": 50,
        })
        config.write_json(ROOT / "config/tools/http_request.json", {"proxy_mode": "none", "timeout": 1})
        self.conv = store.create(config.settings())
        self.caps = patch.object(providers, "capability", self.capability)
        self.caps.start()

    @staticmethod
    async def capability(*args):
        return {"vision": True, "tools": True, "max_context": 32768}

    async def asyncTearDown(self):
        await turn_queue.shutdown()
        turn_queue.recover()
        self.caps.stop()

    async def wait(self, predicate, seconds=5):
        async with asyncio.timeout(seconds):
            while not predicate():
                await asyncio.sleep(.005)

    async def send(self, text="SYNTHETIC FIRST", conv=None, **options):
        return await turn_queue.enqueue((conv or self.conv)["id"], {"content": text, "options": options})

    async def done(self, turn):
        await self.wait(lambda: turn_queue.get(turn["id"])["status"] in turn_queue.TERMINAL)
        return turn_queue.get(turn["id"])

    async def question(self, turn):
        await self.wait(lambda: turn_queue.get(turn["id"]).get("user_question", {}).get("status") == "pending")
        return turn_queue.get(turn["id"])["user_question"]

    def last_answer(self, conv=None):
        return store.path(store.get((conv or self.conv)["id"]))[-1]

    async def test_cancel_answer_waiting_to_reacquire_preserves_other_owner(self):
        entered = asyncio.Event()
        gate = asyncio.Event()
        third_entered = asyncio.Event()
        first_requests = []

        async def stream(ident, body, stop):
            texts = [x.get("content", "") for x in body["messages"]]
            if "SYNTHETIC BLOCKER" in texts:
                entered.set()
                await gate.wait()
                yield {"message": {"content": "second finished"}, "done": True}
            elif "SYNTHETIC THIRD" in texts:
                third_entered.set()
                yield {"message": {"content": "third finished"}, "done": True}
            else:
                first_requests.append(copy.deepcopy(body))
                yield {"message": {"tool_calls": [{"function": {"name": "ask_user", "arguments": {"question": "Synthetic choice?"}}}]}, "done": True}

        with patch.object(providers, "resilient_stream", stream):
            first = await self.send()
            question = await self.question(first)
            second_conv = store.create(config.settings())
            second = await self.send("SYNTHETIC BLOCKER", conv=second_conv)
            await asyncio.wait_for(entered.wait(), 3)
            interaction.answer(self.conv["id"], first["id"], question["id"], "Synthetic answer")
            first_job = jobs.JOBS[turn_queue.get(first["id"])["job_id"]]
            await self.wait(lambda: first_job["status"] == "queued")
            third_conv = store.create(config.settings())
            third = await self.send("SYNTHETIC THIRD", conv=third_conv)
            turn_queue.cancel(self.conv["id"], first["id"])
            self.assertEqual((await self.done(first))["status"], "cancelled")
            second_job = jobs.JOBS[turn_queue.get(second["id"])["job_id"]]
            self.assertTrue(second_job["_holds_lock"])
            self.assertTrue(second_job["_held_lock"].locked())
            self.assertFalse(third_entered.is_set())
            gate.set()
            await self.done(second)
            await self.done(third)
        self.assertEqual(len(first_requests), 1)

    async def test_steer_during_real_file_write_finishes_current_and_skips_next(self):
        started = threading.Event()
        release = threading.Event()
        requests = []
        called = []
        real_execute = tools.execute
        folder = ROOT / ("astra-files-" + store.uid())
        folder.mkdir()

        def execute(name, args, selected, scope=None):
            called.append(args["name"])
            started.set()
            if not release.wait(3):
                raise AssertionError("Synthetic write gate did not open")
            return real_execute(name, args, selected, scope)

        async def stream(ident, body, stop):
            requests.append(copy.deepcopy(body))
            if len(requests) == 1:
                yield {"message": {"tool_calls": [{"function": {"name": "text_create", "arguments": {
                    "directory": str(folder), "name": name, "format": "txt", "text": "Synthetic document"}}}
                    for name in ("first", "second")]}, "done": True}
            else:
                yield {"message": {"content": "Synthetic adjusted answer"}, "done": True}

        try:
            with patch.object(providers, "resilient_stream", stream), patch.object(tools, "execute", execute):
                turn = await self.send()
                await self.wait(started.is_set)
                interaction.steer(self.conv["id"], turn["id"], "SYNTHETIC KEEP ONLY FIRST", "astra-steer-file")
                self.assertFalse(release.is_set())
                release.set()
                self.assertEqual((await self.done(turn))["status"], "done")
        finally:
            release.set()
        self.assertEqual(called, ["first"])
        self.assertEqual((folder / "first.txt").read_text(encoding="utf-8"), "Synthetic document")
        self.assertFalse((folder / "second.txt").exists())
        wire = requests[-1]["messages"]
        declaration = next(x for x in wire if x.get("tool_calls"))
        replies = [x for x in wire if x["role"] == "tool"]
        self.assertEqual({x["id"] for x in declaration["tool_calls"]}, {x["tool_call_id"] for x in replies})
        self.assertIn("SYNTHETIC KEEP ONLY FIRST", wire[-1]["content"])
        self.assertTrue(self.last_answer()["interactions"][0]["success"])

    async def test_native_default_fifty_counts_failed_calls_and_closes_every_call_id(self):
        requests = []
        executed = []

        async def stream(ident, body, stop):
            requests.append(copy.deepcopy(body))
            if len(requests) == 1:
                yield {"message": {"tool_calls": [{"function": {"name": "system_time", "arguments": {"synthetic_index": i}}}
                    for i in range(51)]}, "done": True}
            else:
                yield {"message": {"content": "Synthetic summary"}, "done": True}

        async def invoke(name, args, selected, mode, scope=None):
            executed.append(args["synthetic_index"])
            success = args["synthetic_index"] % 5 != 0
            return {"name": name, "arguments": args, "success": success, "result": {"synthetic": args["synthetic_index"]}, "mode": mode, "seconds": .001}

        with patch.object(providers, "resilient_stream", stream), patch.object(chat, "call_tool", invoke):
            turn = await self.send()
            self.assertEqual((await self.done(turn))["status"], "done")
        self.assertEqual(executed, list(range(50)))
        self.assertEqual(len(requests), 2)
        self.assertNotIn("tools", requests[-1])
        replies = [x for x in requests[-1]["messages"] if x["role"] == "tool"]
        self.assertEqual(len(replies), 51)
        self.assertEqual(self.last_answer()["stats"]["tool_calls"], 50)
        self.assertEqual(sum(not x["success"] for x in self.last_answer()["interactions"]), 10)

    async def test_legacy_limit_counts_failures_before_final_answer(self):
        config.save_settings({"max_tool_calls": 3})
        decisions, final, invoked = [], [], []

        async def decision(ident, body):
            decisions.append(copy.deepcopy(body))
            return json.dumps({"action": "call_tool", "name": "system_time", "arguments": {}})

        async def stream(ident, body, stop):
            final.append(copy.deepcopy(body))
            yield {"message": {"content": "Synthetic summary"}, "done": True}

        async def invoke(name, args, selected, mode, scope=None):
            invoked.append(name)
            return {"name": name, "arguments": args, "success": len(invoked) != 2, "result": {"synthetic": True}, "mode": mode, "seconds": .001}

        with patch.object(providers, "decision", decision), patch.object(providers, "resilient_stream", stream), patch.object(chat, "call_tool", invoke):
            turn = await self.send(tool_mode="legacy")
            self.assertEqual((await self.done(turn))["status"], "done")
        self.assertEqual(len(decisions), 3)
        self.assertEqual(len(invoked), 3)
        self.assertEqual(len(final), 1)
        self.assertEqual(self.last_answer()["stats"]["tool_calls"], 3)

    async def test_successful_same_parameters_create_two_files(self):
        folder = ROOT / ("astra-repeat-" + store.uid())
        folder.mkdir()
        arguments = {"directory": str(folder), "name": "synthetic", "format": "txt", "text": "Synthetic repeated operation"}
        requests = []

        async def stream(ident, body, stop):
            requests.append(copy.deepcopy(body))
            if len(requests) == 1:
                yield {"message": {"tool_calls": [{"function": {"name": "text_create", "arguments": arguments}} for _ in range(2)]}, "done": True}
            else:
                yield {"message": {"content": "Two synthetic outputs"}, "done": True}

        with patch.object(providers, "resilient_stream", stream):
            turn = await self.send()
            self.assertEqual((await self.done(turn))["status"], "done")
        events = self.last_answer()["interactions"]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["success"] for event in events))
        self.assertNotEqual(events[0]["call_id"], events[1]["call_id"])
        self.assertNotEqual(events[0]["result"]["path"], events[1]["result"]["path"])
        self.assertEqual(len(list(folder.glob("*.txt"))), 2)

    async def test_oversized_result_stops_turn_preserving_executed_result(self):
        requests, executed = [], []
        payload = "合成结果" * 15000

        async def stream(ident, body, stop):
            requests.append(copy.deepcopy(body))
            yield {"message": {"tool_calls": [{"function": {"name": "system_time", "arguments": {}}} for _ in range(2)]}, "done": True}

        def execute(*args):
            executed.append(True)
            return {"synthetic_payload": payload, "executed": True}

        with patch.object(providers, "resilient_stream", stream), patch.object(tools, "execute", execute):
            turn = await self.send(context=8192)
            completed = await self.done(turn)
        self.assertEqual(completed["status"], "failed")
        self.assertIn("上下文", completed["error"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(executed), 1)
        answer = self.last_answer()
        self.assertEqual(answer["interactions"][0]["result"]["synthetic_payload"], payload)
        self.assertTrue(answer["interactions"][0]["success"])

    async def test_cloud_fragmented_question_answer_keeps_wire_id(self):
        requests = []

        async def stream(ident, body, stop):
            requests.append(copy.deepcopy(body))
            if len(requests) == 1:
                yield {"message": {"tool_calls": [{"index": 0, "id": "call_astra", "function": {"name": "ask_user", "arguments": '{"question":"Synthetic'}}]}}
                yield {"message": {"tool_calls": [{"index": 0, "function": {"arguments": ' choice?","options":["A","B"]}'}}]}, "done": True}
            else:
                yield {"message": {"content": "Synthetic completed"}, "done": True}

        with patch.object(providers, "is_ollama", return_value=False), patch.object(providers, "resilient_stream", stream):
            turn = await self.send()
            question = await self.question(turn)
            interaction.answer(self.conv["id"], turn["id"], question["id"], "B")
            self.assertEqual((await self.done(turn))["status"], "done")
        wire = providers.cloud_messages(requests[-1]["messages"])
        declaration = next(x for x in wire if x.get("tool_calls"))
        reply = next(x for x in wire if x["role"] == "tool")
        self.assertEqual(declaration["tool_calls"][0]["id"], "call_astra")
        self.assertEqual(reply["tool_call_id"], "call_astra")
        self.assertIn('"answer": "B"', reply["content"])
        self.assertEqual(len(self.last_answer()["interactions"]), 1)
        first = providers.cloud_messages(requests[0]["messages"])
        self.assertEqual(wire[:len(first)], first)

    async def test_unknown_post_result_is_not_automatically_submitted_again(self):
        received = []
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        args = {"url": f"http://127.0.0.1:{server.server_port}/", "method": "POST", "body": "SYNTHETIC WRITE"}

        async def stream(ident, body, stop):
            requests.append(copy.deepcopy(body))
            if len(requests) <= 2:
                yield {"message": {"tool_calls": [{"function": {"name": "http_request", "arguments": args}}]}, "done": True}
            else:
                yield {"message": {"content": "Synthetic failure summary"}, "done": True}

        async def decision(ident, body):
            return json.dumps({"action": "call_tool", "name": "http_request", "arguments": args})

        try:
            with patch.object(providers, "resilient_stream", stream), patch.object(providers, "decision", decision):
                for mode in ("native", "legacy"):
                    with self.subTest(mode=mode):
                        before = len(received)
                        conv = store.create(config.settings())
                        turn = await self.send(conv=conv, tool_mode=mode)
                        completed = await self.done(turn)
                        self.assertEqual(completed["status"], "failed")
                        self.assertEqual(len(received) - before, 1, "A completed POST with lost response was automatically sent again")
                        events = self.last_answer(conv)["interactions"]
                        self.assertEqual(len(events), 1)
                        self.assertTrue(events[0]["result"]["outcome_unknown"])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)


class AstraServices(unittest.TestCase):
    def test_owned_process_still_starting_does_not_spawn_again(self):
        record = {"pid": 424242, "created": 123.5, "models": str(ROOT / "models"),
                  "exe": str(ROOT / "synthetic-service.exe"), "port": 11437}
        process = Mock()
        process.create_time.return_value = record["created"]
        process.exe.return_value = record["exe"]
        process.environ.return_value = {"OLLAMA_MODELS": record["models"], "OLLAMA_HOST": "127.0.0.1:11437"}
        process.cmdline.return_value = [record["exe"], "serve"]
        process.net_connections.side_effect = AssertionError("An owned starting process has not opened its listener yet")
        with patch("psutil.Process", return_value=process), patch.object(port_manager, "read_json", return_value=record), \
                patch.object(port_manager, "set_port", side_effect=lambda role, port: port), \
                patch.object(providers, "settings", return_value={"ollama_management": "managed"}), \
                patch.object(providers, "read_json", return_value=record), \
                patch.object(providers.httpx, "get", side_effect=AssertionError("Existing owned process should be returned")), \
                patch.object(providers.subprocess, "Popen") as launch:
            self.assertEqual(port_manager.owned_port("chat"), 11437)
            self.assertEqual(providers.launch_ollama(), record)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
