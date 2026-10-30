"""Run actual browser preference modules in a minimal DOM, without private chat data."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
NODE=shutil.which("node")
HARNESS=r"""
const assert=require('assert'),vm=require('vm'),fs=require('fs');
const source=fs.readFileSync(process.argv[1],'utf8');
function node(tag='div',data={}){
 return {tagName:tag.toUpperCase(),dataset:{...data},open:false,children:[],parentElement:null,
 querySelectorAll(selector){
   let all=[];function walk(n){for(const c of n.children){if(selector==='details'&&c.tagName==='DETAILS'||selector==='details[data-fold-memory]'&&c.tagName==='DETAILS'&&c.dataset.foldMemory||selector==='[data-message]'&&c.dataset.message)all.push(c);walk(c);}}walk(this);return all;
 },matches(selector){return selector==='[data-message]'&&!!this.dataset.message;}};
}
function message(id,kinds=['thinking','stats','context','recovery']){
 const m=node('article',{message:id});
 for(const kind of kinds){const d=node('details',{detailKind:kind});d.parentElement=m;m.children.push(d);}
 return m;
}
function boot(store=new Map(),throws=false){
 const handlers={},localStorage={getItem:k=>store.get(k)||null,setItem(k,v){if(throws)throw new Error('quota');store.set(k,v);}};
 const context={window:{},localStorage,document:{addEventListener:(name,handler)=>handlers[name]=handler},Date,JSON,Map,Object,Number,String};
 vm.runInNewContext(source,context);
 return {api:context.window.WorkbenchDetails,handlers,store};
}
"""
@unittest.skipUnless(NODE,"Node.js unavailable")
class HistoryDetailState(unittest.TestCase):
    def run_js(self,body):
        run=subprocess.run([NODE,"-e",HARNESS+body,str(ROOT/"web/modules/detail-state.js")],capture_output=True,text=True,encoding="utf-8",timeout=15)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
    def test_redraw_reload_and_conversation_isolation(self):
        self.run_js(r"""
const state=boot(),m=message('message-a');state.api.apply(m,'chat-a');
for(const d of m.children){d.open=true;state.handlers.toggle({target:d});}
const replaced=message('message-a');state.api.apply(replaced,'chat-a');
assert(replaced.children.every(d=>d.open));
const refreshed=boot(state.store),fresh=message('message-a');refreshed.api.apply(fresh,'chat-a');
assert(fresh.children.every(d=>d.open));
const other=message('message-a');refreshed.api.apply(other,'chat-b');assert(other.children.every(d=>!d.open));
fresh.children[0].open=false;refreshed.api.capture(fresh);
const after=message('message-a');boot(state.store).api.apply(after,'chat-a');assert(!after.children[0].open);assert(after.children[1].open);
""")
    def test_tool_call_ids_survive_order_changes(self):
        self.run_js(r"""
const s=boot(),m=message('m',[]),a=node('details',{toolKey:'call-a'}),b=node('details',{toolKey:'call-b'});
m.children=[a,b];s.api.apply(m,'c');a.open=true;s.api.capture(m);
const next=message('m',[]);next.children=[node('details',{toolKey:'call-b'}),node('details',{toolKey:'call-a'})];s.api.apply(next,'c');
assert.strictEqual(next.children[0].open,false);assert.strictEqual(next.children[1].open,true);
""")
    def test_storage_limits_and_expiry(self):
        self.run_js(r"""
const s=boot();for(let i=0;i<2020;i++){const m=message('m'+i,['thinking']);s.api.apply(m,'c');m.children[0].open=true;s.api.capture(m);}
let rows=JSON.parse(s.store.get('workbench-detail-state-v1'));assert(rows.length<=2000);
assert(!rows.some(x=>JSON.parse(x[0])[1]==='m0'));
const expired=new Map([['workbench-detail-state-v1',JSON.stringify([[JSON.stringify(['c','old','thinking']),true,Date.now()-31*86400000]])]]);
const m=message('old',['thinking']);boot(expired).api.apply(m,'c');assert.strictEqual(m.children[0].open,false);
""")
    def test_storage_failure_does_not_break_chat(self):
        self.run_js(r"""
