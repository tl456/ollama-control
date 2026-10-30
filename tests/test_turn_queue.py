"""1.3 outbox regressions. All model calls and documents are synthetic."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx
from test_core import ROOT
from app import attachments, chat, jobs, lifecycle, store, turn_queue
from app.config import save_settings, settings
from app.server import app, CONTROL_TOKEN


class TurnQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await turn_queue.shutdown()
        lifecycle.reset_managed()
        turn_queue.WORKERS.clear()
        turn_queue.CHANNELS.clear()
        turn_queue.STOPS.clear()
        chat.ACTIVE.clear()
        jobs.JOBS.clear()
        turn_queue.recover()
        save_settings({"memory_enabled": False})
        self.conv = store.create(settings())
        self.calls, self.gates = [], {}

        async def caps(*args):
            return {"vision": False, "tools": True, "max_context": 262144}

        async def run(conv, capability, stop):
            content = store.path(conv)[-1]["content"]
            self.calls.append({"content": content, "history": copy.deepcopy(store.path(conv)), "options": copy.deepcopy(conv["options"]), "remote": conv.get("_remote_request"), "restricted": conv.get("_restricted_request")})
            answer = store.append(conv, "assistant", "", thinking="", interactions=[], stats={})
            store.put(conv)
            yield {"type": "start", "message_id": answer["id"], "mode": "off"}
            try:
                while content in self.gates and not self.gates[content].is_set():
                    if stop.is_set():
                        raise asyncio.CancelledError()
                    await asyncio.sleep(.005)
                if content == "FAIL":
                    answer["error"] = "合成失败"
                    answer["stats"] = {"complete": False}
                    yield {"type": "error", "message": "合成失败"}
                else:
                    answer["content"] = "回答：" + content
                    answer["stats"] = {"complete": True}
                    yield {"type": "delta", "text": answer["content"]}
                yield {"type": "done", "conversation": store.window(conv)}
            finally:
                store.put(conv)
                chat.ACTIVE.pop(conv["id"], None)

        self.caps = patch("app.providers.capability", caps)
        self.run = patch("app.chat.run", run)
        self.caps.start()
        self.run.start()

    async def asyncTearDown(self):
        await turn_queue.shutdown()
        turn_queue.recover()  # Other legacy TestClient suites do not run lifespan.
        self.run.stop()
        self.caps.stop()

    async def send(self, text, **extra):
        return await turn_queue.enqueue(self.conv["id"], {"content": text, **extra})

    async def wait_for(self, predicate, seconds=3):
        async with asyncio.timeout(seconds):
            while not predicate():
                await asyncio.sleep(.01)

    async def completed(self, turn):
        await self.wait_for(lambda: turn_queue.get(turn["id"])["status"] in turn_queue.TERMINAL)
        return turn_queue.get(turn["id"])

    def fixture(self):
        meta = attachments.upload("synthetic.txt", "原始合成附件 42".encode())
        return attachments.extract(meta["id"])

    async def test_same_conversation_fifo_future_messages_not_in_history(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        two = await self.send("第二")
        three = await self.send("第三")
        self.assertEqual([m["content"] for m in store.path(store.get(self.conv["id"])) if m["role"] == "user"], ["第一"])
        self.assertEqual([m["content"] for m in self.calls[0]["history"]], ["第一"])
        self.gates["第一"].set()
        await self.completed(three)
        self.assertEqual([c["content"] for c in self.calls], ["第一", "第二", "第三"])
        self.assertIn("回答：第一", [m["content"] for m in self.calls[1]["history"]])
        self.assertNotIn("第三", [m["content"] for m in self.calls[1]["history"]])
        self.assertEqual(turn_queue.get(one["id"])["status"], "done")
        self.assertEqual(turn_queue.get(two["id"])["status"], "done")

    async def test_snapshot_options_attachments_and_future_defaults_are_independent(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        meta = self.fixture()
        body = {"content": "第二", "options": {"model": "synthetic-4b", "context": 102400, "tools": []}, "attachments": [{"id": meta["id"]}]}
        two = await turn_queue.enqueue(self.conv["id"], body)
        body["options"]["model"] = "mutated-client"
        body["attachments"].clear()
        persisted = store.get(self.conv["id"])
        persisted["options"]["model"] = "future-default"
        store.put(persisted)
        self.gates["第一"].set()
        await self.completed(two)
        self.assertEqual(self.calls[1]["options"]["model"], "synthetic-4b")
        self.assertEqual(self.calls[1]["options"]["context"], 102400)
        self.assertEqual(self.calls[1]["history"][-1]["attachments"][0]["text"], "原始合成附件 42")
        self.assertEqual(store.get(self.conv["id"])["options"]["model"], "future-default")
        self.assertEqual(turn_queue.get(one["id"])["status"], "done")

    async def test_cancel_waiter_does_not_interrupt_running(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        two = await self.send("第二")
        turn_queue.cancel(self.conv["id"], two["id"])
        self.assertFalse(turn_queue.STOPS[one["id"]].is_set())
        self.gates["第一"].set()
        await self.completed(one)
        self.assertEqual([c["content"] for c in self.calls], ["第一"])
        self.assertEqual(turn_queue.get(two["id"])["status"], "cancelled")

    async def test_stop_running_pauses_dependents_until_explicit_resume(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        two = await self.send("第二")
        turn_queue.cancel(self.conv["id"], one["id"])
        await self.completed(one)
        self.assertEqual(turn_queue.get(two["id"])["status"], "paused")
        self.assertEqual(len(self.calls), 1)
        three = await self.send("第三")
        self.assertEqual(three["status"], "paused")
        turn_queue.resume(self.conv["id"])
        await self.completed(three)
        self.assertEqual([c["content"] for c in self.calls], ["第一", "第二", "第三"])

    async def test_failure_pauses_followers_without_silent_retry(self):
        self.gates["FAIL"] = asyncio.Event()
        one = await self.send("FAIL")
        await self.wait_for(lambda: len(self.calls) == 1)
        two = await self.send("第二")
        self.gates["FAIL"].set()
        self.assertEqual((await self.completed(one))["status"], "failed")
        self.assertEqual(turn_queue.get(two["id"])["status"], "paused")
        self.assertIn("合成失败", turn_queue.listing(self.conv["id"])["paused_reason"])
        self.assertEqual(len(self.calls), 1)

    async def test_idempotency_survives_completion_and_body_retry(self):
        one = await self.send("第一", client_request_id="synthetic-request-1")
        same = await self.send("不同重试正文", client_request_id="synthetic-request-1")
        self.assertEqual(one["id"], same["id"])
        await self.completed(one)
        again = await self.send("重试", client_request_id="synthetic-request-1")
        self.assertEqual(one["id"], again["id"])
        self.assertEqual(len(self.calls), 1)

    async def test_queue_limit_and_pagination(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        for index in range(7):
            await self.send(f"等候{index}")
        with self.assertRaisesRegex(ValueError, "队列已满"):
            await self.send("超过上限")
        listing = turn_queue.listing(self.conv["id"], page=2, page_size=5)
        self.assertEqual((listing["total"], len(listing["items"])), (8, 3))
        self.assertEqual(listing["pending_count"], 8)
        turn_queue.stop_all()
        await self.completed(one)

    async def test_edit_has_optimistic_revision_and_preserves_attachment_snapshot(self):
        self.gates["第一"] = asyncio.Event()
        await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        meta = self.fixture()
        two = await self.send("第二", attachments=[{"id": meta["id"]}], options={"model": "original-model"})
        original = turn_queue.get(two["id"])
        changed_meta = attachments.load(meta["id"])
        changed_meta["text"] = "后续磁盘变化，不得替换快照"
        attachments.save(changed_meta)
        edited = await turn_queue.edit(self.conv["id"], two["id"], {"revision": two["revision"], "content": "已修改"})
        self.assertEqual(turn_queue.get(two["id"])["attachment_snapshots"], original["attachment_snapshots"])
        self.assertEqual(edited["options"]["model"], "original-model")
        with self.assertRaisesRegex(ValueError, "已变化"):
            await turn_queue.edit(self.conv["id"], two["id"], {"revision": two["revision"], "content": "冲突写"})
        self.gates["第一"].set()
        await self.completed(two)
        self.assertEqual(self.calls[1]["content"], "已修改")

    async def test_restart_marks_running_interrupted_and_queue_paused(self):
        with patch.object(turn_queue, "kick"):
            one = await self.send("运行中")
            two = await self.send("待发")
        with store.connect() as db:
            db.execute("UPDATE chat_turns SET status='running' WHERE id=?", (one["id"],))
        turn_queue.recover()
        self.assertEqual(turn_queue.get(one["id"])["status"], "interrupted")
        self.assertEqual(turn_queue.get(two["id"])["status"], "paused")
        await asyncio.sleep(.03)
        self.assertFalse(self.calls)
        turn_queue.resume(self.conv["id"])
        await self.completed(two)
        self.assertEqual([c["content"] for c in self.calls], ["待发"])

    async def test_store_upsert_keeps_outbox_and_erase_cascades(self):
        with patch.object(turn_queue, "kick"):
            one = await self.send("待发")
        conv = store.get(self.conv["id"])
        store.append(conv, "user", "历史")
        store.put(conv)
        self.assertEqual(turn_queue.get(one["id"])["content"], "待发")
        store.erase(self.conv["id"])
        with self.assertRaises(KeyError):
            turn_queue.get(one["id"])

    async def test_version_reset_clears_outbox_via_foreign_key(self):
        with patch.object(turn_queue, "kick"):
            one = await self.send("待发")
        lifecycle.reset_managed()
        with self.assertRaises(KeyError):
            turn_queue.get(one["id"])

    async def test_events_resync_then_live_without_duplicate_history(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        stream = turn_queue.events(self.conv["id"], one["id"], after=0)
        first = await anext(stream)
        self.assertEqual(first["type"], "resync")
        self.assertEqual(first["live_message"]["content"], "")
        cursor = first["event_id"]
        self.gates["第一"].set()
        packets = [p async for p in stream]
        self.assertEqual(sum(p.get("text") == "回答：第一" for p in packets), 1)
        self.assertTrue(all(p["event_id"] > cursor for p in packets))
        self.assertEqual(turn_queue.get(one["id"])["status"], "done")

    async def test_http_subscriber_disconnect_does_not_cancel_worker(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        stream = turn_queue.events(self.conv["id"], one["id"])
        await anext(stream)
        await stream.aclose()
        self.assertFalse(turn_queue.STOPS[one["id"]].is_set())
        self.gates["第一"].set()
        self.assertEqual((await self.completed(one))["status"], "done")

    async def test_terminal_stats_appended_while_consumer_yields_are_not_dropped(self):
        # Deterministically reproduce HTTP backpressure: the subscriber snapshots
        # only delta, then the producer finishes before that delta has been sent.
        with patch.object(turn_queue, "kick"):
            turn = await self.send("终止事件竞态")
        stream = turn_queue.events(self.conv["id"], turn["id"])
        self.assertEqual((await anext(stream))["type"], "resync")
        turn_queue.emit(turn["id"], {"type": "delta", "text": "已经回答"})
        self.assertEqual((await anext(stream))["type"], "delta")
        with store.connect() as db:
            db.execute("UPDATE chat_turns SET status='done' WHERE id=?", (turn["id"],))
        turn_queue.emit(turn["id"], {"type": "stats", "complete": True, "output_tokens": 5})
        turn_queue.emit(turn["id"], {"type": "done"})
        turn_queue.emit(turn["id"], {"type": "turn_state", "turn": turn_queue.public(turn_queue.get(turn["id"]))})
        tail = [packet async for packet in stream]
        self.assertEqual([packet["type"] for packet in tail], ["stats", "done", "turn_state"])
        self.assertTrue(tail[0]["complete"])

    async def test_event_buffer_and_terminal_retention_are_bounded(self):
        with patch.object(turn_queue, "kick"):
            one = await self.send("待发")
        for index in range(500):
            turn_queue.emit(one["id"], {"type": "delta", "text": "X" * 4096})
        channel = turn_queue.CHANNELS[one["id"]]
        self.assertLessEqual(len(channel["events"]), turn_queue.MAX_EVENTS)
        self.assertLessEqual(channel["chars"], turn_queue.MAX_EVENT_CHARS)
        stream = turn_queue.events(self.conv["id"], one["id"], after=1)
        self.assertEqual((await anext(stream))["type"], "resync")
        await stream.aclose()
        self.assertEqual(channel["cursor"], 500)

    async def test_remote_snapshot_cannot_enable_code_or_skills(self):
        with patch.object(turn_queue, "kick"):
            one = await turn_queue.enqueue(self.conv["id"], {"content": "远端", "options": {"tools": ["python_run", "read_file", "system_time"]}}, remote=True)
        stored = turn_queue.get(one["id"])
        self.assertEqual(stored["options"]["tools"], ["system_time"])
        self.assertEqual(stored["options"]["skill_ids"], [])
        self.assertTrue(stored["remote"])
        self.assertNotIn("remote", one)

    async def test_owner_granted_remote_without_password_keeps_full_tools_at_execution(self):
        save_settings({"remote_full_access": True})
        one = await turn_queue.enqueue(self.conv["id"], {"content": "已授权合成远程任务",
            "options": {"tools": ["python_run", "read_file", "system_time"]}}, remote=True, can_manage=True)
        await self.completed(one)
        self.assertTrue(self.calls[-1]["remote"])
        self.assertFalse(self.calls[-1]["restricted"])
        self.assertIn("python_run", self.calls[-1]["options"]["tools"])

    async def test_remote_full_access_revocation_before_execution_still_restricts(self):
        save_settings({"remote_full_access": True})
        with patch.object(turn_queue, "kick"):
            one = await turn_queue.enqueue(self.conv["id"], {"content": "授权已撤销的合成任务",
                "options": {"tools": ["python_run", "system_time"]}}, remote=True, can_manage=True)
        save_settings({"remote_full_access": False})
        turn_queue.kick(self.conv["id"])
        await self.completed(one)
        self.assertTrue(self.calls[-1]["restricted"])

    async def test_wrong_conversation_and_cloud_confirmation_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "数据去向"):
            await self.send("云端", options={"provider": "synthetic-cloud"})
        with patch.object(turn_queue, "kick"):
            one = await self.send("待发")
        other = store.create(settings())
        with self.assertRaises(KeyError):
            turn_queue.cancel(other["id"], one["id"])

    async def test_regenerate_reuses_last_user_without_duplicate_and_refuses_pending(self):
        conv = store.get(self.conv["id"])
        user = store.append(conv, "user", "重生成")
        store.put(conv)
        one = await self.send("", regenerate=True)
        await self.completed(one)
        path = store.path(store.get(self.conv["id"]))
        self.assertEqual([m["id"] for m in path if m["role"] == "user"], [user["id"]])
        self.gates["第一"] = asyncio.Event()
        await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 2)
        with self.assertRaisesRegex(ValueError, "仍有待发"):
            await self.send("", regenerate=True)

    async def test_http_queue_contract_options_edit_and_quit_permissions(self):
        self.gates["第一"] = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
            base = "/api/chat/" + self.conv["id"] + "/turns"
            first = await client.post(base, json={"content": "第一", "client_request_id": "synthetic-http-one"})
            self.assertEqual(first.status_code, 202)
            await self.wait_for(lambda: len(self.calls) == 1)
            two = (await client.post(base, json={"content": "第二"})).json()["turn"]
            self.assertEqual((await client.patch("/api/conversations/" + self.conv["id"], json={"options": {"model": "next-model"}})).status_code, 200)
            self.assertEqual((await client.patch("/api/conversations/" + self.conv["id"], json={"title": "blocked"})).status_code, 400)
            edited = await client.patch(base + "/" + two["id"], json={"revision": two["revision"], "content": "修改第二"})
            self.assertEqual(edited.status_code, 200)
            self.assertEqual((await client.post("/api/ui/quit")).status_code, 403)
            self.assertEqual((await client.post("/api/ui/quit", headers={"X-Local-Control": CONTROL_TOKEN})).status_code, 409)
            self.gates["第一"].set()
            await self.completed(two)
            self.assertEqual((await client.get(base)).json()["pending_count"], 0)
            self.assertEqual((await client.get("/api/conversations/" + self.conv["id"])).json()["options"]["model"], "next-model")

    async def test_reference_tracking_includes_paused_pending_attachment(self):
        meta = self.fixture()
        with patch.object(turn_queue, "kick"):
            one = await self.send("待发", attachments=[{"id": meta["id"]}])
        turn_queue.stop_all()
        self.assertEqual(turn_queue.get(one["id"])["status"], "paused")
        self.assertIn(meta["id"], turn_queue.referenced_attachments())
        packets = [packet async for packet in turn_queue.events(self.conv["id"], one["id"])]
        self.assertEqual(packets[0]["type"], "resync")
        self.assertEqual(packets[-1]["turn"]["status"], "paused")

    async def test_server_generated_skill_and_rag_snapshots_ignore_client_private_fields(self):
        from app import skills
        selected = skills.listing()["items"][0]["id"]
        with patch.object(turn_queue, "kick"):
            one = await self.send("任务", options={"skill_ids": [selected], "skill_snapshot": [{"instructions": "伪造"}], "_rag_top_k": 10,
                                                   "attachment_ids": ["forged"], "python_run_ids": ["forged"]})
        opts = turn_queue.get(one["id"])["options"]
        self.assertEqual(opts["skill_snapshot"], skills.snapshot([selected]))
        self.assertEqual(opts["_rag_top_k"], 2)
        self.assertNotIn("attachment_ids", opts)
        save_settings({"rag_top_k": 7})
        edited = await turn_queue.edit(self.conv["id"], one["id"], {"revision": one["revision"], "content": "只改正文"})
        self.assertEqual(edited["rag_top_k"], 2)
        self.assertEqual(turn_queue.get(one["id"])["options"]["skill_snapshot"], opts["skill_snapshot"])

    async def test_invalid_sampling_and_snapshot_size_are_rejected_without_queueing(self):
        for opts in ({"context": {}}, {"temperature": {}}, {"temperature": float("nan")}, {"top_p": 3}, {"tool_mode": "invalid"}):
            with self.subTest(options=opts), self.assertRaises(ValueError):
                await self.send("参数检查", options=opts)
        self.assertEqual(turn_queue.listing(self.conv["id"])["total"], 0)

    async def test_generation_parameters_are_server_snapshot_not_later_global_defaults(self):
        initial = settings()
        with patch.object(turn_queue, "kick"):
            one = await self.send("任务", options={"_generation_settings": {"system_prompt": "forged", "output_tokens": 1}})
        opts = turn_queue.get(one["id"])["options"]
        self.assertEqual(opts["_generation_settings"]["output_tokens"], initial["output_tokens"])
        self.assertEqual(opts["_generation_settings"]["system_prompt"], initial["system_prompt"])
        save_settings({"output_tokens": 512, "system_prompt": "新的合成默认指令"})
        await turn_queue.edit(self.conv["id"], one["id"], {"revision": one["revision"], "content": "只修改任务"})
        edited = turn_queue.get(one["id"])["options"]
        self.assertEqual(edited["_generation_settings"], opts["_generation_settings"])

    async def test_real_chat_uses_frozen_generation_but_live_memory_rag_disable(self):
        from app import context, knowledge, memory
        opts = turn_queue.options(self.conv["options"])
        opts.update(tool_mode="off", tools=[], rag_enabled=True, rag_ids=["synthetic-kb"], rag_mode="original")
        conv = store.get(self.conv["id"])
        conv["options"] = opts
        store.append(conv, "user", "验证固定参数")
        memory.update("不应注入的合成记忆", memory.read()["revision"])
        save_settings({"output_tokens": 512, "system_prompt": "之后变更的合成提示词", "memory_enabled": False, "rag_enabled": False})
        seen = []
        async def model_stream(ident, body, stop):
            seen.append(copy.deepcopy(body))
            yield {"message": {"content": "已验证"}, "done": True}
        # Stop the setUp fake chat runner only for this integration assertion.
        self.run.stop()
        try:
            with patch("app.providers.resilient_stream", model_stream), patch.object(knowledge, "search") as search:
                packets = [p async for p in chat.run(conv, {"vision": False, "tools": True}, asyncio.Event())]
                self.assertFalse(search.called)
            self.assertEqual(seen[0]["options"]["num_predict"], opts["_generation_settings"]["output_tokens"])
            self.assertEqual(seen[0]["messages"][0]["content"], opts["_generation_settings"]["system_prompt"])
            self.assertNotIn("不应注入的合成记忆", json.dumps(seen[0]["messages"], ensure_ascii=False))
            self.assertNotIn("tools", seen[0])
            self.assertTrue(any(p["type"] == "done" for p in packets))
            self.assertEqual(context.preview(conv)["output_tokens"], opts["_generation_settings"]["output_tokens"])
        finally:
            self.run.start()

    async def test_two_conversations_share_backend_fifo_and_cancel_waiter(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        other = store.create(settings())
        two = await turn_queue.enqueue(other["id"], {"content": "不同对话第二"})
        await self.wait_for(lambda: other["id"] in chat.ACTIVE)
        self.assertEqual(len(self.calls), 1)
        turn_queue.stop_current(other["id"])
        self.assertEqual((await self.completed(two))["status"], "cancelled")
        self.assertFalse(turn_queue.STOPS[one["id"]].is_set())
        self.gates["第一"].set()
        await self.completed(one)

    async def test_normal_shutdown_interrupts_running_and_retains_paused_followers(self):
        self.gates["第一"] = asyncio.Event()
        one = await self.send("第一")
        await self.wait_for(lambda: len(self.calls) == 1)
        two = await self.send("第二")
        await turn_queue.shutdown()
        self.assertEqual(turn_queue.get(one["id"])["status"], "interrupted")
        self.assertEqual(turn_queue.get(two["id"])["status"], "paused")
        self.assertFalse(chat.ACTIVE)
        self.assertFalse(turn_queue.WORKERS)
        turn_queue.recover()
        await asyncio.sleep(.02)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
