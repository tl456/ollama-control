"""Behavioral tests for stable request history, bounded tools and dynamic ports."""
import asyncio
import copy
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_core import ROOT
from app import chat, config, context, providers, store, port_manager

class ConversationV16Tests(unittest.TestCase):
    def opts(self):
        return {**copy.deepcopy(config.DEFAULTS), "model":"synthetic", "tools":[], "provider":"local", "memory_enabled":False}
    def conv(self):
        return {"id":store.uid(),"messages":[],"leaf":None,"options":self.opts()}
    def test_time_and_memory_updates_do_not_rewrite_previous_prefix(self):
        conv=self.conv()
        store.append(conv,"user","合成问题一")
        conv["_runtime_context"]=[{"role":"system","content":"时间一"},{"role":"user","content":"记忆版本一"}]
        context.freeze_current_input(conv,False)
        first=context.history(conv,False)
        store.append(conv,"assistant","合成答复一",wire_messages=[{"role":"assistant","content":"合成答复一"}])
        store.append(conv,"user","合成问题二")
        conv["_runtime_context"]=[{"role":"system","content":"时间二"},{"role":"user","content":"记忆版本二"}]
        context.freeze_current_input(conv,False)
        second=context.history(conv,False)
        self.assertEqual(second[:len(first)],first)
        self.assertEqual(second[len(first)]["content"],"合成答复一")
        self.assertEqual([x["content"] for x in second[-3:]],["时间二","记忆版本二","合成问题二"])
    def test_cloud_tool_trace_is_exact_and_does_not_mutate_stored_arguments(self):
        trace=[{"role":"assistant","content":"","tool_calls":[{"id":"call_synthetic","type":"function","function":{"name":"system_time","arguments":{}}}]},
               {"role":"tool","tool_call_id":"call_synthetic","tool_name":"system_time","content":"合成结果"},
               {"role":"assistant","content":"完成"}]
        before=copy.deepcopy(trace)
        result=providers.cloud_messages(trace)
        self.assertEqual(result[0]["tool_calls"][0]["function"]["arguments"],"{}")
        self.assertEqual(result[1]["tool_call_id"],"call_synthetic")
        self.assertEqual(trace,before)
    def test_cloud_usage_accumulates_auxiliary_and_main_requests(self):
        sink=[]; token=providers.USAGE_SINK.set(sink)
        try:
            record=providers.record_usage({"prompt_tokens":1000,"completion_tokens":20,"prompt_cache_hit_tokens":800})
            providers.record_usage({"completion_tokens":30},record)
            providers.record_usage({"prompt_tokens":500,"completion_tokens":40,"prompt_tokens_details":{"cached_tokens":200}})
            result=providers.usage_totals(sink)
        finally:providers.USAGE_SINK.reset(token)
        self.assertEqual((result["input_tokens"],result["output_tokens"],result["cached_input_tokens"],result["model_requests"]),(1500,70,1000,2))
    def test_internal_wire_trace_does_not_expand_ui_page(self):
        conv=self.conv()
        store.append(conv,"user","问题",request_content="injected private body",request_context=[{"role":"system","content":"state"}])
        store.append(conv,"assistant","答复",wire_messages=[{"role":"tool","content":"private tool body"}])
        result=store.window(conv)
        self.assertNotIn("request_content",result["messages"][0])
        self.assertNotIn("wire_messages",result["messages"][1])
        self.assertEqual(result["message_page"]["window_leaf"],conv["leaf"])
    def test_visual_results_are_separated_from_json_and_bounded(self):
        event={"result":{"preview_id":"synthetic","visual_images":[{"data_base64":"iVBORtest","mime_type":"image/png"}]*4}}
        values=chat.take_visual(event,[],8192,2048)
        self.assertEqual(len(values[0]["images"]),3)
        self.assertNotIn("visual_images",event["result"])
        self.assertEqual(event["result"]["visual_pages_omitted"],1)
        self.assertTrue(providers.cloud_messages(values)[0]["content"][1]["image_url"]["url"].startswith("data:image/png;"))
    def test_repeated_failures_only_stops_same_failed_operation(self):
        errors=[{"name":"file_manage","arguments":{"action":"edit"},"success":False}]*3
        self.assertTrue(chat.repeated_failures(errors))
        self.assertFalse(chat.repeated_failures(errors[:2]))
        self.assertFalse(chat.repeated_failures(errors[:2]+[{**errors[0],"success":True}]))
    def test_port_collision_increments_and_skips_reserved_without_touching_listener(self):
        with socket.socket() as original:
            original.bind(("127.0.0.1",0)); original.listen()
            port=original.getsockname()[1]
            with patch.object(port_manager,"set_port",side_effect=lambda role,p,requested=None:p):
                allocated,actual=port_manager.bind_available("web","127.0.0.1",port,excluded=[port+1])
            try:
                self.assertGreaterEqual(actual,port+2)
                with socket.create_connection(("127.0.0.1",port),timeout=1):pass
            finally:allocated.close()

if __name__=="__main__":unittest.main()
