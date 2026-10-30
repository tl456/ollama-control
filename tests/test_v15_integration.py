"""Synthetic contracts for new filesystem, retrieval and download behavior."""
import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace
from test_core import ROOT, save_settings
from app import tools, downloads, rag_query, providers
from app.config import write_json


class UpgradeIntegrationTests(unittest.TestCase):
    def test_device_failure_rolls_back_settings_before_gateway_mutation(self):
        from app.api.admin import build_router
        before={'gateway_enabled':False,'gateway_bind':'127.0.0.1','gateway_port':11436,'gateway_require_key':False,'inference_device':'auto','embedding_device':'cpu','remote_full_access':False}
        after={**before,'gateway_enabled':True,'inference_device':'cpu'}
        writes=[]
        def save(value):
            writes.append(value.copy())
            return after.copy() if len(writes)==1 else before.copy()
        gateway=AsyncMock()
        ctx=SimpleNamespace(network=SimpleNamespace(is_local=lambda request:True,auth_config=lambda:{},apply_gateway=gateway),
            chat=SimpleNamespace(ACTIVE={}),jobs=SimpleNamespace(active=lambda:False),knowledge=SimpleNamespace(RUNNING={}),FILE_TASKS={},
            python_lab=SimpleNamespace(active_ids=lambda:[]),settings=lambda:before.copy(),save_settings=save,
            providers=SimpleNamespace(apply_device_preferences=lambda *args:(_ for _ in ()).throw(RuntimeError('synthetic device failure'))),background=lambda work:None)
        endpoint=next(route.endpoint for route in build_router(ctx).routes if route.path=='/api/settings' and 'PUT' in route.methods)
        with self.assertRaisesRegex(ValueError,'已保留原设置'):
            asyncio.run(endpoint({'gateway_enabled':True,'inference_device':'cpu'},None))
        self.assertEqual(writes,[{'gateway_enabled':True,'inference_device':'cpu'},before])
        gateway.assert_not_awaited()

    def test_each_builtin_has_independent_definition_and_implementation(self):
        folder=Path(tools.__file__).parent/'toolkit'
        for name in ('system_time','list_directory','file_info','search_files','read_file','read_attachment','python_run'):
            self.assertTrue((folder/name/'__init__.py').is_file())
            self.assertTrue((folder/name/'README.md').is_file())
        ids=[t['id'] for t in tools.list_tools()]
        self.assertEqual(len(ids),len(set(ids)))

    def test_hidden_file_outside_alias_is_readable_and_original_unchanged(self):
        directory=ROOT/'v15-free-files';directory.mkdir(exist_ok=True)
        target=directory/'.synthetic.dat';target.write_text('SYNTHETIC V15\n第二行',encoding='utf-8')
        tools.save_tool_configuration('read_file',{'tool_read_mode':'native'})
        result=tools.execute('read_file',{'path':str(target),'mode':'lines','start_line':2,'line_count':1},['read_file'])
        self.assertEqual(result['text'],'第二行')
        self.assertEqual(target.read_text(encoding='utf-8'),'SYNTHETIC V15\n第二行')
        entries=tools.execute('list_directory',{'path':str(directory)},['list_directory'])['items']
        self.assertIn('.synthetic.dat',[x['name'] for x in entries])

    def test_tool_settings_are_independent(self):
        tools.save_tool_configuration('search_files',{'search_depth':2,'start_directory':'SYNTHETIC START'})
        tools.save_tool_configuration('list_directory',{'start_directory':'OTHER SYNTHETIC'})
        self.assertEqual(tools.tool_configuration('search_files')['values']['start_directory'],'SYNTHETIC START')
        self.assertEqual(tools.tool_configuration('list_directory')['values']['start_directory'],'OTHER SYNTHETIC')
        self.assertTrue((ROOT/'config/tools/search_files.json').is_file())

    def test_finished_download_dismissal_persists_without_deleting_layers(self):
        job={'id':'synthetic-v15','model':'synthetic-model','done':True,'created':time.time(),'ended':time.time(),'completed':123}
        current={job['id']:job}
        downloads.remember(job)
        self.assertTrue(any(x['id']==job['id'] for x in downloads.listing({})))
        result=downloads.dismiss(job['id'],current)
        self.assertFalse(result['model_data_deleted'])
        self.assertFalse(any(x['id']==job['id'] for x in downloads.listing({})))

    def test_running_download_cannot_be_dismissed(self):
        with self.assertRaises(ValueError):downloads.dismiss('running',{'running':{'id':'running','done':False}})

    def test_dismiss_write_failure_does_not_hide_live_record(self):
        current={'failing':{'id':'failing','done':True,'ended':time.time()}}
        with patch.object(downloads,'write_json',side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):downloads.dismiss('failing',current)
        self.assertFalse(current['failing'].get('dismissed',False))

    def test_download_retention_is_bounded(self):
        write_json(downloads.PATH,[{'id':'old','done':True,'ended':time.time()-8*86400}])
        self.assertFalse(any(x['id']=='old' for x in downloads.listing({})))

    def test_query_does_not_accept_invented_numbers_or_keywords(self):
        q,k=rag_query.validate_result(json.dumps({'query':'A-123 Z 10Hz 刚度','keywords':['A-123','10Hz','虚构词']},ensure_ascii=False),'A-123 Z 10Hz 刚度')
        self.assertEqual(k,['A-123','10Hz'])
        with self.assertRaises(ValueError):rag_query.validate_result('{"query":"A-124 20Hz","keywords":[]}','A-123 10Hz')
        with self.assertRaises(ValueError):rag_query.validate_result('{"query":"含铁","keywords":[]}','不含铁')
        for old,new in (('A-123 Z 10Hz','B-123 X 10kHz'),('DEMO-BEARING N/mm','DEMO-GEAR N/m'),('1MPa','1mPa')):
            with self.assertRaises(ValueError):rag_query.validate_result(json.dumps({'query':new,'keywords':[]}),old)

    def test_query_complete_signed_numeric_literals_and_counts_are_preserved(self):
        changed = (
            ('检索2026年2个样件的记录', '检索2026年样件的记录'),
            ('样件1和11的记录', '样件11的记录'),
            ('温度-20℃的记录', '温度20℃的记录'),
            ('温度+20℃的记录', '温度20℃的记录'),
            ('温度 - 20℃的记录', '温度20℃的记录'),
            ('温度−20℃的记录', '温度20℃的记录'),
            ('位移-.5mm的记录', '位移.5mm的记录'),
            ('位移-0.5mm的记录', '位移0.5mm的记录'),
            ('数值1e-3的记录', '数值1e+3的记录'),
            ('2个样件各做2次', '2个样件进行试验'),
            ('2个样件进行试验', '2个样件做2次'),
            ('123456-A-240101 X轴 20Hz', '123455-A-240101 X轴 20Hz'),
        )
        for original, query in changed:
            with self.subTest(original=original, query=query), self.assertRaises(ValueError):
                rag_query.validate_result(json.dumps({'query':query,'keywords':[]}),original)

    def test_query_valid_reordering_keeps_model_numbers_decimals_and_signs(self):
        unchanged = (
            ('2026年2个样件各做2次', '2次试验，2026年2个样件'),
            ('123456-A-240101 X轴 -20℃ 0.5mm 20Hz', '20Hz 0.5mm -20℃，123456-A-240101 X轴'),
            ('温度 - 20℃', '温度-20℃'),
            ('温度−20℃', '温度-20℃'),
            ('温度＋20℃', '温度+20℃'),
            ('位移-.5mm', '位移-.5mm的记录'),
            ('数值1e-3', '检索数值1e-3的记录'),
        )
        for original, query in unchanged:
            with self.subTest(original=original, query=query):
                result, _ = rag_query.validate_result(json.dumps({'query':query,'keywords':[]}),original)
                self.assertEqual(result,query)

    def test_query_numeric_condition_loss_uses_original_fallback_once(self):
        for original, query in (('检索2026年2个样件的记录','检索2026年样件的记录'),
                                ('检索温度-20℃的记录','检索温度20℃的记录')):
            calls=[]
            async def changed(ident,body):
                calls.append(body)
                yield {'message':{'content':json.dumps({'query':query,'keywords':[]})}}
            with self.subTest(original=original), patch.object(providers,'stream_model',changed):
                result=asyncio.run(rag_query.prepare(original,{'model':'synthetic','rag_query_enabled':True}))
                self.assertEqual(len(calls),1)
                self.assertTrue(result['fallback'])
                self.assertEqual(result['status'],'fallback')
                self.assertEqual(result['query'],original)
                self.assertIn('ValueError',result['error'])

    def test_query_single_bounded_model_call_and_record(self):
        calls=[]
        async def fake(ident,body):
            calls.append(body)
            yield {'message':{'content':'{"query":"A-123 10Hz","keywords":["A-123","10Hz"]}'}}
        with patch.object(providers,'stream_model',fake):
            result=asyncio.run(rag_query.prepare('A-123 10Hz',{'rag_query_enabled':True,'model':'synthetic','provider':'local'}))
        self.assertEqual(len(calls),1)
        self.assertEqual(result['status'],'extracted')
        self.assertNotIn('tools',calls[0])
        self.assertFalse(calls[0]['think'])

    def test_query_failure_returns_original_without_retry(self):
        count=[]
        async def bad(ident,body):
            count.append(1)
            yield {'message':{'content':'not JSON'}}
        with patch.object(providers,'stream_model',bad):
            result=asyncio.run(rag_query.prepare('合成原问题',{'model':'synthetic','rag_query_enabled':True}))
        self.assertEqual(count,[1])
        self.assertTrue(result['fallback'])
        self.assertEqual(result['query'],'合成原问题')

    def test_query_disabled_does_not_call_model(self):
        with patch.object(providers,'stream_model',side_effect=AssertionError('must not call')):
            result=asyncio.run(rag_query.prepare('合成原问题',{'rag_query_enabled':False}))
        self.assertEqual(result['status'],'disabled')

    def test_query_cancellation_before_first_packet_is_prompt(self):
        async def case():
            async def blocked(ident,body):
                await asyncio.sleep(30)
                yield {'message':{'content':'{}'}}
            stop=asyncio.Event()
            with patch.object(providers,'stream_model',blocked):
                task=asyncio.create_task(rag_query.prepare('合成问题',{'model':'synthetic','rag_query_enabled':True},stop=stop))
                await asyncio.sleep(.01);stop.set()
                with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,.5)
        asyncio.run(case())

    def test_query_already_cancelled_does_not_create_provider_iterator(self):
        async def case():
            stop=asyncio.Event();stop.set()
            with patch.object(providers,'stream_model',side_effect=AssertionError('not called')):
                with self.assertRaises(asyncio.CancelledError):await rag_query.prepare('合成问题',{'model':'synthetic'},stop=stop)
        asyncio.run(case())


if __name__=='__main__':unittest.main()
