"""Synthetic interaction roundtrips; no real models, user documents or credentials."""
import asyncio
import copy
import json
import unittest
import httpx
from unittest.mock import patch
from test_core import ROOT
from app import chat,store,turn_queue,jobs,lifecycle,providers,user_interaction as interaction,audit,tools,memory
from app.config import settings,save_settings
from app.server import app

class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await turn_queue.shutdown()
        lifecycle.reset_managed()
        turn_queue.WORKERS.clear();turn_queue.CHANNELS.clear();turn_queue.STOPS.clear()
        chat.ACTIVE.clear();jobs.JOBS.clear();jobs.LOCKS.clear()
        interaction.SIGNALS.clear();interaction.STEERING_PENDING.clear()
        turn_queue.recover()
        save_settings({"memory_enabled":False,"rag_enabled":False,"tools":["ask_user"],"context":32768,"tool_mode":"native"})
        self.conv=store.create(settings())
        async def caps(*args):return {"vision":False,"tools":True,"max_context":32768}
        self.caps=patch.object(providers,"capability",caps);self.caps.start()
    async def asyncTearDown(self):
        await turn_queue.shutdown();turn_queue.recover();self.caps.stop()
    async def wait(self,predicate):
        async with asyncio.timeout(5):
            while not predicate():await asyncio.sleep(.01)
    async def send(self,text="合成问题",mode="native",conv=None):
        return await turn_queue.enqueue((conv or self.conv)["id"],{"content":text,"options":{"tool_mode":mode,"tools":["ask_user"]}})
    async def done(self,turn):
        await self.wait(lambda:turn_queue.get(turn["id"])["status"] in turn_queue.TERMINAL)
        return turn_queue.get(turn["id"])
    async def question(self,turn):
        await self.wait(lambda:turn_queue.get(turn["id"]).get("user_question",{}).get("status")=="pending")
        return turn_queue.get(turn["id"])["user_question"]
    def reply(self,turn,q,text="自定义答案"):
        return interaction.answer(turn["conversation_id"],turn["id"],q["id"],text)
    async def test_native_question_continue_and_refresh_idempotency(self):
        requests=[]
        async def stream(ident,body,stop):
            requests.append(copy.deepcopy(body))
            if len(requests)==1:
                yield {"message":{"tool_calls":[{"function":{"name":"ask_user","arguments":{"question":"选择页面？","options":["一","二"]}}}]},"done":True}
            else:yield {"message":{"content":"已按回答处理"},"done":True,"eval_count":3}
        with patch.object(providers,"resilient_stream",stream):
            turn=await self.send();q=await self.question(turn)
            restored=turn_queue.public(turn_queue.get(turn["id"]))
            self.assertEqual(restored["user_question"],q)
            self.assertEqual(restored["live_message"]["interactions"][0]["status"],"running")
            subscriber=turn_queue.events(self.conv["id"],turn["id"])
            self.assertEqual((await anext(subscriber))["turn"]["user_question"],q)
            await subscriber.aclose()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=("127.0.0.1",1234)),base_url="http://127.0.0.1:8765") as client:
                response=await client.post(f'/api/chat/{self.conv["id"]}/turns/{turn["id"]}/answer',json={"question_id":q["id"],"answer":"自定义答案"})
                self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(self.reply(turn,q)["status"],"answered")
            with self.assertRaises(ValueError):self.reply(turn,q,"不同回答")
            self.assertEqual((await self.done(turn))["status"],"done")
        self.assertTrue(any("自定义答案" in str(x) for x in requests[-1]["messages"]))
        self.assertEqual(turn_queue.get(turn["id"])["user_question"]["status"],"answered")
        self.assertNotIn("自定义答案",json.dumps(audit.listing(),ensure_ascii=False))
    async def test_legacy_question_continue(self):
        decisions=[];final=[]
        async def decide(ident,body):
            decisions.append(copy.deepcopy(body))
            return json.dumps({"action":"call_tool","name":"ask_user","arguments":{"question":"哪个工况？","options":[]}} if len(decisions)==1 else {"action":"answer"},ensure_ascii=False)
        async def stream(ident,body,stop):
            final.append(copy.deepcopy(body));yield {"message":{"content":"完成"},"done":True}
        with patch.object(providers,"decision",decide),patch.object(providers,"resilient_stream",stream):
            turn=await self.send(mode="legacy");q=await self.question(turn);self.reply(turn,q,"工况A")
            self.assertEqual((await self.done(turn))["status"],"done")
        self.assertIn("工况A",json.dumps(final[0]["messages"],ensure_ascii=False))
    async def test_waiting_yields_slot_and_cancel_never_unlocks_other_owner(self):
        other_entered=asyncio.Event();release_other=asyncio.Event()
        async def stream(ident,body,stop):
            text=body["messages"][-1].get("content","")
            if text.endswith("第二对话"):
                other_entered.set();await release_other.wait()
                yield {"message":{"content":"第二完成"},"done":True}
            else:yield {"message":{"tool_calls":[{"function":{"name":"ask_user","arguments":{"question":"继续？"}}}]},"done":True}
        with patch.object(providers,"resilient_stream",stream):
            first=await self.send();await self.question(first)
            first_job=jobs.JOBS[turn_queue.get(first["id"])["job_id"]]
            self.assertEqual(first_job["status"],"waiting_user");self.assertTrue(jobs.active())
            other_conv=store.create(settings());second=await self.send("第二对话",conv=other_conv)
            await asyncio.wait_for(other_entered.wait(),3)
            second_job=jobs.JOBS[turn_queue.get(second["id"])["job_id"]]
            turn_queue.cancel(self.conv["id"],first["id"])
            self.assertEqual((await self.done(first))["status"],"cancelled")
            self.assertTrue(second_job["_holds_lock"]);self.assertTrue(second_job["_held_lock"].locked())
            release_other.set();await self.done(second)
    async def test_answer_reacquires_after_other_task(self):
        starts=0;other_entered=asyncio.Event();other_gate=asyncio.Event();resumed=asyncio.Event()
        async def stream(ident,body,stop):
            nonlocal starts
            if any(x.get("content","").endswith("占用任务") for x in body["messages"]):
                other_entered.set();await other_gate.wait();yield {"message":{"content":"结束"},"done":True};return
            starts+=1
            if starts==1:yield {"message":{"tool_calls":[{"function":{"name":"ask_user","arguments":{"question":"确认？"}}}]},"done":True}
            else:resumed.set();yield {"message":{"content":"恢复执行"},"done":True}
        with patch.object(providers,"resilient_stream",stream):
            first=await self.send();q=await self.question(first)
            other=store.create(settings());second=await self.send("占用任务",conv=other);await other_entered.wait()
            self.reply(first,q);await asyncio.sleep(.08);self.assertFalse(resumed.is_set())
            other_gate.set();await self.done(second);await self.done(first);self.assertTrue(resumed.is_set())
    async def test_same_conversation_normal_send_is_not_answer(self):
        asks=0;requests=[]
        async def stream(ident,body,stop):
            nonlocal asks
            requests.append(copy.deepcopy(body))
            if asks==0:
                asks+=1;yield {"message":{"tool_calls":[{"function":{"name":"ask_user","arguments":{"question":"需要哪个？"}}}]},"done":True}
            else:yield {"message":{"content":"回答"},"done":True}
        with patch.object(providers,"resilient_stream",stream):
            first=await self.send();q=await self.question(first);second=await self.send("排队消息")
            await asyncio.sleep(.05);self.assertEqual(turn_queue.get(second["id"])["status"],"queued")
            self.assertEqual(turn_queue.get(first["id"])["user_question"]["status"],"pending")
            self.reply(first,q);await self.done(first);await self.done(second)
        self.assertNotIn("排队消息",str(requests[1]["messages"]))
        self.assertIn("排队消息",str(requests[2]["messages"]))
    async def test_steer_skips_question_and_changes_current_turn(self):
        requests=[]
        async def stream(ident,body,stop):
            requests.append(copy.deepcopy(body))
            if len(requests)==1:yield {"message":{"tool_calls":[{"function":{"name":"ask_user","arguments":{"question":"请选择？"}}}]},"done":True}
            else:yield {"message":{"content":"采用新方向"},"done":True}
        with patch.object(providers,"resilient_stream",stream):
            turn=await self.send();await self.question(turn)
            response=interaction.steer(self.conv["id"],turn["id"],"改为只生成目录","steer-request-01")
            self.assertEqual(interaction.steer(self.conv["id"],turn["id"],"改为只生成目录","steer-request-01"),response)
            completed=await self.done(turn)
        self.assertEqual(completed["user_question"]["status"],"skipped")
        self.assertEqual(completed["steering"][0]["status"],"applied")
        self.assertIn("改为只生成目录",str(requests[1]["messages"]))
        self.assertEqual(turn_queue.listing(self.conv["id"])["total"],1)
    async def test_steer_discards_partial_tool_call(self):
        started=asyncio.Event();requests=[]
        async def stream(ident,body,stop):
            requests.append(copy.deepcopy(body))
            if len(requests)==1:
                yield {"message":{"content":"正在规划"}}
                started.set()
                # Partially streamed native call is discarded before execute.
                yield {"message":{"tool_calls":[{"function":{"name":"ask_user","arguments":{"question":"旧方向"}}}]}}
                await asyncio.Event().wait()
            else:yield {"message":{"content":"新方向完成"},"done":True}
        with patch.object(providers,"resilient_stream",stream):
            turn=await self.send();await started.wait()
            interaction.steer(self.conv["id"],turn["id"],"按新方向继续","steer-request-02")
            self.assertEqual((await self.done(turn))["status"],"done")
        self.assertNotIn("user_question",turn_queue.get(turn["id"]))
        self.assertIn("按新方向继续",str(requests[-1]["messages"]))
        self.assertEqual(len(store.path(store.get(self.conv["id"]))[-1]["interactions"]),0)
    async def test_cross_conversation_and_invalid_submissions(self):
        async def stream(ident,body,stop):
            yield {"message":{"tool_calls":[{"function":{"name":"ask_user","arguments":{"question":"测试？"}}}]},"done":True}
        with patch.object(providers,"resilient_stream",stream):
            turn=await self.send();q=await self.question(turn)
            with self.assertRaises(ValueError):interaction.answer("different",turn["id"],q["id"],"答")
            with self.assertRaises(ValueError):interaction.answer(self.conv["id"],turn["id"],"stale","答")
            with self.assertRaises(ValueError):interaction.steer(self.conv["id"],turn["id"],"x"*4001,"request-steer")
            turn_queue.cancel(self.conv["id"],turn["id"]);await self.done(turn)
    async def test_synchronous_tool_test_cannot_block(self):
        with self.assertRaisesRegex(ValueError,"聊天"):
            tools.execute("ask_user",{"question":"测试？"},["ask_user"])
    async def test_waiting_job_finish_does_not_release_reused_lock(self):
        one=jobs.enqueue("synthetic","one");lock=await jobs.acquire(one)
        jobs.suspend(one)
        two=jobs.enqueue("synthetic","two");await jobs.acquire(two)
        jobs.finish(one,lock)
        self.assertTrue(two["_held_lock"].locked())
        jobs.finish(two,lock);self.assertFalse(lock.locked())
    async def test_generic_http_copy_failure_and_non_object(self):
        with patch.object(tools,"execute",return_value={"status":500,"text":"synthetic"}):
            event=await chat.call_tool("copy_http_request",{},["copy_http_request"],"native")
            self.assertFalse(event["success"])
        with patch.object(tools,"execute",return_value=[]):
            event=await chat.call_tool("synthetic",{},["synthetic"],"native")
            self.assertFalse(event["success"])

    async def test_unknown_side_effects_and_known_debug_failures(self):
        with patch.object(tools,"execute",side_effect=OSError("synthetic transport failure")):
            for name,args,unknown in (
                ("system_time",{},False),
                ("file_manage",{"action":"info","path":"synthetic.txt"},False),
                ("file_manage",{"action":"edit_text","path":"synthetic.txt","text":"x"},True),
                ("http_request",{"url":"http://127.0.0.1/","method":"GET"},False),
                ("http_request",{"url":"http://127.0.0.1/","method":"POST"},True),
                ("text_create",{"directory":"synthetic","name":"test.txt","text":"x"},True),
            ):
                event=await chat.call_tool(name,args,[name],"native")
                self.assertEqual(bool(event["result"].get("outcome_unknown")),unknown,(name,event))
        with patch.object(tools,"execute") as execute:
            event=await chat.call_tool("text_create",{},["text_create"],"native")
            self.assertIs(event["result"]["executed"],False)
            self.assertNotIn("outcome_unknown",event["result"])
            execute.assert_not_called()
        class NotExecuted(ValueError):
            executed=False
        with patch.object(tools,"execute",side_effect=NotExecuted("synthetic preflight rejection")):
            event=await chat.call_tool("text_create",{"directory":"synthetic","name":"test.txt","text":"x"},["text_create"],"native")
            self.assertNotIn("outcome_unknown",event["result"])
        for result,unknown in (({"status":"failed","exit_code":1,"note":None,"stderr":"SyntaxError"},False),
                               ({"status":"failed","exit_code":1,"note":"synthetic timeout"},True)):
            with patch.object(tools,"execute",return_value=result):
                event=await chat.call_tool("python_run",{"code":"synthetic"},["python_run"],"native")
                self.assertFalse(event["success"])
                self.assertEqual(bool(event["result"].get("outcome_unknown")),unknown)
        self.assertEqual(audit.action_metadata("snapshot_restore",{"action":"private-text"}),("snapshot_restore","restore"))

    async def test_legacy_unknown_write_ends_current_turn(self):
        decisions=[]
        async def decision(ident,body):
            decisions.append(body)
            return json.dumps({"action":"call_tool","name":"text_create","arguments":{"directory":"synthetic","name":"test.txt","text":"x"}})
        with patch.object(providers,"decision",decision),patch.object(tools,"execute",side_effect=OSError("synthetic connection lost")) as execute:
            turn=await turn_queue.enqueue(self.conv["id"],{"content":"合成写入","options":{"tool_mode":"legacy","tools":["text_create"]}})
            completed=await self.done(turn)
        self.assertEqual(completed["status"],"failed")
        self.assertEqual(len(decisions),1)
        self.assertEqual(execute.call_count,1)
        event=store.path(store.get(self.conv["id"]))[-1]["interactions"][0]
        self.assertEqual(event["status"],"outcome_unknown")
        self.assertTrue(event["result"]["outcome_unknown"])

    async def test_rejected_and_unknown_calls_have_single_consistent_audit(self):
        rejected=store.uid()
        event=await chat.call_tool("text_create",{"name":"synthetic"},["text_create"],"native",{"_call_id":rejected})
        self.assertIs(event["result"]["executed"],False)
        rows=[x for x in audit.listing(page_size=100)["items"] if x["id"]==rejected]
        self.assertEqual([x["status"] for x in rows],["failed"])
        uncertain=store.uid()
        with patch("app.toolkit.text_create.run",side_effect=OSError("synthetic transport error")):
            event=await chat.call_tool("text_create",{"directory":"synthetic","name":"test.txt","text":"PRIVATE BODY"},["text_create"],"native",{"_call_id":uncertain})
        self.assertTrue(event["result"]["outcome_unknown"])
        rows=[x for x in audit.listing(page_size=100)["items"] if x["id"]==uncertain]
        self.assertEqual([x["status"] for x in rows],["unknown"])
        self.assertNotIn("PRIVATE BODY",json.dumps(rows))
        save_settings({"memory_enabled":True})
        with patch.object(memory,"update",side_effect=memory.MemoryOperationError("memory_token_limit","synthetic limit")):
            event=await chat.call_tool("memory_update",{"text":"x"},["memory_update"],"native")
        self.assertIs(event["result"]["executed"],False)
        self.assertNotIn("outcome_unknown",event["result"])

    async def test_successful_identical_writes_are_not_deduplicated(self):
        requests=[]
        async def stream(ident,body,stop):
            requests.append(body)
            if len(requests)==1:
                yield {"message":{"tool_calls":[{"function":{"name":"text_create","arguments":{"directory":"synthetic","name":"test.txt","text":"x"}}} for _ in range(2)]},"done":True}
            else:yield {"message":{"content":"合成完成"},"done":True}
        with patch.object(providers,"resilient_stream",stream),patch.object(tools,"execute",return_value={"ok":True,"path":"synthetic"}) as execute:
            turn=await turn_queue.enqueue(self.conv["id"],{"content":"合成重复写入","options":{"tools":["text_create"]}})
            self.assertEqual((await self.done(turn))["status"],"done")
        self.assertEqual(execute.call_count,2)

    async def test_readonly_remote_cannot_steer_privileged_turn(self):
        async def stream(ident,body,stop):
            yield {"message":{"tool_calls":[{"function":{"name":"ask_user","arguments":{"question":"测试？"}}}]},"done":True}
        with patch.object(providers,"resilient_stream",stream):
            turn=await self.send();q=await self.question(turn)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=("192.168.1.42",1234)),base_url="http://192.168.1.3:8765") as client:
                for action,body in (("answer",{"question_id":q["id"],"answer":"答"}),("steer",{"content":"修改文件","client_request_id":"remote-steer-01"})):
                    response=await client.post(f'/api/chat/{self.conv["id"]}/turns/{turn["id"]}/{action}',json=body)
                    self.assertEqual(response.status_code,403,response.text)
            self.reply(turn,q);turn_queue.cancel(self.conv["id"],turn["id"]);await self.done(turn)

if __name__=="__main__":unittest.main()