const s=boot(new Map(),true),m=message('m',['thinking']);s.api.apply(m,'c');m.children[0].open=true;
s.api.capture(m);const next=message('m',['thinking']);s.api.apply(next,'c');assert(next.children[0].open);
""")
    def test_summary_click_remembers_request(self):
        self.run_js(r"""
const s=boot(),m=message('m',['thinking']);s.api.apply(m,'c');
const summary={parentElement:m.children[0]},target={closest:x=>x==='summary'?summary:null};
s.handlers.click({target,defaultPrevented:false});
const replacement=message('m',['thinking']);s.api.apply(replacement,'c');assert(replacement.children[0].open);
""")

@unittest.skipUnless(NODE,"Node.js unavailable")
class TopLevelFolds(unittest.TestCase):
    def test_actual_layout_boot_and_persistence(self):
        code=r"""
const assert=require('assert'),vm=require('vm'),fs=require('fs'),source=fs.readFileSync(process.argv[1],'utf8');
function start(saved={}){
 const elements=[],store=new Map(Object.entries(saved));let toggles=0;
 class Element{
   constructor(id='',cls=''){this.id=id;this.className=cls;this.children=[];this.dataset={};this.attrs={};this.style={setProperty(){}};this.parentElement=null;elements.push(this);}
   get classList(){const e=this;return {contains(c){return e.className.split(' ').includes(c);},toggle(c,on){const s=new Set(e.className.split(' ').filter(Boolean));on?s.add(c):s.delete(c);e.className=[...s].join(' ');}};}
   setAttribute(k,v){this.attrs[k]=v;}
   before(el){el.parentElement=this.parentElement;}
   append(...nodes){for(const n of nodes){n.parentElement=this;this.children.push(n);}}
   prepend(n){this.append(n);}
 }
 const top=new Element('','topbar'),guide=new Element('','beginner-guide'),body=new Element(),html=new Element(),newChat=new Element('new-chat');
 for(const id of ['model-runtime','tool-toggles','knowledge-bar'])new Element(id).parentElement=body;
 const doc={body,documentElement:html,createElement:()=>new Element(),getElementById:id=>elements.find(x=>x.id===id),querySelectorAll:()=>[],querySelector:s=>s.startsWith('#')?elements.find(x=>x.id===s.slice(1)):elements.find(x=>x.classList.contains(s.slice(1)))};
 const context={window:{dispatchEvent(){}},Event:class{},document:doc,localStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)},JSON,Object,Number,String,Math};
 vm.runInNewContext(source,context);context.window.WorkbenchLayout.boot();
 return {api:context.window.WorkbenchLayout,doc,elements,store,guide};
}
const s=start();assert.strictEqual(s.elements.filter(e=>e.className==='chat-fold').length,3);
const tools=s.doc.getElementById('tool-toggles').parentElement;tools.children[0].onclick();
assert(tools.classList.contains('is-collapsed'));assert.strictEqual(s.guide.parentElement,tools);
s.api.sidebar(true);s.api.zoom(200);
s.api.boot();assert.strictEqual(s.elements.filter(e=>e.dataset.foldId).length,3);
const next=start(Object.fromEntries(s.store));
assert(next.doc.getElementById('tool-toggles').parentElement.classList.contains('is-collapsed'));
assert(next.doc.documentElement.classList.contains('sidebar-collapsed'));assert.strictEqual(next.doc.body.style.zoom,'2');
next.api.zoom(20);assert.strictEqual(next.doc.body.style.zoom,'0.5');
"""
        result=subprocess.run([NODE,"-e",code,str(ROOT/"web/modules/layout.js")],capture_output=True,text=True,encoding="utf-8",timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=="__main__":unittest.main()
